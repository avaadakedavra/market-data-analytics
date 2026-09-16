"""Fault injection — the recall *and* precision harness for the quality engine.

`tests.support.synth` produces perfectly clean bars; this module breaks them in one
specific, named way and states exactly what the engine should say about it:

```python
frame, expected = inject(clean, SwapHighLow(rows=[10]))
assert_findings_match(run_checks(...), expected)
```

`assert_findings_match` checks both directions. Every expected finding must appear with
the right row ids — that is *recall*. And no finding may appear that was not expected —
that is *precision*, and it is the half that actually protects the user of this tool. A
check that flags everything has perfect recall and is worthless.

Each fault is therefore deliberately **surgical**: it violates one invariant and leaves
the others intact. `NegativePrice` moves `low` (which no other check reads) rather than
`close` (which would also break the OHLC relation and the return series);
`ConflictingDuplicate` perturbs `volume` rather than a price for the same reason. Where a
fault genuinely has two consequences — a price spike produces an outlier on the way in
*and* on the way out — both are declared, because hiding one would weaken the precision
assertion.

Faults that prevent a row from becoming a bar at all (a non-existent local time, a ragged
CSV line) cannot be expressed as a bar frame. They contribute to a **rejects** frame
instead, in the shape `mdq.quality.checks.malformed_record` documents; use
`inject_with_rejects` when a scenario needs one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any, Protocol

import polars as pl

from mdq.domain.findings import RejectReason, Severity
from mdq.domain.schema import BAR_SCHEMA, C
from mdq.quality.report import QualityReport
from mdq.time.localise import CHICAGO

__all__ = [
    "ALL_FAULTS",
    "REJECTS_SCHEMA",
    "AmbiguousLocalTime",
    "CarriedForwardSettlement",
    "CloseOutsideRange",
    "ConflictingDuplicate",
    "DropRange",
    "DropSession",
    "ExactDuplicate",
    "Expected",
    "Fault",
    "FlatZeroVolumeBar",
    "FutureTimestamp",
    "MalformedLine",
    "NegativePrice",
    "NegativeVolume",
    "NonexistentLocalTime",
    "NullField",
    "OffGridSeconds",
    "PriceSpike",
    "ShiftWallClockToUtc",
    "SwapHighLow",
    "ZeroVolumeWithRange",
    "assert_findings_match",
    "inject",
    "inject_with_rejects",
    "merge_contracts",
    "no_rejects",
    "rejects_frame",
]


#: The shape `IngestResult.rejects` is documented to have (PLAN §3.1). Optional columns
#: are present here so a test exercises the enriched path rather than the minimum.
REJECTS_SCHEMA: pl.Schema = pl.Schema(
    [
        (C.ROW_ID, pl.UInt32),
        (C.CONTRACT, pl.String),
        (C.REASON, pl.String),
        ("raw_values", pl.String),
        ("source", pl.String),
    ]
)


@dataclass(frozen=True)
class Expected:
    """One finding the engine must produce, and nothing more.

    Attributes:
        check_id: The check that must report it.
        count: Sum of `count` across that check's findings — i.e. how many bars (or, for
            a gap, how many absent minutes) the check should account for.
        row_ids: Every `row_id` the check's evidence must name. Empty for findings about
            data that is absent rather than wrong.
        severity: Required severity, when the point of the test *is* the severity (a
            regime downgrade). `None` means "any".
    """

    check_id: str
    count: int
    row_ids: tuple[int, ...] = ()
    severity: Severity | None = None


class Fault(Protocol):
    """A named corruption of a clean bar frame."""

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        """Return the corrupted frame and what the engine must say about it."""
        ...

    def rejects(self) -> list[dict[str, Any]]:
        """Rows that never became bars, in `REJECTS_SCHEMA` shape."""
        ...


class _BarFault:
    """Default `rejects()` for faults that only touch bars."""

    def rejects(self) -> list[dict[str, Any]]:
        return []


# --------------------------------------------------------------------------- #
# duplicates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExactDuplicate(_BarFault):
    """Re-deliver `n` bars byte-for-byte at the same `(contract, ts_utc)`.

    Copies are appended with fresh `row_id`s, as a re-sent file would arrive. Nothing is
    contradicted, so the engine must call this INFO — `cleanse` collapses it losslessly.
    """

    n: int = 1
    rows: Sequence[int] | None = None

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        originals = _pick(frame, self.rows, self.n)
        copies = _renumber(originals, _next_row_id(frame))
        combined = pl.concat([frame, copies], how="vertical")
        ids = tuple(sorted(_ids(originals) + _ids(copies)))
        return combined, [
            Expected("duplicate_timestamp", len(ids), ids, Severity.INFO),
        ]


@dataclass(frozen=True)
class ConflictingDuplicate(_BarFault):
    """Re-deliver `n` bars at the same instant with a **different** volume.

    Volume rather than a price on purpose: a contradictory price would also break the
    OHLC relation and the return series, and the fault would then be testing three checks
    at once instead of isolating this one.
    """

    n: int = 1
    rows: Sequence[int] | None = None
    volume_delta: int = 7

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        originals = _pick(frame, self.rows, self.n)
        copies = _renumber(originals, _next_row_id(frame)).with_columns(
            (pl.col(C.VOLUME) + self.volume_delta).alias(C.VOLUME)
        )
        combined = pl.concat([frame, copies], how="vertical")
        ids = tuple(sorted(_ids(originals) + _ids(copies)))
        return combined, [
            Expected("duplicate_timestamp", len(ids), ids, Severity.ERROR),
        ]


# --------------------------------------------------------------------------- #
# absence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DropRange(_BarFault):
    """Delete every bar whose local wall clock lies in `[start, end)`.

    The surviving neighbours bracket a hole of `minutes_missing` bars, which is what
    `intrabar_gap` must report — but **only** if the session is still ACTIVE afterwards.
    Removing enough of a session to make it DORMANT is a different test (and yields no
    finding, correctly); keep the range short relative to the session.
    """

    start: datetime
    end: datetime
    expect_finding: bool = True

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        inside = (pl.col(C.TS_LOCAL) >= self.start) & (pl.col(C.TS_LOCAL) < self.end)
        dropped = frame.filter(inside)
        if dropped.height == 0:
            raise ValueError(f"DropRange({self.start}, {self.end}) matched no bars")
        kept = frame.filter(~inside)
        if not self.expect_finding:
            return kept, []
        bracket = _bracketing_rows(frame, dropped)
        return kept, [
            Expected("intrabar_gap", dropped.height, bracket, Severity.WARNING),
        ]


@dataclass(frozen=True)
class DropSession(_BarFault):
    """Delete a whole `session_date` for one contract (or for all of them).

    Pick an **interior** session: the expected-coverage window is the contract's own
    first-to-last ACTIVE session, so deleting the first or last one shrinks the window
    instead of leaving a hole, and nothing is missing.
    """

    session: date_type
    contract: str | None = None
    severity: Severity = Severity.WARNING

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        target = pl.col(C.SESSION_DATE) == self.session
        if self.contract is not None:
            target = target & (pl.col(C.CONTRACT) == self.contract)
        dropped = frame.filter(target)
        if dropped.height == 0:
            raise ValueError(f"DropSession({self.session}, {self.contract}) matched no bars")
        contracts = sorted(set(dropped.get_column(C.CONTRACT).to_list()))
        return frame.filter(~target), [
            Expected("missing_session", len(contracts), (), self.severity),
        ]


# --------------------------------------------------------------------------- #
# broken values
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SwapHighLow(_BarFault):
    """Exchange `high` and `low`, producing the `high < low` clause that the real corpus
    never exhibits (0 of the 43 genuine violations) and that therefore needs a fixture."""

    rows: Sequence[int]

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask(self.rows)
        updated = frame.with_columns(
            pl.when(selected).then(pl.col(C.LOW)).otherwise(pl.col(C.HIGH)).alias(C.HIGH),
            pl.when(selected).then(pl.col(C.HIGH)).otherwise(pl.col(C.LOW)).alias(C.LOW),
        )
        return updated, [
            Expected("invalid_ohlc", len(self.rows), tuple(sorted(self.rows)), Severity.ERROR),
        ]


@dataclass(frozen=True)
class CloseOutsideRange(_BarFault):
    """Push `close` above `high` — the `high < max(open, close)` clause, which accounts
    for 24 of the 43 genuine violations in the real daily data."""

    rows: Sequence[int]
    offset: float = 5.0

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask(self.rows)
        updated = frame.with_columns(
            pl.when(selected)
            .then(pl.col(C.HIGH) + self.offset)
            .otherwise(pl.col(C.CLOSE))
            .alias(C.CLOSE)
        )
        return updated, [
            Expected("invalid_ohlc", len(self.rows), tuple(sorted(self.rows)), Severity.ERROR),
        ]


@dataclass(frozen=True)
class CarriedForwardSettlement(_BarFault):
    """Reproduce the corpus's 39-of-43 signature: `open == high == low`, `close` moved on,
    `volume == 0`.

    On a non-trading day the vendor carried OHL forward and updated `close` to the new
    settlement, leaving the settlement outside its own bar's range. Because volume is
    zero the session reads as DORMANT, so the engine must **downgrade** this to WARNING
    — that downgrade is the difference between a usable report and 43 false alarms.
    """

    rows: Sequence[int]
    settlement_offset: float = 3.5

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask(self.rows)
        carried = pl.col(C.OPEN)
        updated = frame.with_columns(
            pl.when(selected).then(carried).otherwise(pl.col(C.HIGH)).alias(C.HIGH),
            pl.when(selected).then(carried).otherwise(pl.col(C.LOW)).alias(C.LOW),
            pl.when(selected)
            .then(carried + self.settlement_offset)
            .otherwise(pl.col(C.CLOSE))
            .alias(C.CLOSE),
            pl.when(selected).then(pl.lit(0, pl.Int64)).otherwise(pl.col(C.VOLUME)).alias(C.VOLUME),
        )
        ids = tuple(sorted(self.rows))
        return updated, [
            Expected("invalid_ohlc", len(ids), ids, Severity.WARNING),
        ]


@dataclass(frozen=True)
class NegativePrice(_BarFault):
    """Drive one price field below zero.

    `low` by default: no other check reads `low` in isolation, so the fault stays
    surgical. (`close` would also trip `invalid_ohlc` and `outlier_return`.)
    """

    rows: Sequence[int]
    value: float = -1.0
    column: str = C.LOW

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask(self.rows)
        updated = frame.with_columns(
            pl.when(selected)
            .then(pl.lit(self.value))
            .otherwise(pl.col(self.column))
            .alias(self.column)
        )
        return updated, [
            Expected(
                "non_positive_price", len(self.rows), tuple(sorted(self.rows)), Severity.ERROR
            ),
        ]


@dataclass(frozen=True)
class NegativeVolume(_BarFault):
    """Set `volume` below zero — never a market event, always a defect."""

    rows: Sequence[int]
    value: int = -5

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask(self.rows)
        updated = frame.with_columns(
            pl.when(selected)
            .then(pl.lit(self.value, pl.Int64))
            .otherwise(pl.col(C.VOLUME))
            .alias(C.VOLUME)
        )
        return updated, [
            Expected("negative_volume", len(self.rows), tuple(sorted(self.rows)), Severity.ERROR),
        ]


@dataclass(frozen=True)
class NullField(_BarFault):
    """Null one of open/high/low/close/volume, as an unparseable cell would arrive.

    Ingestion keeps such rows deliberately — they are locatable, so the user should see
    them in context — which is exactly what makes them this check's business.
    """

    column: str
    rows: Sequence[int]

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask(self.rows)
        dtype = BAR_SCHEMA[self.column]
        updated = frame.with_columns(
            pl.when(selected)
            .then(pl.lit(None, dtype))
            .otherwise(pl.col(self.column))
            .alias(self.column)
        )
        return updated, [
            Expected("missing_value", len(self.rows), tuple(sorted(self.rows)), Severity.ERROR),
        ]


@dataclass(frozen=True)
class ZeroVolumeWithRange(_BarFault):
    """Zero the volume while leaving a price range — a contradiction with no benign
    reading in an active session."""

    rows: Sequence[int]
    severity: Severity = Severity.WARNING

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask(self.rows)
        updated = frame.with_columns(
            pl.when(selected).then(pl.lit(0, pl.Int64)).otherwise(pl.col(C.VOLUME)).alias(C.VOLUME)
        )
        return updated, [
            Expected(
                "zero_volume_with_range", len(self.rows), tuple(sorted(self.rows)), self.severity
            ),
        ]


@dataclass(frozen=True)
class FlatZeroVolumeBar(_BarFault):
    """Collapse a bar to `open == high == low == close` with zero volume — the shape of
    all 14,152 no-range bars in the corpus."""

    rows: Sequence[int]
    severity: Severity = Severity.WARNING

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask(self.rows)
        flat = pl.col(C.OPEN)
        updated = frame.with_columns(
            *(
                pl.when(selected).then(flat).otherwise(pl.col(col)).alias(col)
                for col in (C.HIGH, C.LOW, C.CLOSE)
            ),
            pl.when(selected).then(pl.lit(0, pl.Int64)).otherwise(pl.col(C.VOLUME)).alias(C.VOLUME),
        )
        return updated, [
            Expected("stale_bar", len(self.rows), tuple(sorted(self.rows)), self.severity),
        ]


# --------------------------------------------------------------------------- #
# broken time
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OffGridSeconds(_BarFault):
    """Nudge minute bars off the minute boundary, as a mislabelled tick feed would."""

    rows: Sequence[int]
    seconds: int = 30

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask(self.rows)
        shift = pl.duration(seconds=self.seconds)
        updated = frame.with_columns(
            pl.when(selected).then(pl.col(C.TS_UTC) + shift).otherwise(pl.col(C.TS_UTC)),
            pl.when(selected).then(pl.col(C.TS_LOCAL) + shift).otherwise(pl.col(C.TS_LOCAL)),
        )
        return updated, [
            Expected(
                "off_grid_timestamp", len(self.rows), tuple(sorted(self.rows)), Severity.WARNING
            ),
        ]


@dataclass(frozen=True)
class FutureTimestamp(_BarFault):
    """Move bars far beyond `now`, as a mis-scaled epoch would land them.

    Keep the selection short and non-contiguous: removing more than `max_gap_minutes`
    consecutive bars from their session would open a genuine gap as well, and the fault
    would stop being surgical.
    """

    rows: Sequence[int]
    years: int = 40

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask(self.rows)
        shift = pl.duration(days=365 * self.years)
        updated = frame.with_columns(
            pl.when(selected).then(pl.col(C.TS_UTC) + shift).otherwise(pl.col(C.TS_UTC)),
            pl.when(selected).then(pl.col(C.TS_LOCAL) + shift).otherwise(pl.col(C.TS_LOCAL)),
            pl.when(selected)
            .then(pl.col(C.SESSION_DATE) + pl.duration(days=365 * self.years))
            .otherwise(pl.col(C.SESSION_DATE))
            .alias(C.SESSION_DATE),
        )
        return updated, [
            Expected(
                "timestamp_out_of_range",
                len(self.rows),
                tuple(sorted(self.rows)),
                Severity.WARNING,
            ),
        ]


@dataclass(frozen=True)
class AmbiguousLocalTime:
    """Append bars inside the repeated fall-back hour (01:00–01:59 Chicago).

    `synth` refuses to build these — it localises through the real code path and raises —
    so they have to be constructed here. With `passes=2` the same wall clocks arrive
    twice, which under `ambiguous="earliest"` gives both copies the *same* `ts_utc`: they
    therefore trip `duplicate_timestamp` as well, and that is the correct reading. The
    vendor's wall clock genuinely does not distinguish the two passes.

    The appended session is tiny, so the regime model reads it as DORMANT and no
    coverage check fires on it.
    """

    on: date_type = date_type(2025, 11, 2)
    bars: int = 2
    passes: int = 1
    contract: str | None = None

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        if frame.height == 0:
            raise ValueError("AmbiguousLocalTime needs a non-empty frame to copy metadata from")
        template = frame.row(0, named=True)
        contract = self.contract or template[C.CONTRACT]
        next_id = _next_row_id(frame)
        rows: list[dict[str, Any]] = []
        for pass_index in range(self.passes):
            for minute in range(self.bars):
                wall = datetime.combine(self.on, datetime.min.time()) + timedelta(
                    hours=1, minutes=minute
                )
                rows.append(
                    {
                        C.ROW_ID: next_id + pass_index * self.bars + minute,
                        C.CONTRACT: contract,
                        C.EXCHANGE: template[C.EXCHANGE],
                        C.ROOT: template[C.ROOT],
                        C.TS_LOCAL: wall,
                        C.SESSION_DATE: self.on,
                        C.OPEN: 100.0,
                        C.HIGH: 100.0,
                        C.LOW: 100.0,
                        C.CLOSE: 100.0,
                        C.VOLUME: 10,
                        C.OPEN_INTEREST: None,
                    }
                )
        appended = (
            pl.DataFrame(rows, schema={**dict(BAR_SCHEMA), C.TS_UTC: pl.Datetime("us")})
            .with_columns(
                pl.col(C.TS_LOCAL)
                .dt.replace_time_zone(CHICAGO, ambiguous="earliest", non_existent="null")
                .dt.convert_time_zone("UTC")
                .alias(C.TS_UTC)
            )
            .select(BAR_SCHEMA.names())
            .cast(dict(BAR_SCHEMA))
        )
        ids = tuple(appended.get_column(C.ROW_ID).to_list())
        expected = [
            Expected("ambiguous_local_time", len(ids), ids, Severity.WARNING),
            # These bars are flat by construction — one price repeated across OHLC, because
            # the fault cares about the wall clock and not the prices — so the vendor's own
            # no-range predicate sees them and `stale_bar` reports them. INFO, not WARNING:
            # volume is non-zero, so `untraded` is false and the regime cannot promote it.
            #
            # Declared late. `scripts/measure_metrics.py` measured it as a false positive
            # and the check turned out to be right: this list was short. It was invisible
            # because `assert_findings_match` — the only assertion that forbids an
            # *undeclared* check — is never run against this fault, so the finding had been
            # appearing inside a passing test. See `docs/EVALUATION.md`.
            Expected("stale_bar", len(ids), ids, Severity.INFO),
        ]
        if self.passes > 1:
            expected.append(Expected("duplicate_timestamp", len(ids), ids, Severity.INFO))
        return pl.concat([frame, appended], how="vertical"), expected

    def rejects(self) -> list[dict[str, Any]]:
        return []


@dataclass(frozen=True)
class NonexistentLocalTime(_BarFault):
    """A wall clock inside the spring-forward gap — 02:30 on 2026-03-08 never happened.

    Such a row can never *be* a bar: there is no instant for it. Ingestion rejects it
    with `NONEXISTENT_LOCAL_TIME`, so it reaches the engine through the rejects frame and
    surfaces as `malformed_record`.
    """

    on: date_type = date_type(2026, 3, 8)
    row_id: int = 900_000
    contract: str = "ESH26"

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        return frame, [
            Expected("malformed_record", 1, (self.row_id,), Severity.ERROR),
        ]

    def rejects(self) -> list[dict[str, Any]]:
        return [
            {
                C.ROW_ID: self.row_id,
                C.CONTRACT: self.contract,
                C.REASON: RejectReason.NONEXISTENT_LOCAL_TIME.value,
                "raw_values": f"{self.on} 02:30:00",
                "source": "<fault>",
            }
        ]


@dataclass(frozen=True)
class MalformedLine(_BarFault):
    """A CSV line that never parsed into fields at all."""

    row_ids: Sequence[int] = (900_100,)
    contract: str | None = None
    raw: str = "ESH26,2026-03-03 17:00:00,5000.0,5001.0"

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        ids = tuple(sorted(self.row_ids))
        return frame, [
            Expected("malformed_record", len(ids), ids, Severity.ERROR),
        ]

    def rejects(self) -> list[dict[str, Any]]:
        return [
            {
                C.ROW_ID: row_id,
                C.CONTRACT: self.contract,
                C.REASON: RejectReason.MALFORMED_LINE.value,
                "raw_values": self.raw,
                "source": "<fault>",
            }
            for row_id in self.row_ids
        ]


@dataclass(frozen=True)
class PriceSpike(_BarFault):
    """Multiply one bar's prices by `factor`, keeping the bar internally coherent.

    Two outliers follow, not one: the move into the spike and the move back out. Both are
    declared, because a harness that quietly expected only one would let a check that
    misses the return leg pass.
    """

    row: int
    factor: float = 1.5
    also_row_after: bool = True

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        selected = _row_mask([self.row])
        updated = frame.with_columns(
            *(
                pl.when(selected).then(pl.col(col) * self.factor).otherwise(pl.col(col)).alias(col)
                for col in C.PRICES
            )
        )
        ids = (self.row, self.row + 1) if self.also_row_after else (self.row,)
        return updated, [
            Expected("outlier_return", len(ids), ids, Severity.INFO),
        ]


@dataclass(frozen=True)
class ShiftWallClockToUtc(_BarFault):
    """The trap: re-derive `ts_utc` by *reading the Chicago wall clock as UTC*.

    This is the single mistake the whole time layer exists to prevent, and the point of
    the fault is what it does **not** produce: the frame stays internally consistent, so
    every row-level check still reports nothing. Only reconciliation against another
    frequency (WP3's session aggregation versus the vendor daily file) can see it. The
    harness records the shift so a test can assert that `session_date` really did move.
    """

    def apply(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, list[Expected]]:
        shifted = frame.with_columns(
            pl.col(C.TS_LOCAL).dt.replace_time_zone("UTC").alias(C.TS_UTC)
        ).with_columns(
            pl.col(C.TS_UTC)
            .dt.convert_time_zone(CHICAGO)
            .dt.replace_time_zone(None)
            .alias(C.TS_LOCAL)
        )
        return shifted.with_columns(
            pl.when(pl.col(C.TS_LOCAL).dt.hour() >= 17)
            .then(pl.col(C.TS_LOCAL).dt.date().dt.offset_by("1d"))
            .otherwise(pl.col(C.TS_LOCAL).dt.date())
            .alias(C.SESSION_DATE)
        ), []


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def inject(frame: pl.DataFrame, *faults: Fault) -> tuple[pl.DataFrame, list[Expected]]:
    """Apply `faults` in order and return the corrupted frame with its expectations."""
    corrupted, _, expected = inject_with_rejects(frame, *faults)
    return corrupted, expected


def inject_with_rejects(
    frame: pl.DataFrame, *faults: Fault
) -> tuple[pl.DataFrame, pl.DataFrame, list[Expected]]:
    """As `inject`, plus the rejects frame for faults that stop a row becoming a bar."""
    current = frame
    expected: list[Expected] = []
    reject_rows: list[dict[str, Any]] = []
    for fault in faults:
        current, produced = fault.apply(current)
        expected.extend(produced)
        reject_rows.extend(fault.rejects())
    ordered = current.sort(C.ROW_ID).select(BAR_SCHEMA.names()).cast(dict(BAR_SCHEMA))
    return ordered, rejects_frame(reject_rows), expected


def merge_contracts(*frames: pl.DataFrame) -> pl.DataFrame:
    """Stack per-contract synthetic frames into one dataset with unique `row_id`s.

    `synth` numbers each contract from zero, so a naive concat would give two contracts
    the same ids and make every finding ambiguous. Renumbering here keeps `row_id` what
    it claims to be: a unique handle on one input row.
    """
    stacked = pl.concat(frames, how="vertical").sort(C.CONTRACT, C.TS_UTC)
    return (
        stacked.drop(C.ROW_ID)
        .with_row_index(C.ROW_ID)
        .select(BAR_SCHEMA.names())
        .cast(dict(BAR_SCHEMA))
    )


def rejects_frame(rows: Sequence[dict[str, Any]] = ()) -> pl.DataFrame:
    """A frame in `REJECTS_SCHEMA`, the shape `IngestResult.rejects` is documented to have."""
    return pl.DataFrame(list(rows), schema=REJECTS_SCHEMA)


def no_rejects() -> pl.DataFrame:
    """An empty rejects frame — what a clean file produces."""
    return rejects_frame()


def assert_findings_match(report: QualityReport, expected: Sequence[Expected]) -> None:
    """Assert the report contains exactly `expected` — recall *and* precision.

    Three assertions, in the order that gives the most useful failure message:
    no unexpected check fired; every expected check fired with the right total `count`;
    every expected `row_id` appears in that check's evidence.
    """
    actual_ids = set(report.check_ids())
    wanted_ids = {e.check_id for e in expected}
    unexpected = sorted(actual_ids - wanted_ids)
    assert not unexpected, (
        f"unexpected findings from {unexpected}: {report.filter(check_id=unexpected).to_dicts()}"
    )
    missing = sorted(wanted_ids - actual_ids)
    assert not missing, f"expected findings from {missing}, got only {sorted(actual_ids)}"

    # Several faults may target the same check (two kinds of reject, two duplicate
    # shapes); their expectations combine into one statement about that check.
    for check_id in sorted(wanted_ids):
        wants = [e for e in expected if e.check_id == check_id]
        subset = report.filter(check_id=check_id)
        want_count = sum(e.count for e in wants)
        total = int(subset.findings.get_column(C.COUNT).sum())
        assert total == want_count, (
            f"{check_id}: expected count {want_count}, got {total} ({subset.to_dicts()})"
        )
        want_severities = {e.severity.label for e in wants if e.severity is not None}
        if want_severities:
            severities = set(subset.findings.get_column(C.SEVERITY).to_list())
            assert severities == want_severities, (
                f"{check_id}: expected severities {sorted(want_severities)}, "
                f"got {sorted(severities)}"
            )
        want_rows = {row_id for e in wants for row_id in e.row_ids}
        if want_rows:
            found = set(subset.rows_affected(Severity.INFO))
            assert want_rows <= found, (
                f"{check_id}: evidence is missing row_ids "
                f"{sorted(want_rows - found)} (found {sorted(found)})"
            )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _row_mask(rows: Sequence[int]) -> pl.Expr:
    return pl.col(C.ROW_ID).is_in(list(rows))


def _pick(frame: pl.DataFrame, rows: Sequence[int] | None, n: int) -> pl.DataFrame:
    if rows is not None:
        picked = frame.filter(_row_mask(rows))
        if picked.height != len(list(rows)):
            raise ValueError(f"row_ids {list(rows)} are not all present in the frame")
        return picked
    return frame.sort(C.ROW_ID).head(n)


def _renumber(rows: pl.DataFrame, start: int) -> pl.DataFrame:
    return rows.with_columns((pl.int_range(pl.len(), dtype=pl.UInt32) + start).alias(C.ROW_ID))


def _next_row_id(frame: pl.DataFrame) -> int:
    if frame.height == 0:
        return 0
    return int(frame.get_column(C.ROW_ID).max() or 0) + 1


def _ids(frame: pl.DataFrame) -> tuple[int, ...]:
    return tuple(int(i) for i in frame.get_column(C.ROW_ID).to_list())


def _bracketing_rows(original: pl.DataFrame, dropped: pl.DataFrame) -> tuple[int, ...]:
    """The two surviving bars either side of a deleted range, by `ts_utc`."""
    first = dropped.get_column(C.TS_UTC).min()
    last = dropped.get_column(C.TS_UTC).max()
    before = original.filter(pl.col(C.TS_UTC) < first).sort(C.TS_UTC).tail(1)
    after = original.filter(pl.col(C.TS_UTC) > last).sort(C.TS_UTC).head(1)
    return tuple(sorted(_ids(before) + _ids(after)))


#: Every fault this harness ships, so a test can assert the catalogue stays complete.
ALL_FAULTS: tuple[type, ...] = (
    AmbiguousLocalTime,
    CarriedForwardSettlement,
    CloseOutsideRange,
    ConflictingDuplicate,
    DropRange,
    DropSession,
    ExactDuplicate,
    FlatZeroVolumeBar,
    FutureTimestamp,
    MalformedLine,
    NegativePrice,
    NegativeVolume,
    NonexistentLocalTime,
    NullField,
    OffGridSeconds,
    PriceSpike,
    ShiftWallClockToUtc,
    SwapHighLow,
    ZeroVolumeWithRange,
)
