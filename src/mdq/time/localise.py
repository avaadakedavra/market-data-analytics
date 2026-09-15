"""Wall-clock → canonical instant.

The single most important rule in this codebase: **the vendor's timestamps are local
wall-clock, not UTC**. The HuggingFace minute files carry `timestamp_chicago_wall`
(naive) and a `timestamp_ms` that, decoded as a naive datetime, equals it exactly.
Treating either as UTC silently shifts every bar by 5–6 hours and quietly breaks
session aggregation. So the canonical instant `ts_utc` is always *derived* by
localising a declared wall clock.

Two hours a year are not clean wall clocks:

* **Spring forward** — 02:00–02:59 local never happens. `non_existent="null"` turns
  those into nulls, which this module converts into rejects with reason
  `NONEXISTENT_LOCAL_TIME`. They are never silently shifted into 03:00.
* **Fall back** — 01:00–01:59 local happens twice. `ambiguous="earliest"` picks the
  first (CDT) pass deterministically, and the row is flagged `is_ambiguous` so the
  DST quality check can report it. The flag is computed by localising twice, once
  with `earliest` and once with `latest`, and comparing the resulting instants.
"""

from __future__ import annotations

import polars as pl

from mdq.domain.findings import RejectReason
from mdq.domain.schema import C

__all__ = ["CHICAGO", "localise_wall_clock"]

#: The vendor publishes every product on Chicago wall clock, even non-Chicago venues.
CHICAGO = "America/Chicago"


def localise_wall_clock(
    lf: pl.LazyFrame,
    col: str,
    tz: str = CHICAGO,
) -> tuple[pl.LazyFrame, pl.LazyFrame]:
    """Localise a naive wall-clock column and split off the rows that cannot exist.

    Args:
        lf: Any lazy frame containing `col`.
        col: Name of a **time-zone naive** datetime column holding wall-clock
            components in `tz`. Passing a tz-aware column is a programming error.
        tz: IANA zone the wall clock belongs to.

    Returns:
        `(kept, rejects)`.

        `kept` is `lf` plus three columns:

        * `ts_utc` — `Datetime("us", "UTC")`, the canonical instant. Never null.
        * `ts_local` — `Datetime("us")`, the wall clock as traders read it.
        * `is_ambiguous` — True for the repeated fall-back hour.

        `rejects` is `lf`'s original columns plus `reason`
        (`NONEXISTENT_LOCAL_TIME` for a wall clock inside the spring-forward gap,
        `UNPARSEABLE_TIMESTAMP` for a null input timestamp).

    Raises:
        ValueError: if `col` is absent, not a datetime, or already time-zone aware.
    """
    schema = lf.collect_schema()
    if col not in schema:
        raise ValueError(f"column {col!r} not found; frame has {schema.names()}")
    dtype = schema[col]
    if not isinstance(dtype, pl.Datetime):
        raise ValueError(f"column {col!r} must be a Datetime, got {dtype}")
    if dtype.time_zone is not None:
        raise ValueError(
            f"column {col!r} is already time-zone aware ({dtype.time_zone}); "
            "localise_wall_clock expects naive wall-clock components"
        )

    wall = pl.col(col).cast(pl.Datetime("us"))
    # Localising twice is what makes ambiguity observable: on the repeated hour the
    # two passes land on instants 60 minutes apart, everywhere else they agree.
    earliest = wall.dt.replace_time_zone(tz, ambiguous="earliest", non_existent="null")
    latest = wall.dt.replace_time_zone(tz, ambiguous="latest", non_existent="null")

    annotated = lf.with_columns(
        wall.alias(C.TS_LOCAL),
        earliest.dt.convert_time_zone("UTC").alias(C.TS_UTC),
        (earliest != latest).fill_null(value=False).alias(C.IS_AMBIGUOUS),
    )

    # A source frame may already carry a `reason` column (e.g. a re-localised reject
    # frame); ours replaces it rather than colliding with it.
    original_columns = [name for name in schema.names() if name != C.REASON]
    rejects = (
        annotated.filter(pl.col(C.TS_UTC).is_null())
        .with_columns(
            pl.when(pl.col(C.TS_LOCAL).is_null())
            .then(pl.lit(RejectReason.UNPARSEABLE_TIMESTAMP.value))
            .otherwise(pl.lit(RejectReason.NONEXISTENT_LOCAL_TIME.value))
            .alias(C.REASON)
        )
        .select([*original_columns, C.REASON])
    )
    kept = annotated.filter(pl.col(C.TS_UTC).is_not_null())
    return kept, rejects
