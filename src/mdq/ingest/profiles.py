"""Source profiles — how one vendor's columns map onto the canonical schema.

A profile is a **data declaration, not code**: adding support for a new column layout
means adding a `SourceProfile` to `PROFILES`, and nothing else in the codebase changes.

The single most important field is `timestamp_kind`. The HuggingFace trap — a column
called `timestamp_ms` that is *not* a UTC epoch but a Chicago wall clock expressed in
milliseconds — is handled by **declaring** what the column means, never by guessing.
Every profile states the kind of every timestamp candidate it will accept:

| kind | meaning |
|---|---|
| `chicago_wall_naive` | naive datetime holding local wall-clock components |
| `epoch_ms_chicago_wall` | int ms which, decoded naively, *is* the local wall clock |
| `epoch_ms_utc` | an honest unix epoch in milliseconds |
| `iso_utc` | an ISO-8601 string carrying a UTC offset |
| `date` | a calendar date; the bar's instant is a *label*, not a trading time |

Timestamp candidates are ordered, so `hf_minute` prefers the explicit
`timestamp_chicago_wall` column and falls back to decoding `timestamp_ms` — with the
kind changing accordingly, which a single `timestamp_kind` string could not express.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from mdq.domain.frequency import Frequency
from mdq.domain.schema import C

__all__ = [
    "GENERIC",
    "HF_DAILY",
    "HF_MINUTE",
    "PROFILES",
    "SourceProfile",
    "TimestampKind",
    "TimestampSource",
    "detect_profile",
    "profile_by_name",
]

TimestampKind = Literal[
    "chicago_wall_naive",
    "epoch_ms_chicago_wall",
    "epoch_ms_utc",
    "iso_utc",
    "date",
]

#: Canonical name for the timestamp column, which `BAR_SCHEMA` does not carry directly
#: (it carries the *derived* `ts_utc` / `ts_local` pair).
TIMESTAMP = "timestamp"


@dataclass(frozen=True)
class TimestampSource:
    """One acceptable timestamp column, and what its values actually mean."""

    column: str
    kind: TimestampKind


@dataclass(frozen=True)
class SourceProfile:
    """A named mapping from a source layout onto the canonical columns.

    Attributes:
        name: Identifier surfaced in `IngestStats` so a report says which layout was
            used.
        timestamps: Acceptable timestamp columns, in priority order.
        required: Canonical name → candidate source columns. A file missing one of
            these cannot be ingested at all. Only `contract` is required beyond the
            timestamp: a row with no price is still a locatable row, and locatable
            rows are kept and flagged rather than rejected.
        optional: Canonical name → candidate source columns; absent columns become
            all-null and are flagged downstream.
        discriminators: Columns that must exist for this profile to *match*, without
            being mapped. They are what tells two layouts from the same vendor apart.
        frequency_hint: Declared frequency; `None` means "infer from the data".
        case_insensitive: Match candidate columns ignoring case and surrounding
            whitespace — what makes hand-made CSVs work.
    """

    name: str
    timestamps: tuple[TimestampSource, ...]
    required: Mapping[str, tuple[str, ...]]
    optional: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    discriminators: tuple[str, ...] = ()
    frequency_hint: Frequency | None = None
    case_insensitive: bool = False

    @property
    def timestamp_kind(self) -> TimestampKind:
        """The kind of the preferred timestamp column."""
        return self.timestamps[0].kind

    def resolve_timestamp(self, columns: Iterable[str]) -> TimestampSource | None:
        """The first declared timestamp column present in `columns`."""
        lookup = self._lookup(columns)
        for candidate in self.timestamps:
            actual = lookup.get(self._key(candidate.column))
            if actual is not None:
                return TimestampSource(actual, candidate.kind)
        return None

    def resolve(self, columns: Iterable[str]) -> dict[str, str]:
        """Canonical name → actual source column, for every mapping that resolves.

        The timestamp is included under `"timestamp"`. Names that do not resolve are
        simply absent, so a caller can tell "missing" from "present but empty".
        """
        lookup = self._lookup(columns)
        resolved: dict[str, str] = {}
        for canonical, candidates in (*self.required.items(), *self.optional.items()):
            for candidate in candidates:
                actual = lookup.get(self._key(candidate))
                if actual is not None:
                    resolved[canonical] = actual
                    break
        timestamp = self.resolve_timestamp(columns)
        if timestamp is not None:
            resolved[TIMESTAMP] = timestamp.column
        return resolved

    def missing(self, columns: Iterable[str]) -> list[str]:
        """Canonical names this profile needs and `columns` does not supply."""
        names = list(columns)
        resolved = self.resolve(names)
        gaps = [name for name in self.required if name not in resolved]
        if TIMESTAMP not in resolved:
            gaps.append(TIMESTAMP)
        return sorted(gaps)

    def detect(self, columns: Iterable[str]) -> bool:
        """True when `columns` supplies everything this profile requires."""
        names = list(columns)
        lookup = self._lookup(names)
        if any(self._key(name) not in lookup for name in self.discriminators):
            return False
        return not self.missing(names)

    def _key(self, name: str) -> str:
        return name.strip().casefold() if self.case_insensitive else name

    def _lookup(self, columns: Iterable[str]) -> dict[str, str]:
        """Match key → actual column name. Earlier columns win a case collision."""
        found: dict[str, str] = {}
        for name in columns:
            found.setdefault(self._key(name), name)
        return found


#: The vendor's minute layout. `timestamp_chicago_wall` is naive Chicago wall clock and
#: `timestamp_ms`, decoded naively, equals it exactly — so the fallback is *not* a UTC
#: epoch, and saying so here is what keeps every bar off by five hours from happening.
HF_MINUTE = SourceProfile(
    name="hf_minute",
    timestamps=(
        TimestampSource("timestamp_chicago_wall", "chicago_wall_naive"),
        TimestampSource("timestamp_ms", "epoch_ms_chicago_wall"),
    ),
    required={C.CONTRACT: ("contract_symbol",)},
    optional={
        C.EXCHANGE: ("exchange",),
        C.ROOT: ("root",),
        C.OPEN: ("open",),
        C.HIGH: ("high",),
        C.LOW: ("low",),
        C.CLOSE: ("close",),
        C.VOLUME: ("volume",),
    },
    # Only the minute layout has a minute-of-day; without this the daily layout (which
    # also carries `timestamp_ms`) would match here first.
    discriminators=("minute_of_day",),
    frequency_hint=Frequency.MINUTE,
)

#: The vendor's daily layout. `date` is a calendar label, so the canonical instant is
#: midnight UTC on that date — documented as a label, never as a trading time.
HF_DAILY = SourceProfile(
    name="hf_daily",
    timestamps=(TimestampSource("date", "date"),),
    required={C.CONTRACT: ("contract_symbol",)},
    optional={
        C.EXCHANGE: ("exchange",),
        C.ROOT: ("root",),
        C.OPEN: ("open",),
        C.HIGH: ("high",),
        C.LOW: ("low",),
        C.CLOSE: ("close",),
        C.VOLUME: ("volume",),
        C.OPEN_INTEREST: ("open_interest",),
    },
    discriminators=("open_interest",),
    frequency_hint=Frequency.DAILY,
)

#: The fallback for anything hand-made or third-party. Aliases are case-insensitive and
#: the timestamp kind defaults to Chicago wall clock, overridable per ingest through
#: `IngestOptions.timestamp_kind`; frequency is inferred from the data.
GENERIC = SourceProfile(
    name="generic",
    timestamps=(
        TimestampSource("timestamp", "chicago_wall_naive"),
        TimestampSource("ts", "chicago_wall_naive"),
        TimestampSource("datetime", "chicago_wall_naive"),
        TimestampSource("date", "chicago_wall_naive"),
        TimestampSource("time", "chicago_wall_naive"),
    ),
    required={C.CONTRACT: ("contract", "symbol", "contract_symbol", "ticker")},
    optional={
        C.EXCHANGE: ("exchange", "venue"),
        C.ROOT: ("root", "product"),
        C.OPEN: ("open", "o"),
        C.HIGH: ("high", "h"),
        C.LOW: ("low", "l"),
        C.CLOSE: ("close", "c", "last"),
        C.VOLUME: ("volume", "vol", "v"),
        C.OPEN_INTEREST: ("open_interest", "oi"),
    },
    frequency_hint=None,
    case_insensitive=True,
)

#: Matched in order; `generic` is last and is also the fallback when nothing matches.
PROFILES: tuple[SourceProfile, ...] = (HF_MINUTE, HF_DAILY, GENERIC)


def detect_profile(
    columns: Iterable[str],
    profiles: Iterable[SourceProfile] | None = None,
) -> SourceProfile:
    """The first profile that claims `columns`, falling back to `generic`.

    Falling back rather than raising is deliberate: `generic` will then report exactly
    which canonical columns are missing as a `MISSING_REQUIRED_COLUMN` reject, which is
    a far more useful answer than "no profile matched".
    """
    names = list(columns)
    for profile in profiles if profiles is not None else PROFILES:
        if profile.detect(names):
            return profile
    return GENERIC


def profile_by_name(name: str) -> SourceProfile:
    """Look a shipped profile up by name.

    Raises:
        KeyError: if no profile has that name.
    """
    for profile in PROFILES:
        if profile.name == name:
            return profile
    raise KeyError(f"unknown source profile {name!r}; known: {[p.name for p in PROFILES]}")
