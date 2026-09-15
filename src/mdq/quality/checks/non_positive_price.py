"""`non_positive_price` — an open/high/low/close at or below zero.

With one documented exception. WTI crude settled **negative** on 2020-04-20, so a
non-positive `CL` price is a real market event and flagging it would be wrong. The
exempt roots live in `QualityConfig.negative_price_roots` rather than in an `if` here,
so the exception is configuration a user can inspect and change, not a special case
buried in code.

The real sample has zero non-positive prices; this check is exercised by the fault
harness.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.emit import SEVERITY_COL, aggregate_by_session
from mdq.quality.registry import CheckContext, register_check

__all__ = ["NonPositivePrice"]


@register_check
class NonPositivePrice:
    """Any of open/high/low/close <= 0, outside the roots allowed to print negative."""

    id: ClassVar[str] = "non_positive_price"
    title: ClassVar[str] = "Non-positive price"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.ERROR
    suggested_rule_id: ClassVar[str | None] = "positive_price_bounds"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for every bar with a non-positive price on a non-exempt root."""
        exempt_roots = sorted(ctx.config.negative_price_roots)
        # A null root is never exempt: we do not grant an exception we cannot attribute.
        exempt = (
            pl.col(C.ROOT).str.to_uppercase().is_in(exempt_roots).fill_null(value=False)
            if exempt_roots
            else pl.lit(value=False)
        )
        non_positive = pl.any_horizontal(pl.col(price) <= 0 for price in C.PRICES)
        violations = bars.filter(non_positive & ~exempt).with_columns(
            pl.lit(self.default_severity.label).alias(SEVERITY_COL)
        )
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format(
                "{} bar(s) have a price at or below zero (minimum {})",
                pl.col(C.COUNT),
                pl.col("min_price"),
            ),
            suggested_rule_id=self.suggested_rule_id,
            evidence={
                "min_price": pl.min_horizontal(pl.col(price).min() for price in C.PRICES),
                "exempt_roots": pl.lit(", ".join(exempt_roots) or "none"),
            },
        )
