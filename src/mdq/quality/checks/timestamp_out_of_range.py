"""`timestamp_out_of_range` — a bar dated outside any plausible trading history.

Two bounds, both from `QualityConfig`:

* later than `now + future_tolerance_days` — a clock skew, a bad epoch unit, or a
  timestamp parsed in the wrong scale (seconds read as milliseconds lands centuries out);
* earlier than `min_valid_year` — usually a null or sentinel decoded as epoch zero.

A pre-1970 *daily* bar is legitimate for long-dated history, which is why the lower
bound is a configurable year rather than the epoch.

WARNING rather than ERROR: the bar's prices may be perfectly good, but every window it
lands in is wrong, so it needs human attention rather than automatic deletion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.emit import SEVERITY_COL, aggregate_by_session
from mdq.quality.registry import CheckContext, register_check

__all__ = ["TimestampOutOfRange"]


@register_check
class TimestampOutOfRange:
    """`ts_utc` beyond the configured future tolerance, or before `min_valid_year`."""

    id: ClassVar[str] = "timestamp_out_of_range"
    title: ClassVar[str] = "Timestamp out of range"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.WARNING
    suggested_rule_id: ClassVar[str | None] = "timestamp_bounds"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for every bar outside `[min_valid_year, now + tolerance]`."""
        cfg = ctx.config
        cutoff = datetime.now(UTC) + timedelta(days=cfg.future_tolerance_days)
        too_late = pl.col(C.TS_UTC) > pl.lit(cutoff)
        too_early = pl.col(C.TS_UTC).dt.year() < cfg.min_valid_year
        violations = (
            bars.with_columns(too_late.alias("_too_late"), too_early.alias("_too_early"))
            .filter(pl.col("_too_late") | pl.col("_too_early"))
            .with_columns(pl.lit(self.default_severity.label).alias(SEVERITY_COL))
        )
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format(
                "{} bar(s) are timestamped outside the plausible range "
                "({} in the future, {} before {})",
                pl.col(C.COUNT),
                pl.col("future"),
                pl.col("ancient"),
                pl.lit(str(cfg.min_valid_year)),
            ),
            suggested_rule_id=self.suggested_rule_id,
            evidence={
                "future": pl.col("_too_late").sum().cast(pl.UInt32),
                "ancient": pl.col("_too_early").sum().cast(pl.UInt32),
                "future_cutoff_utc": pl.lit(cutoff.isoformat()),
                "min_valid_year": pl.lit(cfg.min_valid_year),
            },
        )
