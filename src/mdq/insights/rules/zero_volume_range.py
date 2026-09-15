"""Rule 8 — a bar that moved while nobody traded, in a session that was busy.

`volume == 0` says nothing changed hands. `high != low` says the price travelled. Both
cannot be true of the same bar, so one of the two fields is wrong — and unlike a flat
settlement print (rule 1) or a carried-forward range (rule 2), there is no convention
that makes this shape benign.

Except on a day the contract was not really trading, where it usually is benign: the
"range" is then a stale quote being carried, not a trade, and the quality engine already
downgrades it to INFO for exactly that reason. This rule therefore looks only at the ones
the engine did **not** downgrade — the ones that occurred in a session the activity model
classified as a normal, busy session for that contract. Those are contradictions with no
excuse, and each one is a bar whose range will flow into a true-range or intraday-volatility
number that nothing traded to support.

**Reading severity rather than re-deriving the regime** is deliberate. The check writes
its own downgrade into the finding's severity, so a rule that gated on the regime string
instead could disagree with the report a user is reading. It also inherits the engine's
policy for a session that was never classified at all — not excused as dormant, because
absence of evidence that trading was quiet is not evidence that it was.

**On the real sample this rule does not fire, and that is the finding.** All 400
zero-volume-with-range bars in the 40 daily files sit in DORMANT sessions — every single
one — so all 400 are carried quotes on days the contract did not trade, and none is a
contradiction that needs chasing. Reporting them as 400 defects, as an unconditioned
checker would, is exactly the noise the activity model exists to remove.
"""

from __future__ import annotations

from typing import ClassVar, Final

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.schema import C
from mdq.insights.engine import (
    InsightContext,
    count_rows,
    decode_evidence,
    evidence_share,
    register_rule,
)
from mdq.insights.models import Insight, SuggestedRule
from mdq.quality.context import Regime

__all__ = ["ZeroVolumeWithRangeInActiveSessions"]

_CHECK: Final = "zero_volume_with_range"
_REGIME: Final = "regime"
_MAX_RANGE: Final = "max_range"
_QUIET: Final = (Regime.DORMANT.value, Regime.THIN.value)

_EVIDENCE_FIELDS: Final[dict[str, pl.DataType]] = {
    _REGIME: pl.String(),
    _MAX_RANGE: pl.Float64(),
}


@register_rule
class ZeroVolumeWithRangeInActiveSessions:
    """Untraded bars that printed a range while the market was genuinely busy."""

    id: ClassVar[str] = "zero_volume_with_range_while_active"
    title: ClassVar[str] = "Prices moved on bars where nothing traded"

    #: Findings the quality engine did not downgrade — i.e. not explained by a quiet
    #: session. The check emits WARNING for those and INFO for the rest.
    min_severity: ClassVar[Severity] = Severity.WARNING

    def applies(self, ctx: InsightContext) -> bool:
        """True when at least one such bar occurred in a session that was trading."""
        return ctx.subset(_CHECK, min_severity=self.min_severity).height > 0

    def build(self, ctx: InsightContext) -> Insight:
        """Quantify the contradiction and propose refusing it where it has no excuse."""
        findings = ctx.subset(_CHECK)
        alarming = ctx.subset(_CHECK, min_severity=self.min_severity)
        total = count_rows(findings)
        unexplained = count_rows(alarming)
        share = evidence_share(unexplained, total)
        regimes = _regime_rows(findings)
        widest = _widest_range(alarming)
        contracts = ctx.contracts_in(alarming)

        pattern = (
            f"{unexplained:,} bar(s) report a high and a low that differ while recording "
            f"zero volume, in sessions the data shows were trading normally for that "
            f"contract. Those two statements cannot both be true: if nothing changed "
            f"hands, nothing moved the price. {_widest_sentence(widest)}Out of "
            f"{total:,} untraded-but-moving bars in total, {total - unexplained:,} sit in "
            f"sessions where the contract barely traded — there the range is a carried "
            f"quote rather than a trade, and it is ordinary. The {unexplained:,} flagged "
            f"here have no such excuse, and every one of them feeds a range into "
            f"volatility and true-range calculations that no trade supports."
        )
        rationale = (
            f"Require an untraded bar to be flat. Reject the {unexplained:,} bar(s) that "
            f"break that rule in an active session and alert on them — one of the two "
            f"fields is wrong and the vendor is the only party who can say which. Exempt "
            f"quiet sessions explicitly rather than by accident, so the "
            f"{total - unexplained:,} carried quotes stay out of the alert queue instead "
            f"of drowning it. Until the data is corrected, treat the range on these bars "
            f"as unusable and the close as the only figure worth reading. Confidence is "
            f"the share of untraded-but-moving bars that occur in a session that was "
            f"genuinely trading ({unexplained:,} of {total:,})."
        )
        return Insight(
            id=self.id,
            title=self.title,
            pattern=pattern,
            evidence={
                "check_id": _CHECK,
                "bars_affected": total,
                "bars_in_active_sessions": unexplained,
                "bars_in_quiet_sessions": total - unexplained,
                "bars_by_regime": regimes,
                "widest_range_in_an_active_session": widest,
                "share_unexplained_by_dormancy": round(share, 4),
                "min_severity_threshold": self.min_severity.label,
                "affected_contract_count": ctx.contract_total(alarming),
            },
            affected_contracts=contracts,
            finding_count=alarming.height,
            rule=SuggestedRule(
                rule_id="zero_volume_requires_flat",
                kind="validation",
                params={
                    "assert": "volume > 0 or high == low",
                    "exempt_when": {"session_regime": list(_QUIET)},
                    "on_violation": "reject the bar and alert",
                    "do_not_trust": [C.HIGH, C.LOW],
                    "still_usable": [C.CLOSE],
                    "escalate_to": "the data vendor: one of volume and range is wrong",
                },
                rationale=rationale,
                confidence=share,
            ),
        )


def _regime_rows(findings: pl.DataFrame) -> dict[str, int]:
    """Bars per activity regime, straight from the check's own evidence."""
    decoded = decode_evidence(findings, _EVIDENCE_FIELDS)
    grouped = (
        decoded.with_columns(pl.col(_REGIME).fill_null("UNKNOWN"))
        .group_by(_REGIME)
        .agg(pl.col(C.COUNT).sum().alias("rows"))
        .sort(_REGIME)
    )
    return {str(regime): int(rows) for regime, rows in grouped.iter_rows()}


def _widest_range(alarming: pl.DataFrame) -> float | None:
    """The largest price range printed on an untraded bar, when the check reported one.

    `None` when the check published no `max_range` — the prose then simply omits the
    number rather than printing a placeholder at a reader.
    """
    widest = decode_evidence(alarming, _EVIDENCE_FIELDS).select(pl.col(_MAX_RANGE).max()).item()
    return None if widest is None else float(widest)


def _widest_sentence(widest: float | None) -> str:
    """One concrete number, so the size of the contradiction is not left abstract."""
    if widest is None:
        return ""
    return (
        f"The widest of them puts {widest:,.4g} of price range on a bar that records not "
        f"a single contract traded. "
    )
