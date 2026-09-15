"""Rule 3 ★ — one bad date broke a whole product family at once.

This is the most distinctive thing in the dataset, and the clearest example of a
judgement a business user cannot make from a findings table.

All 43 incoherent-OHLC bars in the corpus fall on just **11 dates**, and they do not
arrive one contract at a time. Measured across all 40 daily files:

| date | root | contracts hit |
|---|---|---|
| 2021-12-02 | CL | 5 |
| 2021-12-16 | CL | 5 |
| 2021-12-22 | CL | 5 |
| 2022-01-13 | CL | 5 |
| 2021-11-11 | CL | 4 |
| 2021-11-18 | CL | 4 |

Six dates on which **every crude-oil contract in the dataset broke simultaneously** —
28 of the 43 bars. Three of those dates hit seven contracts once ES and ZC are counted
too. Five separate contracts, with five separate order books and five separate
settlement committees, do not independently corrupt the same field on the same day. One
upstream file did.

That distinction changes who gets paged. Per-contract corruption is a data-entry
problem: fix the rows. A feed-level incident is a vendor problem: quarantine the date
for the whole family, ask the vendor for a re-delivery, and check every other product
that came down the same pipe on that date. It also changes the blast radius a risk
manager has to assume — if the crude curve was wrong on 2022-01-13, every spread,
every margin calculation and every mark against it was wrong on 2022-01-13 too.

**How the pattern is recognised.** For every (check, session date, product root), count
how many distinct contracts of that root the check fired on. Three or more is an
incident. Two gates keep it honest:

* **Severity ≥ WARNING only.** This is what stops the rule drowning in the ordinary. On
  the real corpus 14,152 flat bars, 400 zero-volume bars and 388 absent sessions are all
  INFO — downgraded by the activity-regime model because nothing was trading — and every
  one of them would otherwise look like a "whole family affected on one date" every
  single day. Applying the gate leaves exactly the 6 incidents above and nothing else.
  It needs no hardcoded list of checks, so a check added later is judged on the same
  terms as the fourteen that exist.
* **Three contracts, not two.** Two contracts of a root sharing a date is an
  unremarkable coincidence in a dataset with five contracts per root; three is not.
"""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar, Final

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.schema import C
from mdq.insights.engine import (
    MAX_LISTED,
    ROOT,
    InsightContext,
    count_rows,
    evidence_share,
    register_rule,
)
from mdq.insights.models import Insight, SuggestedRule

__all__ = ["FeedIncidentCluster"]

_CONTRACTS: Final = "_contracts"
_CONTRACT_COUNT: Final = "_contract_count"
_ROWS: Final = "_rows"
#: Checks about the *absence* of data are excluded: a date on which every contract of a
#: root is missing is a holiday, which `holiday_calendar` (rule 7) explains properly.
_EXCLUDED_CHECKS: Final = frozenset({"missing_session", "malformed_record", "check_failed"})


@register_rule
class FeedIncidentCluster:
    """A date on which one check fired across three or more contracts of one root."""

    id: ClassVar[str] = "feed_incident_date_cluster"
    title: ClassVar[str] = "Whole product families broke on the same dates"

    #: How many contracts of one root must break together before coincidence is ruled
    #: out. Three, because the corpus holds five contracts per root and two overlapping
    #: is unremarkable.
    min_contracts: ClassVar[int] = 3
    #: Findings below this severity are explained by the activity model, not by a feed
    #: fault, and must not be mistaken for one.
    min_severity: ClassVar[Severity] = Severity.WARNING

    def applies(self, ctx: InsightContext) -> bool:
        """True when at least one (date, root, check) triple crosses the threshold."""
        return _incidents(ctx, self.min_contracts, self.min_severity).height > 0

    def build(self, ctx: InsightContext) -> Insight:
        """Name the dates, the family and the contracts, and propose a quarantine."""
        incidents = _incidents(ctx, self.min_contracts, self.min_severity)
        checks = sorted({str(row) for row in incidents.get_column(C.CHECK_ID).to_list()})
        relevant = ctx.subset(checks, min_severity=self.min_severity)
        explained = int(incidents.get_column(_ROWS).sum() or 0)
        total = count_rows(relevant)
        share = evidence_share(explained, total)

        details = _details(ctx, incidents)
        dates = sorted({row["session_date"] for row in details})
        roots = sorted({row["root"] for row in details})
        contracts = sorted({c for row in details for c in row["contracts"]})
        worst = max(details, key=lambda row: (row["contract_count"], row["session_date"]))
        family = _family_phrase(ctx, worst)

        pattern = (
            f"{explained:,} of the {total:,} defects worth a person's attention are not "
            f"scattered across the history at all: they land on just {len(dates)} "
            f"date(s), and on each of those dates they strike several contracts of the "
            f"same product at the same moment. On {worst['session_date']}, "
            f"{worst['contract_count']} {worst['root']} contracts "
            f"({', '.join(worst['contracts'][:6])}) all failed the same check "
            f"({worst['check_id']}) simultaneously{family}. Separate contracts have "
            f"separate order books and separate settlements; they do not go wrong "
            f"together by chance. This is one bad delivery from the data feed, not "
            f"{explained:,} independent bad prices."
        )
        rationale = (
            f"Fixing {explained:,} rows one at a time treats the symptom and leaves the "
            f"cause in place. Quarantine the affected product/date combinations instead: "
            f"hold {', '.join(roots)} for {len(dates)} date(s), request a re-delivery "
            f"from the vendor for those dates, and check every other product that came "
            f"down the same pipe on them. Anything already marked against those "
            f"dates — spreads, margin, P&L — was marked against a bad curve and should "
            f"be re-run. Confidence is the share of the serious findings that fall "
            f"inside an incident ({explained:,} of {total:,}); the remainder are "
            f"isolated single-contract defects that need handling individually."
        )
        return Insight(
            id=self.id,
            title=self.title,
            pattern=pattern,
            evidence={
                "checks": checks,
                "incident_count": len(details),
                "incident_dates": [str(d) for d in dates[:MAX_LISTED]],
                "incident_date_count": len(dates),
                "roots": roots,
                "incidents": details[:MAX_LISTED],
                "bars_in_incidents": explained,
                "bars_affected": total,
                "share_in_incidents": round(share, 4),
                "min_contracts_threshold": self.min_contracts,
                "min_severity_threshold": self.min_severity.label,
                "affected_contract_count": len(contracts),
            },
            affected_contracts=contracts[:MAX_LISTED],
            finding_count=int(relevant.height),
            rule=SuggestedRule(
                rule_id="cross_contract_date_quarantine",
                kind="validation",
                params={
                    "quarantine": [
                        {
                            "root": root,
                            "dates": sorted(
                                {row["session_date"] for row in details if row["root"] == root}
                            ),
                        }
                        for root in roots
                    ],
                    "checks": checks,
                    "trigger": (
                        f"one check fails on >= {self.min_contracts} contracts of a "
                        f"single root on one session date, at severity "
                        f"{self.min_severity.label} or above"
                    ),
                    "action": "hold the product for the date and request re-delivery",
                    "escalate_to": "data vendor, not the contract owner",
                    "also_review": "every other product delivered in the same file batch",
                },
                rationale=rationale,
                confidence=share,
            ),
        )


def _incidents(ctx: InsightContext, min_contracts: int, min_severity: Severity) -> pl.DataFrame:
    """(check, date, root) triples on which enough contracts broke together."""
    serious = [s.label for s in Severity if s >= min_severity]
    findings = ctx.findings.filter(
        ~pl.col(C.CHECK_ID).is_in(list(_EXCLUDED_CHECKS)) & pl.col(C.SEVERITY).is_in(serious)
    )
    return (
        findings.drop_nulls([C.SESSION_DATE, C.CONTRACT])
        .group_by(C.CHECK_ID, C.SESSION_DATE, ROOT)
        .agg(
            pl.col(C.CONTRACT).n_unique().cast(pl.UInt32).alias(_CONTRACT_COUNT),
            pl.col(C.CONTRACT).unique().sort().alias(_CONTRACTS),
            pl.col(C.COUNT).sum().cast(pl.UInt32).alias(_ROWS),
        )
        .filter(pl.col(_CONTRACT_COUNT) >= min_contracts)
        .sort([_CONTRACT_COUNT, C.SESSION_DATE], descending=[True, False])
    )


def _details(ctx: InsightContext, incidents: pl.DataFrame) -> list[dict[str, Any]]:
    """One JSON-safe record per incident, including the collateral damage elsewhere.

    `contracts_other_roots` is what turns "the CL family broke" into "the delivery that
    day was bad": on 2022-01-13 an ES and a ZC contract failed the same check on the same
    date, which no per-product explanation covers.
    """
    records: list[dict[str, Any]] = []
    for row in incidents.iter_rows(named=True):
        session: date = row[C.SESSION_DATE]
        others = _other_roots(ctx, row[C.CHECK_ID], session, row[ROOT])
        records.append(
            {
                "check_id": row[C.CHECK_ID],
                "session_date": session.isoformat(),
                "root": row[ROOT],
                "contract_count": int(row[_CONTRACT_COUNT]),
                "contracts": [str(c) for c in row[_CONTRACTS]],
                "bars": int(row[_ROWS]),
                "contracts_other_roots": others,
            }
        )
    return records


def _other_roots(ctx: InsightContext, check_id: str, session: date, root: str) -> list[str]:
    """Contracts of *other* products that failed the same check on the same date."""
    same_day = ctx.findings.filter(
        (pl.col(C.CHECK_ID) == check_id)
        & (pl.col(C.SESSION_DATE) == session)
        & (pl.col(ROOT) != root)
    )
    return [str(c) for c in ctx.contracts_in(same_day)]


def _family_phrase(ctx: InsightContext, worst: dict[str, Any]) -> str:
    """ " — every one of the N CL contracts held", when the summary knows N."""
    size = ctx.family_size(str(worst["root"]))
    others = worst["contracts_other_roots"]
    parts: list[str] = []
    if size is not None:
        parts.append(f" — {worst['contract_count']} of the {size} {worst['root']} contracts held")
    if others:
        joined = ", ".join(others[:4])
        parts.append(f", and {joined} failed the same check on the same date")
    return "".join(parts)
