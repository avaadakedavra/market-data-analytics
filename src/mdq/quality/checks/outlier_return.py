"""`outlier_return` — a close-to-close move far outside this contract's recent behaviour.

Deliberately robust rather than clever. The baseline is a rolling **median absolute
deviation** over `outlier_window` bars, not a standard deviation: a single bad print
inflates a standard deviation enough to hide itself, whereas the MAD barely moves. A
return is flagged when its deviation from the rolling median exceeds `outlier_mad_k`
MADs — the classic robust z-score.

Two guards keep it from crying wolf on this dataset:

* **ACTIVE bars only, and consecutive ones.** With 22% minute coverage, the "previous
  bar" is often hours or weeks old; a return measured across that is meaningless. Bars
  in DORMANT or THIN sessions are dropped *before* the return is differenced, so the
  comparison is always between two bars of genuinely trading sessions.
* **A degenerate baseline is not a finding.** When the rolling MAD is zero (a run of
  identical closes — extremely common here) every non-zero return would be "infinitely
  many MADs", so those windows produce nothing. Flat runs are `stale_bar`'s business.

INFO severity: a large move is a thing to look at, not a defect to delete. The spec lists
this check as optional; it earns its place because a price spike is the one fault a
business user will name unprompted.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.context import A, Regime
from mdq.quality.emit import SEVERITY_COL, aggregate_by_session
from mdq.quality.registry import CheckContext, register_check

__all__ = ["OutlierReturn"]

_RETURN = "_log_return"
_MEDIAN = "_rolling_median"
_MAD = "_rolling_mad"
_DEVIATION = "_deviation"


@register_check
class OutlierReturn:
    """`|log return - rolling median| > k * rolling MAD`, between consecutive ACTIVE bars."""

    id: ClassVar[str] = "outlier_return"
    title: ClassVar[str] = "Outlier return"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.INFO
    suggested_rule_id: ClassVar[str | None] = "outlier_return_review"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for every ACTIVE bar whose return is a robust outlier."""
        window = ctx.config.outlier_window
        k = ctx.config.outlier_mad_k
        active = (
            ctx.with_regime(bars)
            .filter(pl.col(A.REGIME) == Regime.ACTIVE.value)
            .filter(pl.col(C.CLOSE) > 0)
            .sort(C.CONTRACT, C.TS_UTC)
        )
        returns = active.with_columns(pl.col(C.CLOSE).log().diff().over(C.CONTRACT).alias(_RETURN))
        centred = returns.with_columns(
            pl.col(_RETURN)
            .rolling_median(window, min_samples=window)
            .over(C.CONTRACT)
            .alias(_MEDIAN)
        )
        deviated = centred.with_columns((pl.col(_RETURN) - pl.col(_MEDIAN)).abs().alias(_DEVIATION))
        scaled = deviated.with_columns(
            pl.col(_DEVIATION)
            .rolling_median(window, min_samples=window)
            .over(C.CONTRACT)
            .alias(_MAD)
        )
        violations = scaled.filter(
            pl.col(_MAD).is_not_null()
            & (pl.col(_MAD) > 0)
            & (pl.col(_DEVIATION) > k * pl.col(_MAD))
        ).with_columns(pl.lit(self.default_severity.label).alias(SEVERITY_COL))
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format(
                "{} bar(s) moved more than {} rolling MADs from the recent median "
                "(largest {} log-return)",
                pl.col(C.COUNT),
                pl.lit(k),
                pl.col("max_abs_log_return").round(6),
            ),
            suggested_rule_id=self.suggested_rule_id,
            evidence={
                "max_abs_log_return": pl.col(_RETURN).abs().max(),
                "rolling_mad": pl.col(_MAD).max(),
                "mad_multiple": (pl.col(_DEVIATION) / pl.col(_MAD)).max(),
                "window": pl.lit(window),
                "k": pl.lit(k),
            },
        )
