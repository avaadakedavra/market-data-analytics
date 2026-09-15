"""The insights layer against committed slices of the vendor's real files.

Every other insight test asserts against data we broke on purpose. These assert against
data the vendor shipped — `CLG26_daily.parquet` carries six genuine OHLC violations and
`ESH26_daily.parquet` carries none — so they catch a rule that is confidently,
self-consistently wrong about real market data.

The two files are chosen to be each other's control. CLG26 is dormant-heavy: 81% of its
daily bars are flat and 71% are untraded outright, and all six of its incoherent bars
carry the corpus's 39-of-43 settlement signature. ESH26 is liquid and clean. A rule that
fired on both would be describing nothing.
"""

from __future__ import annotations

import json

import pytest

from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.ingest import ingest_file
from mdq.insights import DatasetSummary, Insight, InsightContext, RuleBasedInsightEngine
from mdq.quality import CheckContext, run_checks
from support.fixtures import CLG26_DAILY, ESH26_DAILY, vendor_diagnostics

pytestmark = pytest.mark.golden


def _context(path) -> InsightContext:
    """Ingest a committed vendor file, run every check, and wrap it for the rules."""
    result = ingest_file(path)
    ctx = CheckContext.build(result.bars, rejects=result.rejects)
    report = run_checks(result.bars, ctx)
    return InsightContext(
        report=report,
        activity=ctx.activity,
        summary=DatasetSummary.from_bars(result.bars),
    )


def _derive(ctx: InsightContext) -> dict[str, Insight]:
    result = RuleBasedInsightEngine().derive_with_failures(ctx.report, ctx.activity, ctx.summary)
    assert not result.failures, f"rules raised: {[f.to_dict() for f in result.failures]}"
    return {insight.id: insight for insight in result.insights}


def test_the_fixtures_still_carry_the_violations_these_tests_are_about() -> None:
    """A guard on everything below: if the fixtures were regenerated differently, the
    assertions that follow would quietly become vacuous."""
    vendor = vendor_diagnostics("daily")
    counts = {
        row["contract_symbol"]: row["invalid_ohlc_row_count"]
        for row in vendor.to_dicts()
        if row["contract_symbol"] in {"CLG26", "ESH26"}
    }
    assert counts == {"CLG26": 6, "ESH26": 0}


def test_the_settlement_signature_is_read_off_the_real_violations() -> None:
    """CLG26's six incoherent bars are the corpus pattern in miniature: open, high and low
    carried forward on a non-trading day while the close moved to the new settlement."""
    insight = _derive(_context(CLG26_DAILY))["carried_forward_settlement"]

    assert insight.evidence["bars_affected"] == 6
    assert insight.evidence["bars_with_carried_forward_range"] == 6
    assert insight.evidence["bars_with_zero_volume"] == 6
    assert insight.evidence["bars_without_the_signature"] == 0
    # `high < low` does not occur anywhere in the real corpus — 0 of 43.
    assert insight.evidence["clause_high_below_low"] == 0
    assert insight.confidence == 1.0
    assert insight.affected_contracts == ["CLG26"]
    assert insight.rule.rule_id == "ohlc_bounds"


def test_dormancy_explains_the_flat_bars_in_the_real_file() -> None:
    """1,629 no-range bars in one contract, and 1,628 of them on a day it did not trade.
    This is the insight that makes the tool usable on this dataset at all.

    The one that is left over is the point of reporting the split rather than the total:
    it sits in a session CLG26 genuinely traded, and a desk should see exactly that one
    rather than all 1,629."""
    insight = _derive(_context(CLG26_DAILY))["dormancy_explains_staleness"]

    assert insight.evidence["bars_affected"] == 1_629
    assert insight.evidence["bars_in_dormant_sessions"] == 1_411
    assert insight.evidence["bars_in_thin_sessions"] == 217
    assert insight.evidence["bars_in_active_sessions"] == 1
    assert insight.confidence == 0.9994
    assert insight.rule.rule_id == "classify_settlement_only_bars"
    assert insight.rule.kind == "cleansing"


def test_a_clean_liquid_file_yields_no_defect_insights() -> None:
    """ESH26 has zero OHLC violations, zero duplicates and zero nulls, so the rules that
    explain those must all stay silent. Anything they said here would be invented."""
    insights = _derive(_context(ESH26_DAILY))

    assert "carried_forward_settlement" not in insights
    assert "duplicate_timestamp_shape" not in insights
    assert "bad_input_concentrates_in_one_field" not in insights
    assert "zero_volume_with_range_while_active" not in insights
    assert "daily_close_is_a_settlement_price" not in insights


def test_a_single_contract_never_produces_a_holiday_calendar() -> None:
    """Corroboration needs siblings. One file on its own cannot supply them, and the rule
    must not manufacture a calendar out of one contract's quiet days."""
    for path in (CLG26_DAILY, ESH26_DAILY):
        assert "missing_sessions_are_holidays" not in _derive(_context(path))


def test_a_single_contract_never_produces_a_feed_incident() -> None:
    """Three contracts of one root have to break together; one file cannot show that."""
    assert "feed_incident_date_cluster" not in _derive(_context(CLG26_DAILY))


def test_the_minute_only_rules_stay_silent_on_a_daily_file() -> None:
    insights = _derive(_context(CLG26_DAILY))
    assert "gaps_cluster_by_time_of_day" not in insights
    assert "gaps_cluster_by_weekday" not in insights


def test_real_insights_round_trip_through_json() -> None:
    """The acceptance property, on the real thing rather than on a constructed object."""
    for insight in _derive(_context(CLG26_DAILY)).values():
        restored = Insight.from_dict(json.loads(json.dumps(insight.to_dict())))
        assert restored == insight
        assert restored.rule.rationale
        assert restored.pattern


def test_every_real_insight_reads_as_prose_a_business_user_could_act_on() -> None:
    """`pattern` and `rationale` are read by traders and risk managers, not by us: they
    must be quantified sentences, not labels, and must not leak our internals."""
    for insight in _derive(_context(CLG26_DAILY)).values():
        assert len(insight.pattern) > 200
        assert len(insight.rule.rationale) > 200
        assert insight.pattern.rstrip().endswith(".")
        for text in (insight.title, insight.pattern, insight.rule.rationale):
            assert "DataFrame" not in text
            assert "None" not in text
            assert "_" not in text.replace("open_", "open ")


def test_an_empty_dataset_produces_no_insights_and_no_failures() -> None:
    from mdq.domain.schema import BarFrame

    bars = BarFrame.empty(Frequency.DAILY)
    report = run_checks(bars, CheckContext.build(bars))
    result = RuleBasedInsightEngine().derive_with_failures(report)
    assert result.insights == []
    assert result.failures == []


def test_the_report_the_rules_read_is_the_one_the_vendor_diagnostics_verify() -> None:
    """Ties the insight layer back to the external check: the findings these rules explain
    are the same rows the vendor published counts for."""
    ctx = _context(CLG26_DAILY)
    rows = {
        row[C.CHECK_ID]: row["rows"]
        for row in ctx.report.summary().to_dicts()
        if row[C.CHECK_ID] in {"invalid_ohlc", "stale_bar"}
    }
    assert rows["invalid_ohlc"] == 6
    assert rows["stale_bar"] == 1_629
