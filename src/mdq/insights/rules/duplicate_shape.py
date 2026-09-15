"""Rule 6 — the same bar twice: a harmless re-delivery, or two prices that disagree.

"Duplicate rows" is one phrase covering two completely different problems, and the
difference decides what you are allowed to do about them.

* Every copy **identical** — the file was sent twice, or a window overlapped. Nothing is
  in doubt; collapsing the copies loses nothing at all. That is a cleansing rule.
* The copies **disagree** — two different prices, or two different volumes, are claimed
  for the same contract at the same instant. One of them is wrong and the data does not
  say which. Keeping either is a guess dressed up as a fact, so both go, and somebody
  gets told. That is a validation rule.

**Safety, not majority, picks the rule.** If even one duplicate contradicts itself the
rule proposed is the strict one, however many harmless re-deliveries surround it: a
`drop_exact_duplicates` policy adopted over a dataset that also contains contradictions
would silently keep whichever contradictory copy happened to arrive first. The confidence
is then the share of duplicated bars that actually contradict, so a reader can see how
much of the problem the strict rule is really about.

**On the real sample this rule cannot fire, and the reason is worth stating plainly.**
The vendor's sample contains **zero** duplicate timestamps — independently reproduced
against the vendor's own published `duplicate_timestamp_count` on 40 of 40 daily files.
There is no arrangement of thresholds that would make this pattern appear in that data,
and contriving one would be a lie about the sample. It fires on uploaded files and on the
fault harness.

One genuine wild source of duplicates does exist, and this rule names it when it sees it:
on the autumn clock change the same wall-clock hour is played twice, and a vendor who
publishes wall clocks without an offset — as this one does — cannot distinguish the two
passes. Those bars land on the same instant by construction. They are not a re-delivery
and no dedupe rule will fix them; only a UTC offset from the vendor will.
"""

from __future__ import annotations

from typing import ClassVar, Final

import polars as pl

from mdq.domain.schema import C
from mdq.insights.engine import (
    InsightContext,
    count_rows,
    decode_evidence,
    evidence_share,
    register_rule,
)
from mdq.insights.models import Insight, SuggestedRule

__all__ = ["DuplicateShape"]

_CHECK: Final = "duplicate_timestamp"
_AMBIGUOUS_CHECK: Final = "ambiguous_local_time"
_CONFLICTING: Final = "conflicting"
_LARGEST_GROUP: Final = "largest_group"
_DISTINCT_TIMESTAMPS: Final = "distinct_timestamps"

_EVIDENCE_FIELDS: Final[dict[str, pl.DataType]] = {
    _CONFLICTING: pl.Boolean(),
    _LARGEST_GROUP: pl.UInt32(),
    _DISTINCT_TIMESTAMPS: pl.UInt32(),
}

#: The value columns the check compares to decide whether two bars are "the same bar".
_COMPARED: Final = [*C.PRICES, C.VOLUME, C.OPEN_INTEREST]


@register_rule
class DuplicateShape:
    """Duplicated timestamps are re-deliveries, contradictions, or a clock change."""

    id: ClassVar[str] = "duplicate_timestamp_shape"
    title: ClassVar[str] = "Duplicate bars: one to collapse, one to refuse"

    def applies(self, ctx: InsightContext) -> bool:
        """True whenever any bar shares its instant with another."""
        return _totals(ctx)["bars"] > 0

    def build(self, ctx: InsightContext) -> Insight:
        """Explain which kind of duplicate this is and propose the matching policy."""
        findings = ctx.subset(_CHECK)
        totals = _totals(ctx)
        bars = totals["bars"]
        conflicting = totals["conflicting_bars"]
        exact = totals["exact_bars"]
        ambiguous = ctx.subset(_AMBIGUOUS_CHECK)
        ambiguous_bars = count_rows(ambiguous)
        contracts = ctx.contracts_in(findings)

        strict = conflicting > 0
        share = evidence_share(conflicting if strict else exact, bars)

        pattern = (
            f"{bars:,} bars share a timestamp with another bar for the same contract, "
            f"across {totals['duplicated_instants']:,} distinct instants. "
            f"{_shape_sentence(exact, conflicting)}"
            f"{_clock_change_sentence(ambiguous_bars)}"
        )
        rule = _strict_rule(bars, exact, conflicting, share) if strict else _lenient_rule(bars)
        return Insight(
            id=self.id,
            title=self.title,
            pattern=pattern,
            evidence={
                "check_id": _CHECK,
                "bars_affected": bars,
                "bars_in_exact_duplicates": exact,
                "bars_in_conflicting_duplicates": conflicting,
                "duplicated_instants": totals["duplicated_instants"],
                "largest_duplicate_group": totals["largest_group"],
                "bars_on_an_ambiguous_wall_clock": ambiguous_bars,
                "share_explained_by_shape": round(share, 4),
                "compared_columns": _COMPARED,
                "affected_contract_count": ctx.contract_total(findings),
            },
            affected_contracts=contracts,
            finding_count=findings.height,
            rule=rule,
        )


def _shape_sentence(exact: int, conflicting: int) -> str:
    """The half of the prose that says which problem this actually is."""
    if conflicting == 0:
        return (
            "Every one of them is an exact copy — same open, high, low, close, volume "
            "and open interest — so nothing is in dispute: the file, or part of it, was "
            "simply delivered twice. Collapsing the copies loses no information "
            "whatsoever, and leaving them in double-counts volume in every total that "
            "reads them."
        )
    if exact == 0:
        return (
            f"All {conflicting:,} of them contradict each other: two different sets of "
            f"values are claimed for the same contract at the same instant. The data "
            f"gives no way to tell which is the real one, so neither can be trusted, and "
            f"any figure computed from them is a coin toss presented as a number."
        )
    return (
        f"{exact:,} of them are exact copies — a re-delivery, harmless and safe to "
        f"collapse — but {conflicting:,} contradict each other, claiming two different "
        f"sets of values for the same contract at the same instant. The data gives no "
        f"way to tell which of those is real, and the contradictory ones are the whole "
        f"problem: they are indistinguishable from the harmless ones until you compare "
        f"the values."
    )


def _clock_change_sentence(ambiguous_bars: int) -> str:
    """Names the autumn clock change when the report shows it, and only then."""
    if ambiguous_bars == 0:
        return ""
    return (
        f" Note that {ambiguous_bars:,} bar(s) in this dataset sit on a wall clock that "
        f"the autumn clock change plays twice. The vendor publishes local time with no "
        f"UTC offset, so the two passes of that hour are genuinely indistinguishable and "
        f"land on the same instant by construction. No de-duplication rule can recover "
        f"them; only an offset from the vendor can."
    )


def _lenient_rule(bars: int) -> SuggestedRule:
    """Collapse-on-key, for a dataset whose duplicates all agree."""
    return SuggestedRule(
        rule_id="drop_exact_duplicates",
        kind="cleansing",
        params={
            "key": [C.CONTRACT, C.TS_UTC],
            "identical_on": _COMPARED,
            "keep": "first",
            "action": "drop the later copies",
        },
        rationale=(
            f"Collapse the {bars:,} duplicated bars on contract and timestamp, keeping "
            f"the first copy. This is safe precisely because every copy is identical — "
            f"no price, volume or open-interest figure changes, and the only thing lost "
            f"is a repetition that would otherwise double-count volume in every total "
            f"downstream. Apply it on the key, not on the whole row, so a genuine "
            f"contradiction arriving later is still caught rather than quietly absorbed. "
            f"Confidence is 100% because the duplicates are unanimous."
        ),
        confidence=1.0,
    )


def _strict_rule(bars: int, exact: int, conflicting: int, share: float) -> SuggestedRule:
    """Refuse-and-alert, for a dataset in which some duplicates disagree."""
    return SuggestedRule(
        rule_id="reject_conflicting_duplicates",
        kind="validation",
        params={
            "key": [C.CONTRACT, C.TS_UTC],
            "conflict_on": _COMPARED,
            "keep": "none",
            "action": "reject every copy and alert",
            "then": "request a corrected delivery for the affected instants",
            "exact_duplicates": "safe to collapse separately, keeping the first copy",
        },
        rationale=(
            f"Keep neither side of the {conflicting:,} contradictory bars. Picking one — "
            f"first, last, or highest volume — would put a number into the risk and P&L "
            f"chain that the data does not support, and nobody downstream would ever "
            f"know a guess had been made. Reject them, alert, and ask the vendor for a "
            f"corrected delivery for those instants. The other {exact:,} duplicated bars "
            f"are exact copies and can be collapsed safely under a separate cleansing "
            f"rule. Confidence is the share of duplicated bars that genuinely contradict "
            f"({conflicting:,} of {bars:,}); the strict rule is proposed even when that "
            f"share is small, because a permissive rule would silently keep whichever "
            f"contradictory copy happened to arrive first."
        ),
        confidence=share,
    )


def _totals(ctx: InsightContext) -> dict[str, int]:
    """Bars per duplicate shape, read from the check's own evidence."""
    findings = ctx.subset(_CHECK)
    totals = {
        "bars": 0,
        "exact_bars": 0,
        "conflicting_bars": 0,
        "duplicated_instants": 0,
        "largest_group": 0,
    }
    if findings.height == 0:
        return totals
    decoded = decode_evidence(findings, _EVIDENCE_FIELDS)
    conflicting = pl.col(_CONFLICTING).fill_null(value=False)
    totals["bars"] = count_rows(findings)
    totals["conflicting_bars"] = count_rows(decoded.filter(conflicting))
    totals["exact_bars"] = totals["bars"] - totals["conflicting_bars"]
    totals["duplicated_instants"] = int(decoded.get_column(_DISTINCT_TIMESTAMPS).sum() or 0)
    totals["largest_group"] = int(decoded.select(pl.col(_LARGEST_GROUP).max().fill_null(0)).item())
    return totals
