"""`stale_bar` — a bar with no range at all: `open == high == low == close`.

This is the single most common observation in the corpus — **14,152** no-range bars across
the daily files, 47% of daily rows and 48% of minute rows — and reporting all of them at
WARNING would make the report unreadable. The regime model is what makes the check usable
instead of unbearable.

**Detection matches the vendor exactly.** `files.parquet` publishes a
`no_range_bar_count` per file; recomputing `open == high == low == close` over all 40 daily
files reproduces it to the row (14,152), which makes this a free golden test. Note the
vendor's definition does **not** mention volume — that is measured, not assumed:

| predicate | rows |
|---|---|
| `open == high == low == close` | **14,152** — the vendor's count |
| `high == low` | 14,192 (the extra 40 are `invalid_ohlc` rows, whose close escaped the range) |
| flat **and** `volume == 0` | 12,841 |

**Deviation from PLAN §4.3**, deliberate and measured. The plan gives the logic as
`volume == 0 & o == h == l == c` *and* claims the golden count 14,152 against the vendor.
Those two statements are inconsistent: the volume clause yields 12,841. Detection
therefore follows the vendor (so the golden test is a real, external check), and volume
becomes a **severity** discriminator rather than a filter — which loses nothing, because
`volume` is carried in the evidence either way:

* flat **and** untraded in a DORMANT or THIN session — a settlement-only print, the bulk
  of the 14,152 — is INFO;
* flat and untraded in an **ACTIVE** session means the feed stopped updating while the
  market was moving: WARNING;
* flat but *traded* is INFO everywhere — an illiquid contract whose day's business all
  happened at one price is unusual, not wrong.

Also deliberate: the plan lists this check as daily-only; it is registered for **both**
frequencies. On daily data a zero-volume session is by definition DORMANT (the daily
regime is volume-driven, one bar per session), so a daily-only check could never reach the
ACTIVE branch and the severity distinction would be dead code. Minute frames are where a
flat bar inside a busy session is genuinely diagnostic.
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

__all__ = ["StaleBar"]

_UNTRADED = "_untraded"


@register_check
class StaleBar:
    """A no-range bar; WARNING only when it was also untraded inside an ACTIVE session."""

    id: ClassVar[str] = "stale_bar"
    title: ClassVar[str] = "Stale (no-range) bar"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.WARNING
    suggested_rule_id: ClassVar[str | None] = "classify_settlement_only_bars"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for every bar with `open == high == low == close`."""
        flat = (
            (pl.col(C.OPEN) == pl.col(C.HIGH))
            & (pl.col(C.HIGH) == pl.col(C.LOW))
            & (pl.col(C.LOW) == pl.col(C.CLOSE))
        )
        violations = (
            ctx.with_regime(bars)
            .filter(flat)
            .with_columns((pl.col(C.VOLUME) == 0).alias(_UNTRADED))
            .with_columns(
                pl.when((pl.col(A.REGIME) == Regime.ACTIVE.value) & pl.col(_UNTRADED))
                .then(pl.lit(Severity.WARNING.label))
                .otherwise(pl.lit(Severity.INFO.label))
                .alias(SEVERITY_COL)
            )
        )
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format(
                "{} bar(s) with no range in a {} session ({} of them untraded)",
                pl.col(C.COUNT),
                pl.col("regime"),
                pl.col("untraded"),
            ),
            suggested_rule_id=self.suggested_rule_id,
            evidence={
                "regime": pl.col(A.REGIME).fill_null("UNKNOWN").first(),
                "untraded": pl.col(_UNTRADED).sum().cast(pl.UInt32),
                "session_bar_count": pl.col(A.BAR_COUNT).first(),
                "session_volume": pl.col(A.VOLUME).first(),
                "activity_baseline": pl.col(A.BASELINE).first(),
                "dormant_threshold": pl.col(A.DORMANT_THRESHOLD).first(),
                "price": pl.col(C.CLOSE).first(),
            },
        )
