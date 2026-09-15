"""`invalid_ohlc` — a bar whose high/low do not bound its own open/close.

This is one of only two checks that fire on the real sample, and the most interesting
finding in the whole dataset. Measured corpus-wide: **43 violations** (CL 28, ES 7,
SR3 5, ZC 3), matching the vendor's own `invalid_ohlc_row_count` on 40/40 daily files.
The clause breakdown is `high < max(open, close)` x24, `low > min(open, close)` x19 and
`high < low` x**0** — so the last clause needs a fixture, while the first two are real.

**39 of the 43 share one signature**: `open == high == low` with a different `close`,
and 38 of 43 have `volume == 0`. On a non-trading day the OHL were carried forward while
`close` was updated to the new settlement, leaving the settlement outside its own bar's
range. That is a settlement convention, not corruption — so when the regime model says
the session was DORMANT the finding is downgraded to WARNING, and the evidence carries
`carried_forward_settlement` and `zero_volume` counts so the insights layer can recognise
the signature (PLAN §5 rule 2) without re-reading the bars.

`contract` and `session_date` are populated on every finding because the second axis of
the insight is *date clustering*: the 43 violations fall on just 11 dates and hit whole
contract families at once, which is a feed incident rather than per-contract corruption.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.emit import aggregate_by_session, regime_severity
from mdq.quality.registry import CheckContext, register_check

__all__ = ["InvalidOhlc"]

_HIGH_LT_LOW = "high_lt_low"
_HIGH_LT_BODY = "high_lt_max_open_close"
_LOW_GT_BODY = "low_gt_min_open_close"
_CARRIED = "carried_forward_settlement"
_ZERO_VOLUME = "zero_volume"


@register_check
class InvalidOhlc:
    """`high < low`, `high < max(open, close)` or `low > min(open, close)`."""

    id: ClassVar[str] = "invalid_ohlc"
    title: ClassVar[str] = "Incoherent OHLC"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.ERROR
    suggested_rule_id: ClassVar[str | None] = "ohlc_bounds"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for every bar whose range does not contain its own body."""
        body_high = pl.max_horizontal(pl.col(C.OPEN), pl.col(C.CLOSE))
        body_low = pl.min_horizontal(pl.col(C.OPEN), pl.col(C.CLOSE))
        # Null prices compare to null, so a null-priced bar never lands here — that is
        # `missing_value`'s territory and double-reporting would be noise.
        flagged = ctx.with_regime(bars).with_columns(
            (pl.col(C.HIGH) < pl.col(C.LOW)).alias(_HIGH_LT_LOW),
            (pl.col(C.HIGH) < body_high).alias(_HIGH_LT_BODY),
            (pl.col(C.LOW) > body_low).alias(_LOW_GT_BODY),
            (
                (pl.col(C.OPEN) == pl.col(C.HIGH))
                & (pl.col(C.HIGH) == pl.col(C.LOW))
                & (pl.col(C.CLOSE) != pl.col(C.OPEN))
            ).alias(_CARRIED),
            (pl.col(C.VOLUME) == 0).alias(_ZERO_VOLUME),
        )
        violations = flagged.filter(
            pl.any_horizontal(pl.col(_HIGH_LT_LOW), pl.col(_HIGH_LT_BODY), pl.col(_LOW_GT_BODY))
        ).with_columns(regime_severity(Severity.ERROR, dormant=Severity.WARNING))
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format(
                "{} bar(s) violate the OHLC relation "
                "({} with a settlement outside a carried-forward range)",
                pl.col(C.COUNT),
                pl.col(_CARRIED),
            ),
            suggested_rule_id=self.suggested_rule_id,
            evidence={
                _HIGH_LT_LOW: pl.col(_HIGH_LT_LOW).sum().cast(pl.UInt32),
                _HIGH_LT_BODY: pl.col(_HIGH_LT_BODY).sum().cast(pl.UInt32),
                _LOW_GT_BODY: pl.col(_LOW_GT_BODY).sum().cast(pl.UInt32),
                _CARRIED: pl.col(_CARRIED).sum().cast(pl.UInt32),
                _ZERO_VOLUME: pl.col(_ZERO_VOLUME).sum().cast(pl.UInt32),
            },
        )
