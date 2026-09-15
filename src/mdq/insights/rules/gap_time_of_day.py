"""Rule 4 — the missing minutes are always missing at the same time of day.

A hole in an intraday series is ambiguous on its own: it could be an outage, or it could
be the market being shut. The way to tell them apart is to ask *when* the holes happen.
Outages are scattered through the day; a closure happens at the same clock time every
day, because that is what a closure is.

So this rule bins every `intrabar_gap` finding by the local hour its hole starts in. If
one hour holds a large share of all the holes, the honest conclusion is not "the feed
dropped out hundreds of times" but "this venue is closed during that hour and our
expectation of continuous data is wrong" — and the fix is a configured session-break
window, after which those holes stop being reported at all and the genuine outages
become visible.

**On the real sample it does not fire, and that is the right answer.** `intrabar_gap` is
a minute-frequency check, so this rule is silent on the 40 daily files that carry every
other real finding. Run against the full real minute files it stays silent too, and the
measurement is the reason to trust it: ESH26 has 70 holes inside ACTIVE sessions and the
busiest single hour holds 11 of them (16%); ESZ25 19%, ESM25 12%. The holes are spread
right across the overnight session, which is exactly what thin overnight liquidity looks
like — genuinely missing minutes, not a closure. A rule that announced a session break
here would be inventing one. It fires on a venue that really does shut mid-session, for
which the fault harness supplies the fixture.

The clock time it reports is **local (Chicago) wall clock**, because that is the frame
in which an exchange's session is defined and the one a trader reads.
"""

from __future__ import annotations

from typing import ClassVar, Final

import polars as pl

from mdq.insights.engine import (
    InsightContext,
    count_rows,
    decode_evidence,
    evidence_share,
    local_hhmm,
    register_rule,
)
from mdq.insights.models import Insight, SuggestedRule

__all__ = ["GapTimeOfDayCluster"]

_CHECK: Final = "intrabar_gap"
_START_MINUTE: Final = "gap_start_minute_of_day"
_GAP_MINUTES: Final = "gap_minutes"
_MISSING: Final = "minutes_missing"
_HOUR: Final = "_hour"
_END_MINUTE: Final = "_end_minute"

_EVIDENCE_FIELDS: Final[dict[str, pl.DataType]] = {
    _START_MINUTE: pl.Int64(),
    _GAP_MINUTES: pl.Int64(),
    _MISSING: pl.Int64(),
}

#: The shape `_hourly_bins` returns when there is nothing to bin. Declaring it once means
#: `applies` and `build` read `.height` and `"gaps"` on the same frame either way, rather
#: than each guarding against a different flavour of "no data".
_BIN_SCHEMA: Final[pl.Schema] = pl.Schema(
    [
        (_HOUR, pl.Int64),
        ("gaps", pl.UInt32),
        ("first_start", pl.Int64),
        ("last_end", pl.Int64),
    ]
)


def _empty_bins() -> pl.DataFrame:
    """No holes to bin — an empty frame in the shape `build` expects."""
    return pl.DataFrame(schema=_BIN_SCHEMA)


@register_rule
class GapTimeOfDayCluster:
    """Intraday holes concentrated in one hour of the local trading day."""

    id: ClassVar[str] = "gaps_cluster_by_time_of_day"
    title: ClassVar[str] = "The data stops at the same time every day"

    #: Share of all holes that must start in a single hour before the clustering is
    #: called a closure rather than coincidence. With 23 candidate hours in a CME
    #: session, 30% in one of them is far from uniform.
    min_share: ClassVar[float] = 0.3

    def applies(self, ctx: InsightContext) -> bool:
        """True when holes exist and enough of them start in one hour."""
        bins = _hourly_bins(ctx)
        if bins.height == 0:
            return False
        gaps = bins.get_column("gaps")
        return evidence_share(int(gaps[0]), int(gaps.sum())) >= self.min_share

    def build(self, ctx: InsightContext) -> Insight:
        """Name the window, and propose configuring it instead of reporting it."""
        findings = ctx.subset(_CHECK)
        bins = _hourly_bins(ctx)
        top = bins.row(0, named=True)
        total_gaps = int(bins.get_column("gaps").sum())
        share = evidence_share(int(top["gaps"]), total_gaps)
        window_start = local_hhmm(int(top["first_start"]))
        window_end = local_hhmm(int(top["last_end"]))
        minutes_missing = count_rows(findings)
        exchange = ctx.exchange
        contracts = ctx.contracts_in(findings)

        pattern = (
            f"{total_gaps:,} holes were found inside sessions that were otherwise busy, "
            f"accounting for {minutes_missing:,} missing minute bars. They are not "
            f"spread through the day: {int(top['gaps']):,} of them ({share:.0%}) begin "
            f"between {window_start} and {window_end} Chicago time. A feed that drops "
            f"out at random does not keep office hours. The overwhelmingly likely reading "
            f"is that this venue simply does not trade during that window, and the tool "
            f"is expecting data that was never meant to exist."
        )
        rationale = (
            f"Configure {window_start}–{window_end} as a scheduled break for "
            f"{exchange or 'this venue'} rather than reporting it {int(top['gaps']):,} "
            f"times. Doing so removes {share:.0%} of the gap findings as noise and leaves "
            f"the {total_gaps - int(top['gaps']):,} holes that occur while the market was "
            f"genuinely open — the ones that are worth chasing with the vendor. "
            f"Confidence is the share of holes that start inside the window."
        )
        return Insight(
            id=self.id,
            title=self.title,
            pattern=pattern,
            evidence={
                "check_id": _CHECK,
                "gap_count": total_gaps,
                "bars_affected": minutes_missing,
                "gaps_in_window": int(top["gaps"]),
                "window_local": f"{window_start}-{window_end}",
                "busiest_local_hour": f"{int(top[_HOUR]):02d}:00",
                "share_in_window": round(share, 4),
                "min_share_threshold": self.min_share,
                "gaps_by_local_hour": {
                    f"{int(row[_HOUR]):02d}:00": int(row["gaps"])
                    for row in bins.sort(_HOUR).iter_rows(named=True)
                },
                "affected_contract_count": ctx.contract_total(findings),
            },
            affected_contracts=contracts,
            finding_count=findings.height,
            rule=SuggestedRule(
                rule_id="session_break_window",
                kind="validation",
                params={
                    "exchange": exchange,
                    "timezone": "America/Chicago",
                    "break_local": {"start": window_start, "end": window_end},
                    "effect": "exclude this window from intraday gap detection",
                    "applies_to": ctx.roots_in(findings),
                },
                rationale=rationale,
                confidence=share,
            ),
        )


def _hourly_bins(ctx: InsightContext) -> pl.DataFrame:
    """Gaps per local hour-of-day, busiest first, with the window each hour spans."""
    findings = ctx.subset(_CHECK)
    if findings.height == 0:
        return _empty_bins()
    decoded = decode_evidence(findings, _EVIDENCE_FIELDS).drop_nulls(_START_MINUTE)
    if decoded.height == 0:
        return _empty_bins()
    return (
        decoded.with_columns(
            (pl.col(_START_MINUTE) // 60).alias(_HOUR),
            (pl.col(_START_MINUTE) + pl.col(_GAP_MINUTES).fill_null(0)).alias(_END_MINUTE),
        )
        .group_by(_HOUR)
        .agg(
            pl.len().cast(pl.UInt32).alias("gaps"),
            pl.col(_START_MINUTE).min().alias("first_start"),
            pl.col(_END_MINUTE).max().alias("last_end"),
        )
        .sort(["gaps", _HOUR], descending=[True, False])
    )
