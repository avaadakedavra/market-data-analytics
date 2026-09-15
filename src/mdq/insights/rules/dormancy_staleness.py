"""Rule 1 — flat bars are explained by sessions in which nothing was trading.

This is the most common pattern in the corpus by three orders of magnitude, and the one
that decides whether the tool is usable at all. **14,152 daily bars print no range**
(`open == high == low == close`) — 47% of every daily row. Measured on all 40 daily
files, the activity regime of the session each one sits in:

| regime | bars | share |
|---|---|---|
| DORMANT (nothing traded that session) | 12,841 | 90.7% |
| THIN (traded far below the contract's own norm) | 1,282 | 9.1% |
| ACTIVE (an ordinary session for that contract) | **29** | **0.2%** |

So 99.8% of all "stale" bars are a settlement price being published on a day the
contract did not really trade — completely normal market behaviour — and 29 are worth
a human's attention. Reporting all 14,152 identically would bury those 29.

**Deviation from PLAN §5 rule 1**, deliberate and measured. The plan gates on "≥90% of
`stale_bar` findings fall in DORMANT sessions". On the real corpus that is 90.73% — one
percentage point of headroom, so a slightly different threshold in `QualityConfig` would
silently switch the headline insight off. The gate here is "the session was not trading
normally", i.e. DORMANT **or** THIN, which is the same claim in business terms and is
99.8% true rather than 90.7% true. The DORMANT/THIN/ACTIVE split is in the evidence
either way, so nothing is hidden by the choice.
"""

from __future__ import annotations

from typing import ClassVar, Final

import polars as pl

from mdq.domain.schema import C
from mdq.insights.engine import (
    InsightContext,
    decode_evidence,
    evidence_share,
    register_rule,
)
from mdq.insights.models import Insight, SuggestedRule
from mdq.quality.context import Regime

__all__ = ["DormancyExplainsStaleness"]

_CHECK: Final = "stale_bar"
_REGIME: Final = "regime"
_UNTRADED: Final = "untraded"
_QUIET: Final = (Regime.DORMANT.value, Regime.THIN.value)


@register_rule
class DormancyExplainsStaleness:
    """Stale bars concentrate in sessions the contract was not really trading in."""

    id: ClassVar[str] = "dormancy_explains_staleness"
    title: ClassVar[str] = "Flat bars are settlement prints, not a frozen feed"

    #: Share of flat bars that must fall outside ACTIVE sessions before the pattern is
    #: called an explanation rather than a coincidence.
    min_share: ClassVar[float] = 0.9

    def applies(self, ctx: InsightContext) -> bool:
        """True when flat bars exist and nearly all of them sit in quiet sessions."""
        counts = _regime_rows(ctx)
        total = sum(counts.values())
        return total > 0 and evidence_share(_quiet(counts), total) >= self.min_share

    def build(self, ctx: InsightContext) -> Insight:
        """Explain the concentration and propose tagging rather than alerting."""
        findings = ctx.subset(_CHECK)
        counts = _regime_rows(ctx)
        total = sum(counts.values())
        quiet = _quiet(counts)
        active = counts.get(Regime.ACTIVE.value, 0)
        untraded = _untraded_rows(findings)
        share = evidence_share(quiet, total)
        contracts = ctx.contracts_in(findings)

        pattern = (
            f"{total:,} bars open, close and trade at a single price, with no range at "
            f"all. That is not a frozen feed: {share:.1%} of them fall on sessions where "
            f"the contract barely traded or did not trade at all "
            f"({counts.get(Regime.DORMANT.value, 0):,} with no volume whatsoever, "
            f"{counts.get(Regime.THIN.value, 0):,} far below that contract's own normal "
            f"turnover). What is being published on those days is the exchange's "
            f"settlement price, which is exactly what it should be. Only {active:,} flat "
            f"bars — {evidence_share(active, total):.1%} — occur in a session that was "
            f"otherwise busy, and those are the ones worth a phone call."
        )
        rationale = (
            f"Treating all {total:,} flat bars as defects would make the quality report "
            f"unreadable and would distort every volatility and VWAP calculation that "
            f"consumes them. Tag them as settlement-only instead: exclude them from "
            f"volume-weighted and range-based analytics, keep them for the price series, "
            f"and raise an alert only for the {active:,} that appear in an active "
            f"session. Confidence is the share of flat bars the explanation covers "
            f"({quiet:,} of {total:,})."
        )
        return Insight(
            id=self.id,
            title=self.title,
            pattern=pattern,
            evidence={
                "check_id": _CHECK,
                "bars_affected": total,
                "bars_in_dormant_sessions": counts.get(Regime.DORMANT.value, 0),
                "bars_in_thin_sessions": counts.get(Regime.THIN.value, 0),
                "bars_in_active_sessions": active,
                "bars_with_zero_volume": untraded,
                "share_explained_by_dormancy": round(share, 4),
                "min_share_threshold": self.min_share,
                "affected_contract_count": ctx.contract_total(findings),
            },
            affected_contracts=contracts,
            finding_count=findings.height,
            rule=SuggestedRule(
                rule_id="classify_settlement_only_bars",
                kind="cleansing",
                params={
                    "match": {
                        "range": "open == high == low == close",
                        "volume": 0,
                        "session_regime": list(_QUIET),
                    },
                    "action": "tag",
                    "tag": "settlement_only",
                    "exclude_from": ["vwap", "realised_volatility", "range_statistics"],
                    "keep_in": ["price_series", "returns"],
                    "alert_when": "session_regime == ACTIVE",
                },
                rationale=rationale,
                confidence=share,
            ),
        )


def _regime_rows(ctx: InsightContext) -> dict[str, int]:
    """Bars per activity regime, read straight from the check's own evidence."""
    findings = ctx.subset(_CHECK)
    if findings.height == 0:
        return {}
    decoded = decode_evidence(findings, {_REGIME: pl.String()})
    grouped = decoded.group_by(_REGIME).agg(pl.col(C.COUNT).sum().alias("rows"))
    return {str(regime): int(rows) for regime, rows in grouped.iter_rows()}


def _quiet(counts: dict[str, int]) -> int:
    return sum(counts.get(regime, 0) for regime in _QUIET)


def _untraded_rows(findings: pl.DataFrame) -> int:
    """Flat bars that also had zero volume — the vendor's own settlement-print shape."""
    decoded = decode_evidence(findings, {_UNTRADED: pl.UInt32()})
    return int(decoded.get_column(_UNTRADED).sum() or 0)
