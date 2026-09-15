"""Rule 2 — the settlement price sits outside its own bar's range.

The single most valuable row-level explanation in this dataset. There are **43 bars in
the whole corpus whose high and low do not contain their own open and close** (CL 28,
ES 7, SR3 5, ZC 3 — reproducing the vendor's own published `invalid_ohlc_row_count` on
40 of 40 files). Read as a table, they look like 43 separate corruptions.

They are not. **39 of the 43 share one signature**: `open == high == low`, with `close`
somewhere else entirely; **38 of 43 also have zero volume**. The mechanism is a
settlement convention, not a defect in the price: on a day the contract did not trade,
the vendor carried the previous open/high/low forward unchanged and updated only
`close` to the exchange's new settlement price. The settlement therefore lands outside
a range that was never updated to contain it.

What that means for a desk is concrete: on those bars the **close is the only
trustworthy number**. The high and low are yesterday's, so any range, true-range or
intraday-volatility calculation that uses them is wrong, and any validator that rejects
the bar wholesale throws away a perfectly good settlement price.

The check already measures the signature — `invalid_ohlc` emits
`carried_forward_settlement` and `zero_volume` counts in its evidence — so this rule
reads those counts rather than re-deriving them from the bars. It never needs the bar
frame at all.
"""

from __future__ import annotations

from typing import ClassVar, Final

import polars as pl

from mdq.insights.engine import (
    InsightContext,
    count_rows,
    decode_evidence,
    evidence_share,
    register_rule,
)
from mdq.insights.models import Insight, SuggestedRule

__all__ = ["CarriedForwardSettlement"]

_CHECK: Final = "invalid_ohlc"
_CARRIED: Final = "carried_forward_settlement"
_ZERO_VOLUME: Final = "zero_volume"
_HIGH_LT_LOW: Final = "high_lt_low"
_HIGH_LT_BODY: Final = "high_lt_max_open_close"
_LOW_GT_BODY: Final = "low_gt_min_open_close"

_EVIDENCE_FIELDS: Final[dict[str, pl.DataType]] = {
    _CARRIED: pl.UInt32(),
    _ZERO_VOLUME: pl.UInt32(),
    _HIGH_LT_LOW: pl.UInt32(),
    _HIGH_LT_BODY: pl.UInt32(),
    _LOW_GT_BODY: pl.UInt32(),
}


@register_rule
class CarriedForwardSettlement:
    """Incoherent OHLC bars are mostly a settlement printed against a stale range."""

    id: ClassVar[str] = "carried_forward_settlement"
    title: ClassVar[str] = "Settlement prices land outside a carried-forward range"

    #: The signature has to account for a majority of the incoherent bars before it is
    #: offered as *the* explanation rather than as one of several.
    min_share: ClassVar[float] = 0.5

    def applies(self, ctx: InsightContext) -> bool:
        """True when incoherent bars exist and most carry the carried-forward shape."""
        totals = _totals(ctx)
        return totals["bars"] > 0 and evidence_share(totals[_CARRIED], totals["bars"]) >= (
            self.min_share
        )

    def build(self, ctx: InsightContext) -> Insight:
        """Explain the signature and propose an exemption plus a repair."""
        findings = ctx.subset(_CHECK)
        totals = _totals(ctx)
        bars = totals["bars"]
        carried = totals[_CARRIED]
        untraded = totals[_ZERO_VOLUME]
        share = evidence_share(carried, bars)
        contracts = ctx.contracts_in(findings)

        pattern = (
            f"{bars:,} bars report a high and low that do not contain their own open and "
            f"close. {carried:,} of them ({share:.0%}) have an identical open, high and "
            f"low with the close somewhere else, and {untraded:,} traded no contracts at "
            f"all. That is a settlement convention rather than a broken price: on a day "
            f"the contract did not trade, the previous open, high and low were carried "
            f"forward unchanged while the close was updated to the exchange's new "
            f"settlement, so the settlement falls outside a range that was never moved. "
            f"On these bars the close is the only figure that can be trusted — the high "
            f"and the low belong to an earlier day."
        )
        rationale = (
            f"Rejecting these bars outright would discard {carried:,} valid settlement "
            f"prices; accepting them silently would feed {carried:,} stale highs and lows "
            f"into every range, true-range and intraday-volatility calculation "
            f"downstream. Do neither. Exempt the shape from the OHLC bounds test, and "
            f"either rebuild the range from the four values that are present "
            f"(high = max(open, high, low, close), low = min(...)) or publish these days "
            f"as close-only. The remaining {bars - carried:,} incoherent bars do not "
            f"carry the signature and still need investigating. Confidence is the share "
            f"of incoherent bars the signature explains ({carried:,} of {bars:,})."
        )
        return Insight(
            id=self.id,
            title=self.title,
            pattern=pattern,
            evidence={
                "check_id": _CHECK,
                "bars_affected": bars,
                "bars_with_carried_forward_range": carried,
                "bars_with_zero_volume": untraded,
                "bars_without_the_signature": bars - carried,
                "clause_high_below_open_or_close": totals[_HIGH_LT_BODY],
                "clause_low_above_open_or_close": totals[_LOW_GT_BODY],
                "clause_high_below_low": totals[_HIGH_LT_LOW],
                "share_explained_by_signature": round(share, 4),
                "min_share_threshold": self.min_share,
                "affected_contract_count": ctx.contract_total(findings),
            },
            affected_contracts=contracts,
            finding_count=findings.height,
            rule=SuggestedRule(
                rule_id="ohlc_bounds",
                kind="validation",
                params={
                    "assert": [
                        "low <= min(open, close)",
                        "high >= max(open, close)",
                        "high >= low",
                    ],
                    "exempt_when": {
                        "volume": 0,
                        "shape": "open == high == low != close",
                    },
                    "on_exempt": {
                        "treat_close_as": "settlement",
                        "repair": {
                            "high": "max(open, high, low, close)",
                            "low": "min(open, high, low, close)",
                        },
                        "alternative": "publish the session as close-only",
                        "trust": ["close"],
                        "do_not_trust": ["high", "low", "open"],
                    },
                },
                rationale=rationale,
                confidence=share,
            ),
        )


def _totals(ctx: InsightContext) -> dict[str, int]:
    """Sum the signature counters the `invalid_ohlc` check already published."""
    findings = ctx.subset(_CHECK)
    totals = dict.fromkeys(_EVIDENCE_FIELDS, 0)
    totals["bars"] = count_rows(findings)
    if findings.height == 0:
        return totals
    decoded = decode_evidence(findings, _EVIDENCE_FIELDS)
    for name in _EVIDENCE_FIELDS:
        totals[name] = int(decoded.get_column(name).sum() or 0)
    return totals
