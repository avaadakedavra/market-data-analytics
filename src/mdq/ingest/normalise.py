"""Raw columns → the canonical `BarFrame`, with a reason for everything dropped.

The governing rule is **ingestion never silently fixes data**:

* A row is *rejected* only when it cannot be **located** — no contract, or no usable
  instant. Those rows are unplaceable on the canonical timeline, so keeping them would
  mean inventing a position for them.
* A row with a null, unparseable or nonsensical **price or volume** is **kept**. It has
  a place on the timeline and the business user needs to see it in context; the quality
  engine flags it (`missing_value`, `non_positive_price`, …).
* **Duplicates are kept.** Classifying a duplicate as exact or conflicting is a quality
  judgement, and removing the safe ones is `mdq.quality.cleanse`'s decision, not
  ingestion's.
* Coercion is always `strict=False`, so `"1,234.50"` becomes a null that is visible and
  attributable — never the number `1234.50`, and never `1`.

Output is sorted by `(contract, ts_utc, row_id)`. `row_id` is the row's position in the
source, which is what lets a finding point back at a line of the original file.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import polars as pl

from mdq.domain.config import QualityConfig
from mdq.domain.findings import RejectReason
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, BarFrame, C, empty_bar_lf
from mdq.ingest.errors import MixedFrequencyError, TooManyMalformedLinesError
from mdq.ingest.profiles import TIMESTAMP, SourceProfile, TimestampKind, detect_profile
from mdq.ingest.readers import MALFORMED_LINE_COLUMN, READERS, ReadSource
from mdq.ingest.report import RAW_VALUES, SOURCE, IngestStats, empty_reject_df
from mdq.time.localise import CHICAGO, localise_wall_clock
from mdq.time.sessions import CALENDAR_SESSION, DEFAULT_SESSIONS, SessionProfile, session_date_expr

__all__ = [
    "IngestOptions",
    "IngestResult",
    "ingest_file",
    "normalise",
    "read_bars",
]

#: Internal column names, prefixed so they can never collide with a source column.
_ROW_ID: Final = "__mdq_row_id"
_WALL: Final = "__mdq_wall"

#: Used only when a file is empty and nothing — profile or caller — declares otherwise.
DEFAULT_FREQUENCY: Final = Frequency.MINUTE


@dataclass(frozen=True)
class IngestOptions:
    """Everything a caller may declare about a file that its columns cannot say.

    Attributes:
        source: Provenance written into the `BarFrame` and every reject.
        timestamp_kind: Override the profile's declared kind. This is how a generic
            CSV says "my timestamps are UTC ISO strings" — the platform never guesses
            a time zone.
        frequency: Declare the frequency instead of inferring it. Also the escape
            hatch for a file that inference would call mixed.
        timezone: The wall clock's zone, for wall-clock timestamp kinds.
        max_reject_ratio: Abort when more than this share of the file's lines are
            malformed. Defaults to the configured quality threshold.
        default_exchange: Exchange for sources that do not carry one; it selects the
            session profile, so it changes `session_date`.
        sessions: Per-exchange session table; defaults to `DEFAULT_SESSIONS`.
    """

    source: str | None = None
    timestamp_kind: TimestampKind | None = None
    frequency: Frequency | None = None
    timezone: str = CHICAGO
    max_reject_ratio: float = QualityConfig().max_reject_ratio
    default_exchange: str | None = None
    sessions: Mapping[str, SessionProfile] | None = None

    @property
    def session_table(self) -> Mapping[str, SessionProfile]:
        """The session table to use."""
        return DEFAULT_SESSIONS if self.sessions is None else self.sessions


@dataclass(frozen=True)
class IngestResult:
    """The whole outcome of reading one file.

    Attributes:
        bars: Every placeable row, sorted and schema-valid. **Not** deduplicated.
        rejects: One row per unplaceable input row, in `REJECT_SCHEMA`.
        stats: Counts and provenance for the report.
    """

    bars: BarFrame
    rejects: pl.DataFrame
    stats: IngestStats

    @property
    def is_clean(self) -> bool:
        """True when every input row became a bar."""
        return self.rejects.height == 0


def normalise(
    raw: pl.LazyFrame,
    profile: SourceProfile,
    options: IngestOptions | None = None,
) -> IngestResult:
    """Map a raw frame onto `BAR_SCHEMA` under `profile`.

    Args:
        raw: A reader's output — source columns, untouched.
        profile: The layout declaration, including what the timestamps *mean*.
        options: Caller declarations; defaults are safe for the vendor's files.

    Returns:
        `IngestResult`. An empty input yields an empty `BarFrame` with the correct
        schema and zero rejects; a file missing a required column yields an empty
        `BarFrame` and a single file-level `MISSING_REQUIRED_COLUMN` reject.

    Raises:
        MixedFrequencyError: if the file interleaves daily and intraday bars and the
            caller did not declare a frequency.
        TooManyMalformedLinesError: if more than `options.max_reject_ratio` of the
            lines could not be parsed at all.
    """
    opts = options or IngestOptions()
    source = opts.source or "<memory>"
    schema = raw.collect_schema()
    columns = [name for name in schema.names() if name != MALFORMED_LINE_COLUMN]

    if not schema.names():
        return _empty_result(profile, opts, source, rows_in=0)

    missing = profile.missing(columns)
    if missing:
        return _missing_column_result(raw, profile, opts, source, missing, columns)

    frame = raw.collect().with_row_index(name=_ROW_ID)
    rows_in = frame.height
    original = frame.select(_ROW_ID, *columns)
    rejects: list[pl.DataFrame] = []

    resolved = profile.resolve(columns)
    declared = profile.resolve_timestamp(columns)
    kind: TimestampKind = opts.timestamp_kind or (
        declared.kind if declared is not None else profile.timestamp_kind
    )

    frame = _take_malformed(frame, rows_in, opts, rejects)
    frame = _take_null_contracts(frame, resolved[C.CONTRACT], rejects)
    frame = _localise(frame, resolved[TIMESTAMP], kind, opts, rejects)

    frequency = opts.frequency or profile.frequency_hint or _infer_frequency(frame)
    bars = _finalise(frame, resolved, frequency, opts)

    return IngestResult(
        bars=BarFrame(bars.lazy(), frequency, source),
        rejects=_reject_frame(original, rejects, columns, source, resolved.get(C.CONTRACT)),
        stats=_stats(bars, rejects, profile, frequency, source, rows_in),
    )


def ingest_file(
    path: str | Path,
    options: IngestOptions | None = None,
    profile: SourceProfile | None = None,
) -> IngestResult:
    """Read and normalise one file, choosing reader and profile automatically.

    Raises:
        UnsupportedFormatError: if no reader claims the file's extension.
    """
    raw = READERS.for_path(path).read(path)
    opts = options or IngestOptions()
    if opts.source is None:
        opts = _with_source(opts, str(path))
    chosen = profile or detect_profile(
        [name for name in raw.collect_schema().names() if name != MALFORMED_LINE_COLUMN]
    )
    return normalise(raw, chosen, opts)


def read_bars(path: str | Path, options: IngestOptions | None = None) -> BarFrame:
    """The bars of one file, dispatching on its extension.

    Rejects and statistics are discarded; use `ingest_file` when they matter.

    Raises:
        UnsupportedFormatError: if no reader claims the file's extension.
    """
    return ingest_file(path, options).bars


def read_buffer(
    source: ReadSource,
    extension: str,
    options: IngestOptions | None = None,
) -> IngestResult:
    """Ingest an in-memory upload whose format is given by `extension`.

    Raises:
        UnsupportedFormatError: if no reader claims `extension`.
    """
    raw = READERS.get(extension).read(source)
    opts = options or IngestOptions()
    profile = detect_profile(
        [name for name in raw.collect_schema().names() if name != MALFORMED_LINE_COLUMN]
    )
    return normalise(raw, profile, opts)


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #
def _with_source(options: IngestOptions, source: str) -> IngestOptions:
    return IngestOptions(
        source=source,
        timestamp_kind=options.timestamp_kind,
        frequency=options.frequency,
        timezone=options.timezone,
        max_reject_ratio=options.max_reject_ratio,
        default_exchange=options.default_exchange,
        sessions=options.sessions,
    )


def _take_malformed(
    frame: pl.DataFrame,
    rows_in: int,
    options: IngestOptions,
    rejects: list[pl.DataFrame],
) -> pl.DataFrame:
    """Split off lines the reader could not parse, and refuse a file that is mostly bad.

    The bound lives here rather than in the reader because the threshold is a policy
    (`IngestOptions`), and applying it here gives every present and future reader the
    same protection for free.
    """
    if MALFORMED_LINE_COLUMN not in frame.columns:
        return frame
    bad = frame.filter(pl.col(MALFORMED_LINE_COLUMN).is_not_null())
    if bad.height and rows_in and bad.height / rows_in > options.max_reject_ratio:
        raise TooManyMalformedLinesError(
            f"{bad.height} of {rows_in} lines are malformed "
            f"({bad.height / rows_in:.1%} > {options.max_reject_ratio:.1%}); "
            "the file does not look like the format it claims to be"
        )
    if bad.height:
        rejects.append(_reason_rows(bad, RejectReason.MALFORMED_LINE))
    return frame.filter(pl.col(MALFORMED_LINE_COLUMN).is_null()).drop(MALFORMED_LINE_COLUMN)


def _take_null_contracts(
    frame: pl.DataFrame,
    column: str,
    rejects: list[pl.DataFrame],
) -> pl.DataFrame:
    """Derive `contract`, rejecting rows that have none.

    A blank string is a missing contract, not a contract called "". Without a contract
    a bar belongs to no instrument and cannot be placed at all.
    """
    contract = pl.col(column).cast(pl.String, strict=False).str.strip_chars()
    frame = frame.with_columns(
        pl.when(contract.str.len_chars() > 0).then(contract).otherwise(None).alias(C.CONTRACT)
    )
    bad = frame.filter(pl.col(C.CONTRACT).is_null())
    if bad.height:
        rejects.append(_reason_rows(bad, RejectReason.NULL_CONTRACT))
    return frame.filter(pl.col(C.CONTRACT).is_not_null())


def _localise(
    frame: pl.DataFrame,
    column: str,
    kind: TimestampKind,
    options: IngestOptions,
    rejects: list[pl.DataFrame],
) -> pl.DataFrame:
    """Produce `ts_utc` / `ts_local`, rejecting rows whose instant cannot be resolved.

    Which branch runs is decided by the *declared* kind, never by inspecting values.
    """
    dtype = frame.schema[column]

    if kind in ("chicago_wall_naive", "epoch_ms_chicago_wall"):
        walled = _with_wall(frame, column, dtype, kind, options.timezone)
        kept, rejected = localise_wall_clock(walled.lazy(), _WALL, tz=options.timezone)
        bad = rejected.collect()
        if bad.height:
            rejects.append(bad.select(pl.col(_ROW_ID), pl.col(C.REASON)))
        return kept.drop(_WALL).collect()

    if kind == "date":
        # A date is a *label*: midnight UTC, and the local reading is the same date.
        instant = _date_expr(column, dtype).cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
    elif kind in ("iso_utc", "epoch_ms_utc"):
        instant = (
            _utc_expr(column, dtype)
            if kind == "iso_utc"
            else _epoch_expr(column, dtype).dt.replace_time_zone("UTC")
        )
    else:  # pragma: no cover - `kind` is a closed Literal
        raise ValueError(f"unsupported timestamp kind {kind!r}")

    frame = frame.with_columns(instant.alias(C.TS_UTC))
    local = (
        pl.col(C.TS_UTC).dt.replace_time_zone(None)
        if kind == "date"
        else pl.col(C.TS_UTC).dt.convert_time_zone(options.timezone).dt.replace_time_zone(None)
    )
    frame = frame.with_columns(local.alias(C.TS_LOCAL))
    bad = frame.filter(pl.col(C.TS_UTC).is_null())
    if bad.height:
        rejects.append(_reason_rows(bad, RejectReason.UNPARSEABLE_TIMESTAMP))
    return frame.filter(pl.col(C.TS_UTC).is_not_null())


def _finalise(
    frame: pl.DataFrame,
    resolved: Mapping[str, str],
    frequency: Frequency,
    options: IngestOptions,
) -> pl.DataFrame:
    """Cast every canonical column into `BAR_SCHEMA` and sort."""
    exchange = (
        pl.col(resolved[C.EXCHANGE]).cast(pl.String, strict=False)
        if C.EXCHANGE in resolved
        else pl.lit(options.default_exchange, dtype=pl.String)
    )
    frame = frame.with_columns(exchange.alias(C.EXCHANGE))
    frame = frame.with_columns(_root_expr(resolved).alias(C.ROOT))
    frame = frame.with_columns(_session_date_expr(frequency, options.session_table))
    if frequency is Frequency.DAILY:
        # A daily bar's instant is a *label*, not a trading time. Pinning it to midnight
        # UTC on its own session date is what makes daily frames from different sources
        # — a `date` column, an epoch, an ISO string — line up with each other.
        label = pl.col(C.SESSION_DATE).cast(pl.Datetime("us"))
        frame = frame.with_columns(
            label.dt.replace_time_zone("UTC").alias(C.TS_UTC),
            label.alias(C.TS_LOCAL),
        )
    frame = frame.with_columns(
        *[_float_expr(resolved, name) for name in C.PRICES],
        _int_expr(frame, resolved, C.VOLUME),
        _int_expr(frame, resolved, C.OPEN_INTEREST),
    )
    return (
        # A source column literally called `row_id` would collide with ours; the
        # canonical one — the position in the file — is the one that must survive.
        frame.drop(C.ROW_ID, strict=False)
        .rename({_ROW_ID: C.ROW_ID})
        .select(BAR_SCHEMA.names())
        .cast(dict(BAR_SCHEMA))  # type: ignore[arg-type]
        .sort([C.CONTRACT, C.TS_UTC, C.ROW_ID])
    )


# --------------------------------------------------------------------------- #
# expressions
# --------------------------------------------------------------------------- #
def _with_wall(
    frame: pl.DataFrame,
    column: str,
    dtype: pl.DataType,
    kind: TimestampKind,
    tz: str,
) -> pl.DataFrame:
    """Attach a naive wall-clock column, whatever the source threw at us.

    Two awkward inputs are handled here rather than left to explode:

    * a column in which *nothing* parses as a timestamp — every row then becomes an
      `UNPARSEABLE_TIMESTAMP` reject, which is a report rather than a crash;
    * strings that turn out to carry an offset (`...T15:00:00Z`) even though the
      profile declared a wall clock. A naive parse cannot express an offset, so polars
      returns nulls for the whole column; the source has told us the instant, so we
      re-read it as an instant and take the wall clock off that, rather than either
      discarding the offset or throwing the file away.
    """
    try:
        out = frame.with_columns(_wall_expr(column, dtype, kind).alias(_WALL))
    except pl.exceptions.PolarsError:
        out = frame.with_columns(pl.lit(None, dtype=pl.Datetime("us")).alias(_WALL))
    if dtype == pl.String and _wholly_unparsed(out, column):
        with contextlib.suppress(pl.exceptions.PolarsError):
            out = frame.with_columns(_utc_expr(column, dtype).alias(_WALL))
    parsed = out.schema[_WALL]
    if isinstance(parsed, pl.Datetime) and parsed.time_zone is not None:
        out = out.with_columns(
            pl.col(_WALL).dt.convert_time_zone(tz).dt.replace_time_zone(None).alias(_WALL)
        )
    return out


def _wholly_unparsed(frame: pl.DataFrame, column: str) -> bool:
    """True when nothing parsed, even though the source column holds values."""
    if frame.height == 0:
        return False
    return (
        frame.get_column(_WALL).null_count() == frame.height
        and frame.get_column(column).null_count() < frame.height
    )


def _wall_expr(column: str, dtype: pl.DataType, kind: TimestampKind) -> pl.Expr:
    """A naive wall-clock datetime, however the source spelled it."""
    if kind == "epoch_ms_chicago_wall":
        return _epoch_expr(column, dtype)
    col = pl.col(column)
    if dtype == pl.String:
        return col.str.to_datetime(time_unit="us", strict=False)
    if isinstance(dtype, pl.Date):
        return col.cast(pl.Datetime("us"))
    if isinstance(dtype, pl.Datetime) and dtype.time_zone is not None:
        # A source that declares a zone has already told us the instant; honour it.
        return col.dt.convert_time_zone(CHICAGO).dt.replace_time_zone(None)
    return col.cast(pl.Datetime("us"), strict=False)


def _epoch_expr(column: str, dtype: pl.DataType) -> pl.Expr:
    """Milliseconds since the epoch → a naive datetime.

    Negative values are ordinary: a daily bar labelled before 1970 is perfectly valid.
    """
    col = pl.col(column)
    if isinstance(dtype, pl.Datetime):
        return col.cast(pl.Datetime("us"))
    return pl.from_epoch(col.cast(pl.Int64, strict=False), time_unit="ms").cast(pl.Datetime("us"))


def _utc_expr(column: str, dtype: pl.DataType) -> pl.Expr:
    """An instant that already carries its offset → `Datetime("us", "UTC")`."""
    col = pl.col(column)
    if isinstance(dtype, pl.Datetime):
        if dtype.time_zone is None:
            return col.cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
        return col.cast(pl.Datetime("us", dtype.time_zone)).dt.convert_time_zone("UTC")
    return col.cast(pl.String, strict=False).str.to_datetime(
        time_unit="us", time_zone="UTC", strict=False
    )


def _date_expr(column: str, dtype: pl.DataType) -> pl.Expr:
    """A calendar date, however the source spelled it."""
    col = pl.col(column)
    if isinstance(dtype, pl.Date):
        return col
    if isinstance(dtype, pl.Datetime):
        return col.dt.date()
    return col.cast(pl.String, strict=False).str.to_date(strict=False)


def _root_expr(resolved: Mapping[str, str]) -> pl.Expr:
    """The product root: the source's, or the contract code minus its expiry.

    Stripping the month code (`ESH26` → `ES`, `SR3H26` → `SR3`) rather than taking the
    leading letters is what keeps numeric roots such as `SR3` intact.
    """
    derived = pl.col(C.CONTRACT).str.replace(r"(?i)[FGHJKMNQUVXZ]\d{1,2}$", "")
    derived = pl.when(derived.str.len_chars() > 0).then(derived).otherwise(pl.col(C.CONTRACT))
    if C.ROOT not in resolved:
        return derived
    given = pl.col(resolved[C.ROOT]).cast(pl.String, strict=False).str.strip_chars()
    return pl.when(given.str.len_chars() > 0).then(given).otherwise(derived)


def _session_date_expr(
    frequency: Frequency,
    sessions: Mapping[str, SessionProfile],
) -> pl.Expr:
    """`session_date`, chosen per row from the bar's exchange.

    A daily bar's timestamp is a date *label*, so its session is that date and nothing
    rolls; the label is read off `ts_utc`, which is where every source's declared date
    ends up regardless of how it spelled it. Intraday bars roll according to their
    venue's profile, with the calendar date as the fallback for venues we have not
    verified.
    """
    if frequency is Frequency.DAILY:
        return pl.col(C.TS_UTC).dt.date().alias(C.SESSION_DATE)
    fallback = session_date_expr(CALENDAR_SESSION)
    chain: Any = None
    for name, profile in sessions.items():
        if not profile.rolls:
            continue
        condition = pl.col(C.EXCHANGE).str.to_uppercase() == name.upper()
        branch = session_date_expr(profile)
        chain = (
            pl.when(condition).then(branch) if chain is None else chain.when(condition).then(branch)
        )
    if chain is None:
        return fallback.alias(C.SESSION_DATE)
    result: pl.Expr = chain.otherwise(fallback).alias(C.SESSION_DATE)
    return result


def _float_expr(resolved: Mapping[str, str], name: str) -> pl.Expr:
    """A price column: `strict=False`, so `"n/a"` and `"1,234.50"` both become null.

    Never a silent repair. `"1,234.50"` is *not* read as 1234.5 — a thousands separator
    is a schema disagreement, and inventing the number would hide it forever.
    """
    if name not in resolved:
        return pl.lit(None, dtype=pl.Float64).alias(name)
    return pl.col(resolved[name]).cast(pl.Float64, strict=False).alias(name)


def _int_expr(frame: pl.DataFrame, resolved: Mapping[str, str], name: str) -> pl.Expr:
    """A count column (volume, open interest) as `Int64`.

    A float that happens to be whole is a formatting choice, so `12.0` casts to `12`.
    A fractional count is a contradiction, so `12.5` becomes null and is flagged —
    truncating it to `12` would silently invent a number the source never gave.
    """
    if name not in resolved:
        return pl.lit(None, dtype=pl.Int64).alias(name)
    column = resolved[name]
    if frame.schema[column].is_integer():
        return pl.col(column).cast(pl.Int64, strict=False).alias(name)
    as_float = pl.col(column).cast(pl.Float64, strict=False)
    whole = as_float.is_finite() & (as_float == as_float.round(0))
    return pl.when(whole).then(as_float.cast(pl.Int64, strict=False)).otherwise(None).alias(name)


# --------------------------------------------------------------------------- #
# frequency
# --------------------------------------------------------------------------- #
def _infer_frequency(frame: pl.DataFrame) -> Frequency:
    """Daily or minute, from the shape of each contract-day.

    A daily bar is a *date label*: one bar, at midnight, for the whole day. Anything
    else is intraday. A file containing both is mixed, and splitting it is an explicit
    non-goal — the caller who knows better declares `IngestOptions.frequency`.

    Distinct instants are counted, so a duplicated daily row still reads as daily:
    duplicates are the quality engine's business, not the frequency detector's.
    """
    if frame.height == 0:
        return DEFAULT_FREQUENCY
    local = pl.col(C.TS_LOCAL)
    per_day = frame.group_by(C.CONTRACT, local.dt.date().alias("day")).agg(
        pl.col(C.TS_UTC).n_unique().alias("instants"),
        (local == local.dt.truncate("1d")).all().alias("midnight_only"),
    )
    daily_like = (pl.col("instants") == 1) & pl.col("midnight_only")
    labelled = per_day.with_columns(daily_like.alias("daily_like"))
    n_daily = int(labelled.get_column("daily_like").sum())
    n_intraday = labelled.height - n_daily
    if n_daily and n_intraday:
        example = labelled.filter(~pl.col("daily_like")).sort(C.CONTRACT, "day").row(0, named=True)
        raise MixedFrequencyError(
            f"file mixes daily and intraday bars: {n_daily} contract-days look like "
            f"daily labels and {n_intraday} look intraday (for example "
            f"{example[C.CONTRACT]} on {example['day']} has {example['instants']} "
            "instants); declare IngestOptions.frequency to override"
        )
    return Frequency.DAILY if n_daily else Frequency.MINUTE


# --------------------------------------------------------------------------- #
# rejects and statistics
# --------------------------------------------------------------------------- #
def _reason_rows(frame: pl.DataFrame, reason: RejectReason) -> pl.DataFrame:
    """`(row_id, reason)` for every row of `frame`."""
    return frame.select(pl.col(_ROW_ID), pl.lit(reason.value, dtype=pl.String).alias(C.REASON))


def _reject_frame(
    original: pl.DataFrame,
    parts: Sequence[pl.DataFrame],
    columns: Sequence[str],
    source: str,
    contract_column: str | None,
) -> pl.DataFrame:
    """Join reject row ids back to their source values, in `REJECT_SCHEMA`.

    The raw values are read from the untouched frame, so a reject shows the file's own
    text — not a half-coerced version of it. The contract comes from the same place,
    which is why it is null exactly when the reject is the reason we do not know it.
    """
    if not parts:
        return empty_reject_df()
    contract = (
        pl.col(contract_column).cast(pl.String, strict=False).str.strip_chars()
        if contract_column is not None
        else pl.lit(None, dtype=pl.String)
    )
    marked = pl.concat(parts, how="vertical")
    return (
        marked.join(original, on=_ROW_ID, how="left")
        .select(
            pl.col(_ROW_ID).cast(pl.UInt32).alias(C.ROW_ID),
            pl.col(C.REASON),
            pl.when(contract.str.len_chars() > 0).then(contract).otherwise(None).alias(C.CONTRACT),
            pl.struct(list(columns)).struct.json_encode().alias(RAW_VALUES),
            pl.lit(source, dtype=pl.String).alias(SOURCE),
        )
        .sort(C.ROW_ID)
    )


def _stats(
    bars: pl.DataFrame,
    rejects: Sequence[pl.DataFrame],
    profile: SourceProfile,
    frequency: Frequency,
    source: str,
    rows_in: int,
) -> IngestStats:
    """Counts, provenance and the span, computed from the materialised frame."""
    by_reason: dict[str, int] = {}
    for part in rejects:
        for reason, count in part.get_column(C.REASON).value_counts().iter_rows():
            by_reason[reason] = by_reason.get(reason, 0) + count
    span: tuple[datetime, datetime] | None = None
    if bars.height:
        instants = bars.get_column(C.TS_UTC)
        span = (instants.min(), instants.max())  # type: ignore[assignment]
    return IngestStats(
        source=source,
        profile=profile.name,
        frequency=frequency,
        rows_in=rows_in,
        rows_out=bars.height,
        rejects_by_reason=dict(sorted(by_reason.items())),
        # Canonical order is (contract, ts_utc, row_id); if sorting left the row ids
        # ascending, the source was already in that order.
        was_sorted=bool(bars.get_column(C.ROW_ID).is_sorted()) if bars.height else True,
        contracts=tuple(bars.get_column(C.CONTRACT).unique().sort().to_list()),
        span=span,
    )


def _empty_result(
    profile: SourceProfile,
    options: IngestOptions,
    source: str,
    rows_in: int,
) -> IngestResult:
    """An empty file is not an error: correct schema, no rows, no rejects."""
    frequency = options.frequency or profile.frequency_hint or DEFAULT_FREQUENCY
    return IngestResult(
        bars=BarFrame(empty_bar_lf(), frequency, source),
        rejects=empty_reject_df(),
        stats=IngestStats(
            source=source,
            profile=profile.name,
            frequency=frequency,
            rows_in=rows_in,
            rows_out=0,
        ),
    )


def _missing_column_result(
    raw: pl.LazyFrame,
    profile: SourceProfile,
    options: IngestOptions,
    source: str,
    missing: Iterable[str],
    columns: Sequence[str],
) -> IngestResult:
    """A file that cannot be read at all still reports *why*, as one reject row.

    Reporting rather than raising means the API and the dashboard render this the same
    way as every other reject, and a batch load does not abort on one bad file.
    """
    gaps = list(missing)
    frequency = options.frequency or profile.frequency_hint or DEFAULT_FREQUENCY
    detail = pl.select(
        pl.struct(
            required=pl.lit(", ".join(gaps)),
            columns=pl.lit(", ".join(columns)),
        )
        .struct.json_encode()
        .alias(RAW_VALUES)
    ).item()
    rejects = pl.DataFrame(
        {
            C.ROW_ID: [None],
            C.REASON: [RejectReason.MISSING_REQUIRED_COLUMN.value],
            C.CONTRACT: [None],
            RAW_VALUES: [detail],
            SOURCE: [source],
        },
        schema=dict(empty_reject_df().schema),
    )
    return IngestResult(
        bars=BarFrame(empty_bar_lf(), frequency, source),
        rejects=rejects,
        stats=IngestStats(
            source=source,
            profile=profile.name,
            frequency=frequency,
            rows_in=int(raw.select(pl.len()).collect().item()),
            rows_out=0,
            rejects_by_reason={RejectReason.MISSING_REQUIRED_COLUMN.value: 1},
        ),
    )
