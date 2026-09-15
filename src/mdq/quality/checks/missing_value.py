"""`missing_value` — a null in any of open/high/low/close/volume.

Ingestion deliberately *keeps* rows whose prices or volume could not be parsed rather
than rejecting them: the row is locatable, so the business user should see it in
context. This check is what makes them visible.

`open_interest` is excluded — the schema declares it null for every minute bar, so a
null there is the contract, not a defect.

The real sample has **zero** nulls across all 5.3M minute and 30k daily rows; this check
is exercised entirely by the fault harness, and the golden expectation on real data is
zero. That is stated plainly rather than worked around.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.emit import SEVERITY_COL, aggregate_by_session
from mdq.quality.registry import CheckContext, register_check

__all__ = ["MissingValue"]

_FIELDS = (*C.PRICES, C.VOLUME)


@register_check
class MissingValue:
    """Null price or volume on a bar that ingestion could place on the timeline."""

    id: ClassVar[str] = "missing_value"
    title: ClassVar[str] = "Null price or volume"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.ERROR
    suggested_rule_id: ClassVar[str | None] = "require_complete_bar"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for every bar with a null in `open/high/low/close/volume`."""
        any_null = pl.any_horizontal(pl.col(field).is_null() for field in _FIELDS)
        violations = bars.filter(any_null).with_columns(
            pl.lit(self.default_severity.label).alias(SEVERITY_COL)
        )
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format("{} bar(s) have a null price or volume", pl.col(C.COUNT)),
            suggested_rule_id=self.suggested_rule_id,
            evidence={
                f"null_{field}": pl.col(field).is_null().sum().cast(pl.UInt32) for field in _FIELDS
            },
        )
