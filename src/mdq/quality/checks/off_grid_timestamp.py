"""`off_grid_timestamp` — a minute bar that is not on a minute boundary.

A one-minute bar stamped 09:30:17 is not a one-minute bar. It usually means a tick or a
second-resolution feed has been mislabelled, and it silently corrupts every gap
calculation downstream, so it is worth a WARNING even though the bar itself may be fine.

Daily frames are exempt: their `ts_utc` is a *label* (the session date at 00:00 UTC),
not a trading instant, so grid alignment says nothing there.

The real sample is perfectly aligned; this check is exercised by the fault harness and
its golden expectation on real data is zero.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.emit import SEVERITY_COL, aggregate_by_session
from mdq.quality.registry import CheckContext, register_check

__all__ = ["OffGridTimestamp"]


@register_check
class OffGridTimestamp:
    """A minute bar whose seconds or sub-seconds are non-zero."""

    id: ClassVar[str] = "off_grid_timestamp"
    title: ClassVar[str] = "Timestamp off the minute grid"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset({Frequency.MINUTE})
    default_severity: ClassVar[Severity] = Severity.WARNING
    suggested_rule_id: ClassVar[str | None] = "minute_grid_alignment"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for every minute bar not stamped exactly on the minute."""
        off_grid = (pl.col(C.TS_UTC).dt.second() != 0) | (pl.col(C.TS_UTC).dt.microsecond() != 0)
        violations = bars.filter(off_grid).with_columns(
            pl.lit(self.default_severity.label).alias(SEVERITY_COL)
        )
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format(
                "{} minute bar(s) are not aligned to a minute boundary", pl.col(C.COUNT)
            ),
            suggested_rule_id=self.suggested_rule_id,
            evidence={
                "distinct_second_offsets": pl.col(C.TS_UTC).dt.second().n_unique().cast(pl.UInt32),
                "example_ts_local": pl.col(C.TS_LOCAL).min().dt.to_string("%Y-%m-%d %H:%M:%S%.6f"),
            },
        )
