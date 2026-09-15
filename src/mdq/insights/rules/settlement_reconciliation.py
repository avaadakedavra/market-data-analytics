"""Rule 10 — the daily close is a settlement price, so reconciling it is a mistake.

Compare a vendor's daily bar against the same session rebuilt from its own minute bars
and most fields line up. The close does not, and it never will: the daily close is the
**exchange's settlement price** — a committee figure struck from a closing range or a
theoretical calculation — while the minute file's last bar is simply the last trade
printed. They are two different quantities that happen to share a name. Measured on this
vendor's data, the session open agrees on 98% of sessions and the low on all 69 verified
liquid ones, while the close agrees on 1% (PLAN §0.1). One per cent is not a tolerance
problem; it is a definition problem.

That matters because the obvious reconciliation — "check the daily bar against the
minute bars, flag anything that differs" — produces a permanent stream of close
mismatches that are all correct data. Nobody triages the same false alarm every day for
long; they switch the whole reconciliation off, and the real breaks in high, low and
volume go with it. Excluding the close by name is what keeps the rest of the check alive.

So this rule reads the field each mismatch was raised on. If the close accounts for most
of them, it says so, and proposes a reconciliation that compares high, low and volume
with a tolerance and skips the close entirely — recording *why* it is skipped, so the
exclusion reads as a documented convention rather than as a suppressed failure.

**This rule cannot fire yet, by design.** It consumes `cross_frequency_mismatch`, which
PLAN §4.3 lists as a WP8 stretch check and which does not exist. With no such findings
`applies` is simply false and the engine produces nothing — no error, no empty insight,
no placeholder. That is the whole reason the engine asks `applies` before `build`.

**The contract WP8 must honour** for this rule to light up: one finding per mismatching
`(contract, session_date, field)`, carrying evidence

| key | type | meaning |
|---|---|---|
| `field` | string | `close`, `high`, `low` or `volume` — which figure disagreed |
| `relative_difference` | float | `abs(daily - minute) / abs(daily)`; for volume, the
  ratio's distance from 1 |

Anything else in the evidence is ignored; a finding missing `field` is counted but
attributed to no field, which is what keeps this rule honest about a partial
implementation rather than guessing.
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

__all__ = ["SettlementReconciliation"]

_CHECK: Final = "cross_frequency_mismatch"
_FIELD: Final = "field"
_DIFFERENCE: Final = "relative_difference"

_CLOSE: Final = "close"
#: The fields a reconciliation can compare honestly. Order is the order a user reads.
_COMPARABLE: Final[tuple[str, ...]] = ("high", "low", "volume")

#: Volume tolerance from PLAN §4.3: the vendor's daily volume runs 3–5% above the sum of
#: its own minute bars, so a band rather than an equality is the only workable test.
_VOLUME_RATIO_BAND: Final[tuple[float, float]] = (0.8, 1.05)

#: Used when the report shows no comparable mismatch to size a tolerance from. Five basis
#: points is tight enough to catch a real break and loose enough to survive rounding.
_FALLBACK_PRICE_TOLERANCE: Final = 0.0005

_EVIDENCE_FIELDS: Final[dict[str, pl.DataType]] = {
    _FIELD: pl.String(),
    _DIFFERENCE: pl.Float64(),
}


@register_rule
class SettlementReconciliation:
    """Daily-versus-minute breaks that are concentrated in the close."""

    id: ClassVar[str] = "daily_close_is_a_settlement_price"
    title: ClassVar[str] = "Never reconcile the daily close against the minute bars"

    #: Share of the breaks that must be close breaks before the close is named as the
    #: cause. Below this the reconciliation has a genuine problem somewhere else too, and
    #: excluding the close would hide it.
    min_share: ClassVar[float] = 0.5

    def applies(self, ctx: InsightContext) -> bool:
        """True when cross-frequency breaks exist and most of them are the close.

        False when the `cross_frequency_mismatch` check has not run — which is the normal
        state until WP8 builds it.
        """
        counts = _field_rows(ctx)
        total = sum(counts.values())
        return total > 0 and evidence_share(counts.get(_CLOSE, 0), total) >= self.min_share

    def build(self, ctx: InsightContext) -> Insight:
        """Explain the settlement convention and propose a reconciliation that survives."""
        findings = ctx.subset(_CHECK)
        counts = _field_rows(ctx)
        total = sum(counts.values())
        close_breaks = counts.get(_CLOSE, 0)
        other = total - close_breaks
        share = evidence_share(close_breaks, total)
        tolerance = _price_tolerance(ctx)
        contracts = ctx.contracts_in(findings)

        pattern = (
            f"{total:,} session(s) fail to reconcile between the daily file and the same "
            f"sessions rebuilt from minute bars, and {close_breaks:,} of those breaks "
            f"({share:.0%}) are on the close alone. That is not a data error. The daily "
            f"close is the exchange's **settlement price** — struck from a closing range "
            f"or calculated by the exchange — while the minute file's last bar is just "
            f"the last trade to print. They are two different numbers by definition and "
            f"agreeing would be the surprise. The other {other:,} break(s) are on high, "
            f"low or volume, where the two sources really are describing the same thing, "
            f"and those are the ones that mean something."
        )
        rationale = (
            f"Reconcile {', '.join(_COMPARABLE)} between the two frequencies and exclude "
            f"the close by name, recording the settlement convention as the reason so the "
            f"exclusion is a documented decision rather than a silenced alarm. Left in, "
            f"the close generates {close_breaks:,} correct-but-failing comparisons; nobody "
            f"triages that for long, and when the reconciliation is switched off in "
            f"frustration the {other:,} genuine break(s) go dark with it. Compare prices "
            f"to {tolerance:.4%} and volume as a ratio within "
            f"{_VOLUME_RATIO_BAND[0]}–{_VOLUME_RATIO_BAND[1]} — the daily volume runs a "
            f"few per cent above the sum of its own minute bars because late and "
            f"block-reported trades are included — and run it only on sessions with "
            f"liquid minute coverage, since a thin session simply has no minute bar at "
            f"the true high or low. Confidence is the share of breaks the settlement "
            f"convention explains ({close_breaks:,} of {total:,})."
        )
        return Insight(
            id=self.id,
            title=self.title,
            pattern=pattern,
            evidence={
                "check_id": _CHECK,
                "bars_affected": total,
                "breaks_on_close": close_breaks,
                "breaks_on_other_fields": other,
                "breaks_by_field": dict(sorted(counts.items())),
                "observed_price_tolerance": tolerance,
                "share_explained_by_settlement": round(share, 4),
                "min_share_threshold": self.min_share,
                "affected_contract_count": ctx.contract_total(findings),
            },
            affected_contracts=contracts,
            finding_count=findings.height,
            rule=SuggestedRule(
                rule_id="reconcile_daily_vs_minute",
                kind="validation",
                params={
                    "fields": list(_COMPARABLE),
                    "skip": [_CLOSE],
                    "skip_reason": (
                        "the daily close is the exchange settlement price, not the last "
                        "minute trade; the two are different quantities by definition"
                    ),
                    "tolerance": {
                        "price_relative": tolerance,
                        "volume_ratio": list(_VOLUME_RATIO_BAND),
                    },
                    "granularity": "one comparison per contract and session date",
                    "restrict_to": "sessions with liquid minute coverage",
                },
                rationale=rationale,
                confidence=share,
            ),
        )


def _field_rows(ctx: InsightContext) -> dict[str, int]:
    """Breaks per reconciled field, from the check's own evidence.

    A finding with no `field` is counted under `"(unattributed)"` rather than dropped:
    the total a user reads must be the total the report contains.
    """
    findings = ctx.subset(_CHECK)
    if findings.height == 0:
        return {}
    decoded = decode_evidence(findings, _EVIDENCE_FIELDS)
    grouped = (
        decoded.with_columns(pl.col(_FIELD).fill_null("(unattributed)"))
        .group_by(_FIELD)
        .agg(pl.col(C.COUNT).sum().alias("rows"))
    )
    return {str(field): int(rows) for field, rows in grouped.iter_rows()}


def _price_tolerance(ctx: InsightContext) -> float:
    """A tolerance sized from the breaks that are *not* the close.

    Those are the comparisons the proposed rule will actually make, so their observed
    spread is the only evidence in the report about how tight it can be. With none to
    measure, fall back to the documented default rather than to zero — a zero tolerance
    would fail on rounding alone.
    """
    others = decode_evidence(ctx.subset(_CHECK), _EVIDENCE_FIELDS).filter(
        pl.col(_FIELD).is_in([f for f in _COMPARABLE if f != "volume"])
    )
    if others.height == 0:
        return _FALLBACK_PRICE_TOLERANCE
    widest = others.select(pl.col(_DIFFERENCE).max()).item()
    if widest is None:
        return _FALLBACK_PRICE_TOLERANCE
    return max(_FALLBACK_PRICE_TOLERANCE, round(float(widest), 6))
