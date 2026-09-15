"""`negative_volume` — a bar reporting fewer than zero contracts traded.

There is no market interpretation of a negative volume: it is always a feed or parsing
defect, so it stays an unconditional ERROR with no regime downgrade. Zero volume is a
different matter entirely and is handled by `stale_bar` and `zero_volume_with_range`.

The real sample has zero negative volumes; this check is exercised by the fault harness.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.emit import SEVERITY_COL, aggregate_by_session
from mdq.quality.registry import CheckContext, register_check

__all__ = ["NegativeVolume"]


@register_check
class NegativeVolume:
    """`volume < 0`."""

    id: ClassVar[str] = "negative_volume"
    title: ClassVar[str] = "Negative volume"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.ERROR
    suggested_rule_id: ClassVar[str | None] = "non_negative_volume"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for every bar whose volume is below zero."""
        violations = bars.filter(pl.col(C.VOLUME) < 0).with_columns(
            pl.lit(self.default_severity.label).alias(SEVERITY_COL)
        )
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format(
                "{} bar(s) report a negative volume (minimum {})",
                pl.col(C.COUNT),
                pl.col("min_volume"),
            ),
            suggested_rule_id=self.suggested_rule_id,
            evidence={"min_volume": pl.col(C.VOLUME).min()},
        )
