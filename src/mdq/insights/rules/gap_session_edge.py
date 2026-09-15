"""Rule 5 — the missing minutes are all missing on the same day of the week.

Rule 4 asks *what time* the holes happen; this one asks *which day*. They are different
questions with different answers, and only the second one catches a venue that keeps a
shorter Friday, closes early before a weekly settlement, or runs a weekly maintenance
window. A calendar effect like that is invisible in an hour-of-day histogram because it
is spread across the day, and invisible in a per-session view because each individual
session looks merely quiet.

So this rule bins every `intrabar_gap` finding by the **weekday of its session date**.
If one weekday holds at least half of all the holes, the honest reading is not "the feed
is unreliable" but "this venue trades a different schedule on that day, and our
expectation of a full session is wrong on it".

**Why half, and not a fifth.** Five weekdays means a uniform spread is 20% each, so the
obvious gate would be 30-40%. That gate would be wrong, and the real data says so: on the
vendor's own ESH26 minute file the busiest weekday (Friday) holds 27 of the 70 holes —
38.6% — with no closure involved at all, and ESZ25 reaches 33.3% the same way. Sessions
are not equally liquid across the week, so weekday skew arises on its own. The gate is
therefore set **above what unremarkable data already produces**: 50%, plus a floor of a
few holes and at least two weekdays to compare against, so a single quiet Friday cannot
be mistaken for a calendar.

**On the real sample it does not fire**, for exactly that reason, and it is meant not to.
The window it names is **local (Chicago) wall clock**, the frame an exchange's session is
defined in.
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
    local_hhmm,
    register_rule,
)
from mdq.insights.models import Insight, SuggestedRule

__all__ = ["GapSessionEdgeCluster"]

_CHECK: Final = "intrabar_gap"
_START_MINUTE: Final = "gap_start_minute_of_day"
_GAP_MINUTES: Final = "gap_minutes"
_WEEKDAY: Final = "_weekday"
_END_MINUTE: Final = "_end_minute"
_GAPS: Final = "gaps"
_CLOSE_AT: Final = "close_at"
_OPEN_AT: Final = "open_at"

_EVIDENCE_FIELDS: Final[dict[str, pl.DataType]] = {
    _START_MINUTE: pl.Int64(),
    _GAP_MINUTES: pl.Int64(),
}

#: The shape `_weekday_bins` returns when there is nothing to bin, so `applies` and
#: `build` read the same columns whether or not any hole was found.
_BIN_SCHEMA: Final[pl.Schema] = pl.Schema(
    [
        (_WEEKDAY, pl.String),
        (_GAPS, pl.UInt32),
        (_CLOSE_AT, pl.Int64),
        (_OPEN_AT, pl.Int64),
    ]
)


def _empty_bins() -> pl.DataFrame:
    """No holes to bin — an empty frame in the shape `build` expects."""
    return pl.DataFrame(schema=_BIN_SCHEMA)


@register_rule
class GapSessionEdgeCluster:
    """Intraday holes concentrated on one weekday — a calendar, not an outage."""

    id: ClassVar[str] = "gaps_cluster_by_weekday"
    title: ClassVar[str] = "One weekday keeps a different schedule"

    #: Share of all holes that must fall on a single weekday. Set above the 38.6% that
    #: the real ESH26 minute file reaches through liquidity alone (see module docstring).
    min_share: ClassVar[float] = 0.5
    #: Below this, "half the holes are on a Friday" can be two holes and a coincidence.
    min_gaps: ClassVar[int] = 5
    #: There must be other weekdays to be unlike. A dataset covering one weekday
    #: trivially has 100% of its holes on it, and that says nothing about a calendar.
    min_weekdays: ClassVar[int] = 2

    def applies(self, ctx: InsightContext) -> bool:
        """True when enough holes exist, spread over enough weekdays, to cluster on one."""
        bins = _weekday_bins(ctx)
        if bins.height < self.min_weekdays:
            return False
        gaps = bins.get_column(_GAPS)
        total = int(gaps.sum())
        return total >= self.min_gaps and evidence_share(int(gaps[0]), total) >= self.min_share

    def build(self, ctx: InsightContext) -> Insight:
        """Name the weekday and its observed trading window, and propose a calendar."""
        findings = ctx.subset(_CHECK)
        bins = _weekday_bins(ctx)
        top = bins.row(0, named=True)
        weekday = str(top[_WEEKDAY])
        total_gaps = int(bins.get_column(_GAPS).sum())
        on_day = int(top[_GAPS])
        share = evidence_share(on_day, total_gaps)
        closes_at = local_hhmm(int(top[_CLOSE_AT]))
        reopens_at = local_hhmm(int(top[_OPEN_AT]))
        minutes_missing = count_rows(findings)
        exchange = ctx.exchange
        contracts = ctx.contracts_in(findings)

        pattern = (
            f"{total_gaps:,} holes were found inside sessions that were otherwise busy, "
            f"accounting for {minutes_missing:,} missing minute bars. {on_day:,} of them "
            f"({share:.0%}) fall on a single day of the week: {weekday}. On a typical "
            f"{weekday} the data stops around {closes_at} Chicago time and does not come "
            f"back until {reopens_at}. A feed that fails at random does not check the "
            f"calendar first. The far likelier reading is that this venue keeps a "
            f"different schedule on {weekday}s — an early close, a weekly settlement "
            f"break or scheduled maintenance — and that the tool is expecting a full "
            f"session on a day that never has one."
        )
        rationale = (
            f"Record {weekday}'s real trading hours for "
            f"{exchange or 'this venue'} — closing at {closes_at} and reopening at "
            f"{reopens_at} — instead of reporting the same absence {on_day:,} times. That "
            f"removes {share:.0%} of the gap findings as expected behaviour and leaves "
            f"the {total_gaps - on_day:,} holes on other weekdays, which are the ones "
            f"worth raising with the vendor. Confirm the hours against the exchange's "
            f"published schedule before adopting them: this is observed from your data, "
            f"not read from a calendar. Confidence is the share of holes that fall on "
            f"{weekday} ({on_day:,} of {total_gaps:,})."
        )
        return Insight(
            id=self.id,
            title=self.title,
            pattern=pattern,
            evidence={
                "check_id": _CHECK,
                "gap_count": total_gaps,
                "bars_affected": minutes_missing,
                "gaps_on_busiest_weekday": on_day,
                "busiest_weekday": weekday,
                "observed_close_local": closes_at,
                "observed_reopen_local": reopens_at,
                "share_on_busiest_weekday": round(share, 4),
                "min_share_threshold": self.min_share,
                "gaps_by_weekday": {
                    str(row[_WEEKDAY]): int(row[_GAPS]) for row in bins.iter_rows(named=True)
                },
                "affected_contract_count": ctx.contract_total(findings),
            },
            affected_contracts=contracts,
            finding_count=findings.height,
            rule=SuggestedRule(
                rule_id="session_calendar",
                kind="validation",
                params={
                    "exchange": exchange,
                    "timezone": "America/Chicago",
                    "sessions": [
                        {"weekday": weekday, "close": closes_at, "open": reopens_at},
                    ],
                    "effect": (
                        "expect no bars between close and open on that weekday; "
                        "do not report their absence as a gap"
                    ),
                    "applies_to": ctx.roots_in(findings),
                    "verify_against": "the exchange's published trading calendar",
                },
                rationale=rationale,
                confidence=share,
            ),
        )


def _weekday_bins(ctx: InsightContext) -> pl.DataFrame:
    """Holes per weekday, busiest first, with that weekday's typical closed window.

    The window is taken from the **median** hole on the weekday rather than its extremes:
    a genuine closure produces the same start and end every week, so the median is that
    time exactly, while one unusually long outage cannot stretch the suggested calendar.
    """
    findings = ctx.subset(_CHECK)
    if findings.height == 0:
        return _empty_bins()
    decoded = decode_evidence(findings, _EVIDENCE_FIELDS).drop_nulls(
        [_START_MINUTE, C.SESSION_DATE]
    )
    if decoded.height == 0:
        return _empty_bins()
    return (
        decoded.with_columns(
            pl.col(C.SESSION_DATE).dt.to_string("%A").alias(_WEEKDAY),
            (pl.col(_START_MINUTE) + pl.col(_GAP_MINUTES).fill_null(0)).alias(_END_MINUTE),
        )
        .group_by(_WEEKDAY)
        .agg(
            pl.len().cast(pl.UInt32).alias(_GAPS),
            pl.col(_START_MINUTE).median().cast(pl.Int64).alias(_CLOSE_AT),
            pl.col(_END_MINUTE).median().cast(pl.Int64).alias(_OPEN_AT),
        )
        .sort([_GAPS, _WEEKDAY], descending=[True, False])
    )
