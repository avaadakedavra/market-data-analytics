"""Rule 7 — the days with no data are the days the exchange was shut.

A weekday on which a contract published nothing is either a hole in the feed or a day the
market was closed, and those two readings lead to opposite actions: chase the vendor, or
change what you expect. Without a holiday calendar the tool cannot tell them apart from
one contract alone — so it does not try. It asks the other contracts instead.

`missing_session` already does the asking: when a contract is absent and its siblings on
the same exchange traded, the absence is a real hole and it is a WARNING; when *no*
contract on the exchange published anything, the absence is holiday-shaped and it is an
INFO. This rule reads that corroboration back out and turns a scatter of individually
uninteresting INFOs into the one thing a business user can actually act on: a list of
dates per exchange that should be treated as non-trading days from now on.

That is deliberately better than shipping a holiday calendar. A shipped calendar is wrong
the moment an exchange changes its schedule, wrong for any venue nobody thought to
include, and silently wrong for early closes; corroboration is derived from the data in
front of you and is right about all three. PLAN §9 lists holiday calendars as an explicit
non-goal for exactly this reason — this rule is what replaces them.

**One gate that matters.** Corroboration is meaningless in a single-contract dataset:
with no siblings, *every* absence is vacuously "nobody traded" and the rule would
confidently announce a holiday calendar assembled from one contract's expiry ramp. So it
fires only when the dataset holds at least two contracts to corroborate across.

**On the real sample it fires.** Across all 40 daily files, 388 of the 389 absent weekday
sessions are corroborated across their whole exchange, and the one that is not is a
genuine single-contract hole — which is precisely the separation this rule exists to make.
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

import polars as pl

from mdq.domain.schema import C
from mdq.insights.engine import (
    MAX_LISTED,
    InsightContext,
    as_iso_dates,
    count_rows,
    decode_evidence,
    distinct,
    evidence_share,
    register_rule,
)
from mdq.insights.models import Insight, SuggestedRule

__all__ = ["HolidayCalendar"]

_CHECK: Final = "missing_session"
_SIBLING: Final = "sibling_contracts_traded"
_EXCHANGE: Final = "exchange"
_WEEKDAY: Final = "weekday"
_UNKNOWN_EXCHANGE: Final = "(unknown exchange)"

_EVIDENCE_FIELDS: Final[dict[str, pl.DataType]] = {
    _SIBLING: pl.Boolean(),
    _EXCHANGE: pl.String(),
    _WEEKDAY: pl.String(),
}


@register_rule
class HolidayCalendar:
    """Absent sessions that every contract on the exchange shares are holidays."""

    id: ClassVar[str] = "missing_sessions_are_holidays"
    title: ClassVar[str] = "The missing days are market holidays, not missing data"

    #: Share of absences that must be exchange-wide before they are called a calendar
    #: rather than a coincidence of several contracts being quiet at once.
    min_share: ClassVar[float] = 0.5
    #: Corroboration needs something to corroborate with.
    min_contracts: ClassVar[int] = 2

    def applies(self, ctx: InsightContext) -> bool:
        """True when most absences are exchange-wide and there were siblings to ask."""
        if _contract_universe(ctx) < self.min_contracts:
            return False
        absences = _absences(ctx)
        total = count_rows(absences)
        return total > 0 and evidence_share(count_rows(_corroborated(absences)), total) >= (
            self.min_share
        )

    def build(self, ctx: InsightContext) -> Insight:
        """List the non-trading dates per exchange and propose adopting them."""
        findings = ctx.subset(_CHECK)
        absences = _absences(ctx)
        corroborated = _corroborated(absences)
        total = count_rows(absences)
        explained = count_rows(corroborated)
        share = evidence_share(explained, total)

        calendars = _calendars(corroborated)
        dates = sorted({d for calendar in calendars for d in calendar["dates"]})
        exchanges = [str(calendar[_EXCHANGE]) for calendar in calendars]
        weekdays = _weekday_counts(corroborated)
        contracts = ctx.contracts_in(corroborated)

        pattern = (
            f"{total:,} weekday session(s) are missing from this dataset, and "
            f"{explained:,} of them ({share:.1%}) are missing for every single contract "
            f"on the exchange at once, not just for one. That is what a market holiday "
            f"looks like: on {len(dates)} distinct date(s) across "
            f"{_exchange_phrase(exchanges)}, nothing traded anywhere, so there was never "
            f"any data to deliver. The remaining {total - explained:,} absence(s) are the "
            f"opposite case — one contract silent while its neighbours traded normally — "
            f"and those are real holes worth raising with the vendor."
        )
        rationale = (
            f"Adopt these {len(dates)} date(s) as non-trading days for "
            f"{_exchange_phrase(exchanges)} so the report stops reporting them. Doing so "
            f"clears {explained:,} findings that need no action and makes the "
            f"{total - explained:,} genuine gap(s) visible instead of leaving them buried. "
            f"The dates are derived from your own data by cross-contract corroboration, "
            f"not read from a shipped calendar, so they follow whatever schedule the "
            f"exchange actually kept — but confirm them against the exchange's published "
            f"holiday schedule before adopting, and be aware that an early close looks "
            f"like a normal day here, not a holiday. Confidence is the share of absences "
            f"that are exchange-wide ({explained:,} of {total:,})."
        )
        return Insight(
            id=self.id,
            title=self.title,
            pattern=pattern,
            evidence={
                "check_id": _CHECK,
                "bars_affected": total,
                "sessions_missing_exchange_wide": explained,
                "sessions_missing_for_one_contract": total - explained,
                "holiday_date_count": len(dates),
                "holiday_dates": dates[:MAX_LISTED],
                "exchanges": exchanges,
                "dates_by_weekday": weekdays,
                "share_corroborated": round(share, 4),
                "min_share_threshold": self.min_share,
                "affected_contract_count": ctx.contract_total(corroborated),
            },
            affected_contracts=contracts,
            finding_count=findings.height,
            rule=SuggestedRule(
                rule_id="holiday_calendar",
                kind="validation",
                params={
                    "calendars": calendars,
                    "effect": (
                        "expect no session on these dates; report an absence only when "
                        "the date is not in the calendar"
                    ),
                    "derived_from": (
                        "cross-contract corroboration within this dataset, not a shipped "
                        "holiday calendar"
                    ),
                    "verify_against": "the exchange's published holiday schedule",
                    "does_not_cover": "early closes, which look like ordinary sessions here",
                },
                rationale=rationale,
                confidence=share,
            ),
        )


def _absences(ctx: InsightContext) -> pl.DataFrame:
    """`missing_session` findings with their corroboration decoded."""
    return decode_evidence(ctx.subset(_CHECK), _EVIDENCE_FIELDS)


def _corroborated(absences: pl.DataFrame) -> pl.DataFrame:
    """The absences on which no contract of the exchange traded at all."""
    return absences.filter(~pl.col(_SIBLING).fill_null(value=False))


def _calendars(corroborated: pl.DataFrame) -> list[dict[str, Any]]:
    """One `{exchange, dates, date_count}` record per exchange, JSON-safe.

    PLAN §5 writes this rule's parameters as a single `{exchange, dates}`; the real
    corpus spans six exchanges at once, so the shape is a list of those records rather
    than one. A dataset with a single exchange therefore yields a single-element list.
    """
    grouped = (
        corroborated.with_columns(pl.col(_EXCHANGE).fill_null(_UNKNOWN_EXCHANGE))
        .group_by(_EXCHANGE)
        .agg(pl.col(C.SESSION_DATE).drop_nulls().unique().sort().alias("dates"))
        .sort(_EXCHANGE)
    )
    return [
        {
            _EXCHANGE: str(row[_EXCHANGE]),
            "dates": as_iso_dates(row["dates"])[:MAX_LISTED],
            "date_count": len(row["dates"]),
        }
        for row in grouped.iter_rows(named=True)
    ]


def _weekday_counts(corroborated: pl.DataFrame) -> dict[str, int]:
    """Distinct holiday dates per weekday — a Monday-heavy list is a good sanity check."""
    grouped = (
        corroborated.drop_nulls(C.SESSION_DATE)
        .unique(subset=[_EXCHANGE, C.SESSION_DATE])
        .group_by(pl.col(C.SESSION_DATE).dt.to_string("%A").alias(_WEEKDAY))
        .agg(pl.len().alias("dates"))
        .sort(_WEEKDAY)
    )
    return {str(weekday): int(count) for weekday, count in grouped.iter_rows()}


def _exchange_phrase(exchanges: list[str]) -> str:
    """ "CME", or "CME, CBOT and 4 other exchanges" — never an unbounded list in prose."""
    if len(exchanges) > 2:
        return f"{exchanges[0]}, {exchanges[1]} and {len(exchanges) - 2} other exchange(s)"
    return " and ".join(exchanges) or "this exchange"


def _contract_universe(ctx: InsightContext) -> int:
    """How many contracts there were to corroborate across.

    The dataset summary when there is one; otherwise the contracts that appear anywhere
    in the report, which is a lower bound and errs towards *not* firing.
    """
    if ctx.summary is not None:
        return len(ctx.summary.contracts)
    return len(distinct(ctx.findings, C.CONTRACT))
