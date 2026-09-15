"""`zero_volume_with_range` — a bar that moved without a single contract trading.

`high != low` means a price range was printed; `volume == 0` means nothing changed
hands. Those two statements contradict each other, so one of the fields is wrong. Unlike
`stale_bar` (flat *and* untraded, which is a perfectly ordinary settlement print), there
is no benign reading of this shape in an active market.

It is still downgraded to INFO in DORMANT sessions, where the "range" is usually an
artefact of a carried-forward quote rather than a trade — the same mechanism behind the
39/43 `invalid_ohlc` signature.

The two zero-volume checks are disjoint by construction: `stale_bar` requires
`high == low`, this one requires `high != low`, so no bar is ever reported twice.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.context import A
from mdq.quality.emit import aggregate_by_session, regime_severity
from mdq.quality.registry import CheckContext, register_check

__all__ = ["ZeroVolumeWithRange"]


@register_check
class ZeroVolumeWithRange:
    """`volume == 0` on a bar whose high and low differ."""

    id: ClassVar[str] = "zero_volume_with_range"
    title: ClassVar[str] = "Zero volume with a price range"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.WARNING
    suggested_rule_id: ClassVar[str | None] = "zero_volume_requires_flat"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for every untraded bar that nonetheless printed a range."""
        violations = (
            ctx.with_regime(bars)
            .filter((pl.col(C.VOLUME) == 0) & (pl.col(C.HIGH) != pl.col(C.LOW)))
            .with_columns(regime_severity(Severity.WARNING, dormant=Severity.INFO))
        )
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format(
                "{} bar(s) report zero volume but a non-zero range in a {} session",
                pl.col(C.COUNT),
                pl.col("regime"),
            ),
            suggested_rule_id=self.suggested_rule_id,
            evidence={
                "regime": pl.col(A.REGIME).fill_null("UNKNOWN").first(),
                "max_range": (pl.col(C.HIGH) - pl.col(C.LOW)).max(),
                "session_volume": pl.col(A.VOLUME).first(),
            },
        )
