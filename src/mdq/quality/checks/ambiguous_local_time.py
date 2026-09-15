"""`ambiguous_local_time` — a wall clock that occurred twice on a fall-back night.

On 2025-11-02, 01:00–01:59 Chicago happens twice: once on CDT and once on CST. The
vendor supplies no UTC offset, so a bar stamped 01:30 is genuinely two possible instants
60 minutes apart. `localise_wall_clock` resolves it deterministically with
`ambiguous="earliest"` and records the fact; this check reports it.

Detection is recomputed here rather than carried as a column because `is_ambiguous` is
an *intermediate* of the time layer, not part of `BAR_SCHEMA` — so the check works on
any frame, including one built by hand or loaded from parquet, without an ingest step
having flagged it first. It localises `ts_local` twice and compares, exactly as the time
layer does.

Consequence worth stating: under `earliest`, both passes of that hour get the **same**
`ts_utc`, so a vendor that emitted both will also trip `duplicate_timestamp`. Both
findings are correct — the ambiguity *is* the reason the timestamps collide.

Both US DST transitions in the sample (2025-11-02 and 2026-03-08) fall inside the CME
Fri 16:00 → Sun 17:00 closure, so this check finds nothing on the real data and is
exercised by DST fixtures.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.emit import SEVERITY_COL, aggregate_by_session
from mdq.quality.registry import CheckContext, register_check
from mdq.time.localise import CHICAGO

__all__ = ["AmbiguousLocalTime"]


@register_check
class AmbiguousLocalTime:
    """A minute bar whose local wall clock maps to two different instants."""

    id: ClassVar[str] = "ambiguous_local_time"
    title: ClassVar[str] = "Ambiguous local time (DST fall-back)"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset({Frequency.MINUTE})
    default_severity: ClassVar[Severity] = Severity.WARNING
    suggested_rule_id: ClassVar[str | None] = "require_utc_offset"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for every minute bar inside a repeated wall-clock hour."""
        wall = pl.col(C.TS_LOCAL)
        earliest = wall.dt.replace_time_zone(CHICAGO, ambiguous="earliest", non_existent="null")
        latest = wall.dt.replace_time_zone(CHICAGO, ambiguous="latest", non_existent="null")
        ambiguous = (earliest != latest).fill_null(value=False)
        violations = bars.filter(ambiguous).with_columns(
            pl.lit(self.default_severity.label).alias(SEVERITY_COL)
        )
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format(
                "{} bar(s) fall in the repeated {} hour; resolved as the earliest "
                "of the two possible instants",
                pl.col(C.COUNT),
                pl.lit(CHICAGO),
            ),
            suggested_rule_id=self.suggested_rule_id,
            evidence={
                "timezone": pl.lit(CHICAGO),
                "resolution": pl.lit("earliest"),
                "first_local": pl.col(C.TS_LOCAL).min().dt.to_string("%Y-%m-%d %H:%M"),
                "last_local": pl.col(C.TS_LOCAL).max().dt.to_string("%Y-%m-%d %H:%M"),
            },
        )
