"""The rule registry, the context every rule reads, and the engine that isolates them.

Two promises are load-bearing here and both are tested directly: every rule PLAN §5 asks
for is discovered with no import list to forget, and one broken rule cannot cost the user
the other nine. The second is the reason `derive` exists at all rather than a loop over
`RuleRegistry.all()` at the call site.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import ClassVar

import polars as pl
import pytest

from mdq.domain.findings import Finding, Severity
from mdq.domain.frequency import Frequency
from mdq.insights import engine as engine_module
from mdq.insights.engine import (
    MAX_LISTED,
    ROOT,
    InsightContext,
    InsightResult,
    PatternRule,
    RuleBasedInsightEngine,
    RuleFailure,
    RuleRegistry,
    UnknownRuleError,
    as_iso_dates,
    count_rows,
    decode_evidence,
    distinct,
    evidence_share,
    local_hhmm,
    register_rule,
)
from mdq.insights.models import DatasetSummary, Insight, SuggestedRule
from mdq.quality import QualityReport
from support import insights as I
from support import scenarios

pytestmark = pytest.mark.unit

#: The ten rules PLAN §5 lists, by the id each module registers.
EXPECTED_RULES = {
    "dormancy_explains_staleness",
    "carried_forward_settlement",
    "feed_incident_date_cluster",
    "gaps_cluster_by_time_of_day",
    "gaps_cluster_by_weekday",
    "duplicate_timestamp_shape",
    "missing_sessions_are_holidays",
    "zero_volume_with_range_while_active",
    "bad_input_concentrates_in_one_field",
    "daily_close_is_a_settlement_price",
}


def _insight(rule_id: str, confidence: float) -> Insight:
    return Insight(
        id=rule_id,
        title="t",
        pattern="p",
        evidence={},
        affected_contracts=[],
        finding_count=1,
        rule=SuggestedRule(rule_id=rule_id, kind="validation", confidence=confidence),
    )


class _Exploding:
    """A rule that raises in `build` — the whole point is that it must not be fatal."""

    id: ClassVar[str] = "_exploding"
    title: ClassVar[str] = "Deliberately broken rule"

    def applies(self, ctx: InsightContext) -> bool:
        return True

    def build(self, ctx: InsightContext) -> Insight:
        raise RuntimeError("boom")


class _ExplodingApplies:
    """A rule that raises before it even decides whether it applies."""

    id: ClassVar[str] = "_exploding_applies"
    title: ClassVar[str] = "Broken even earlier"

    def applies(self, ctx: InsightContext) -> bool:
        raise ValueError("cannot decide")

    def build(self, ctx: InsightContext) -> Insight:  # pragma: no cover - never reached
        return _insight(self.id, 1.0)


class _WrongShape:
    """A rule that returns something that is not an `Insight`."""

    id: ClassVar[str] = "_wrong_shape"
    title: ClassVar[str] = "Returns the wrong type"

    def applies(self, ctx: InsightContext) -> bool:
        return True

    def build(self, ctx: InsightContext) -> Insight:
        return "not an insight"  # type: ignore[return-value]


class _Silent:
    """A well-behaved rule that finds nothing."""

    id: ClassVar[str] = "_silent"
    title: ClassVar[str] = "Finds nothing"

    def applies(self, ctx: InsightContext) -> bool:
        return False

    def build(self, ctx: InsightContext) -> Insight:  # pragma: no cover - never reached
        return _insight(self.id, 1.0)


class _Fixed:
    """A rule that always produces an insight at a fixed confidence."""

    id: ClassVar[str] = "_fixed"
    title: ClassVar[str] = "Always fires"
    confidence: ClassVar[float] = 0.4

    def applies(self, ctx: InsightContext) -> bool:
        return True

    def build(self, ctx: InsightContext) -> Insight:
        return _insight(self.id, self.confidence)


# --------------------------------------------------------------------------- #
# helpers every rule shares
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("explained", "relevant", "expected"),
    [(39, 43, 39 / 43), (0, 10, 0.0), (5, 5, 1.0), (7, 0, 0.0), (11, 10, 1.0), (-1, 10, 0.0)],
)
def test_evidence_share_is_always_a_share(
    explained: float, relevant: float, expected: float
) -> None:
    """Confidence means one thing in this tool, so two insights can be compared."""
    assert evidence_share(explained, relevant) == pytest.approx(expected)


def test_count_rows_counts_bars_not_findings() -> None:
    """One finding can cover a whole session; counting findings under-reports the work."""
    report = I.report_of(
        [
            Finding("stale_bar", Severity.INFO, "ESH26", Frequency.DAILY, "m", count=1_400),
            Finding("stale_bar", Severity.INFO, "CLG26", Frequency.DAILY, "m", count=229),
        ]
    )
    assert count_rows(report.findings) == 1_629
    assert report.findings.height == 2


def test_count_rows_of_an_empty_frame_is_zero() -> None:
    assert count_rows(QualityReport.empty(Frequency.DAILY).findings) == 0


def test_decode_evidence_returns_null_for_a_key_a_check_never_wrote() -> None:
    """This is what lets one rule read findings from checks with different evidence."""
    report = I.report_of(
        [
            Finding(
                "invalid_ohlc",
                Severity.ERROR,
                "CLG26",
                Frequency.DAILY,
                "m",
                evidence={"zero_volume": 3},
            )
        ]
    )
    decoded = decode_evidence(
        report.findings, {"zero_volume": pl.UInt32(), "never_written": pl.UInt32()}
    )
    assert decoded.get_column("zero_volume").to_list() == [3]
    assert decoded.get_column("never_written").to_list() == [None]


def test_decode_evidence_with_no_fields_is_the_identity() -> None:
    report = QualityReport.empty(Frequency.DAILY)
    assert decode_evidence(report.findings, {}) is report.findings


def test_distinct_is_sorted_and_drops_nulls() -> None:
    frame = pl.DataFrame({"contract": ["ESH26", None, "CLG26", "ESH26"]})
    assert distinct(frame, "contract") == ["CLG26", "ESH26"]
    assert distinct(frame.head(0), "contract") == []


def test_as_iso_dates_drops_the_dates_that_are_not_there() -> None:
    assert as_iso_dates([date(2022, 1, 13), None, date(2021, 12, 2)]) == [
        "2022-01-13",
        "2021-12-02",
    ]


@pytest.mark.parametrize(
    ("minute", "expected"),
    [(0, "00:00"), (1_020, "17:00"), (959, "15:59"), (1_440, "00:00"), (1_500, "01:00")],
)
def test_local_hhmm_reads_as_a_wall_clock_and_wraps_past_midnight(
    minute: int, expected: str
) -> None:
    assert local_hhmm(minute) == expected


# --------------------------------------------------------------------------- #
# InsightContext
# --------------------------------------------------------------------------- #


def _mixed_report() -> QualityReport:
    return I.report_of(
        [
            Finding("stale_bar", Severity.INFO, "CLG26", Frequency.DAILY, "m", count=5),
            Finding("invalid_ohlc", Severity.ERROR, "CLH26", Frequency.DAILY, "m", count=2),
            Finding("invalid_ohlc", Severity.WARNING, "ESH26", Frequency.DAILY, "m", count=1),
            Finding("invalid_ohlc", Severity.INFO, "SR3H26", Frequency.DAILY, "m", count=1),
        ]
    )


def test_the_context_derives_the_product_root_findings_do_not_carry() -> None:
    """The headline insight is about whole product families; findings only name contracts."""
    ctx = I.context_for(_mixed_report())
    assert ctx.findings.get_column(ROOT).to_list() == ["CL", "CL", "ES", "SR3"]


def test_subset_narrows_by_check_and_by_severity() -> None:
    ctx = I.context_for(_mixed_report())
    assert ctx.subset("stale_bar").height == 1
    assert ctx.subset(["stale_bar", "invalid_ohlc"]).height == 4
    assert ctx.subset("invalid_ohlc", min_severity=Severity.WARNING).height == 2
    assert ctx.subset("invalid_ohlc", min_severity=Severity.ERROR).height == 1


def test_subset_of_a_check_that_never_ran_is_empty_not_an_error() -> None:
    """Every rule whose check has not been built depends on this (PLAN §5 rule 10)."""
    ctx = I.context_for(_mixed_report())
    assert ctx.subset("cross_frequency_mismatch").height == 0
    assert ctx.subset("cross_frequency_mismatch", min_severity=Severity.ERROR).height == 0
    assert not ctx.has("cross_frequency_mismatch")
    assert ctx.has("stale_bar")


def test_contract_accessors_report_the_uncapped_total_alongside_the_capped_list() -> None:
    findings = [
        Finding("stale_bar", Severity.INFO, f"CL{i:03d}", Frequency.DAILY, "m")
        for i in range(MAX_LISTED + 10)
    ]
    ctx = I.context_for(I.report_of(findings))
    frame = ctx.subset("stale_bar")
    assert len(ctx.contracts_in(frame)) == MAX_LISTED
    assert ctx.contract_total(frame) == MAX_LISTED + 10


def test_roots_in_collapses_a_family() -> None:
    ctx = I.context_for(_mixed_report())
    assert ctx.roots_in(ctx.subset("invalid_ohlc")) == ["CL", "ES", "SR3"]


def test_family_size_is_unknown_without_a_summary_rather_than_invented() -> None:
    """An insight may say "4 contracts" but never "4 of the 5" it does not know about."""
    assert I.context_for(_mixed_report()).family_size("CL") is None


def test_family_size_and_exchange_come_from_the_summary_when_there_is_one() -> None:
    summary = DatasetSummary(contracts=("CLG26", "CLH26", "CLJ26"), exchanges=("NYMEX",))
    ctx = I.context_for(_mixed_report(), summary)
    assert ctx.family_size("CL") == 3
    assert ctx.family_size("ZC") is None
    assert ctx.exchange == "NYMEX"


def test_exchange_is_none_when_the_dataset_spans_more_than_one() -> None:
    summary = DatasetSummary(exchanges=("CME", "NYMEX"))
    assert I.context_for(_mixed_report(), summary).exchange is None
    assert I.context_for(_mixed_report()).exchange is None


def test_the_context_falls_back_to_the_reports_own_activity_profile() -> None:
    """A report is meant to be interpretable on its own, profile included."""
    _, ctx, report = scenarios.checked(scenarios.daily_frame(), Frequency.DAILY)
    assert InsightContext(report=report).activity is ctx.activity


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def test_every_planned_rule_is_discovered_without_an_import_list() -> None:
    assert set(RuleRegistry.ids()) == EXPECTED_RULES


def test_registered_classes_satisfy_the_pattern_rule_protocol() -> None:
    for rule in RuleRegistry.all():
        assert isinstance(rule, PatternRule)
        assert rule.title
        assert rule.id


def test_every_rule_has_a_distinct_human_title() -> None:
    """The title is a headline on the Insights page; two identical ones read as a bug."""
    titles = [rule.title for rule in RuleRegistry.all()]
    assert len(set(titles)) == len(titles)


def test_all_is_ordered_by_id_so_two_runs_agree() -> None:
    assert [rule.id for rule in RuleRegistry.all()] == sorted(EXPECTED_RULES)


def test_get_unknown_rule_names_what_is_available() -> None:
    with pytest.raises(UnknownRuleError, match="unknown insight rule 'nope'"):
        RuleRegistry.get("nope")


def test_duplicate_ids_are_rejected() -> None:
    class _First:
        id: ClassVar[str] = "_dupe"
        title: ClassVar[str] = "first"

        def applies(self, ctx: InsightContext) -> bool:  # pragma: no cover - never run
            return False

        def build(self, ctx: InsightContext) -> Insight:  # pragma: no cover - never run
            return _insight(self.id, 1.0)

    class _Second(_First):
        pass

    register_rule(_First)
    try:
        with pytest.raises(ValueError, match="duplicate rule id"):
            register_rule(_Second)
    finally:
        engine_module._REGISTRY.pop("_dupe", None)


def test_registering_the_same_class_twice_is_idempotent() -> None:
    """Module re-import must not explode; only a genuine id collision is an error."""
    try:
        register_rule(_Silent)
        register_rule(_Silent)
        assert RuleRegistry.get("_silent") is not None
    finally:
        engine_module._REGISTRY.pop("_silent", None)


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #


def test_an_empty_report_yields_no_insights_and_no_exception() -> None:
    """ "Nothing to explain" is an ordinary answer that every caller relies on."""
    for frequency in Frequency:
        report = QualityReport.empty(frequency)
        assert RuleBasedInsightEngine().derive(report) == []


def test_clean_data_yields_no_insights() -> None:
    """The precision guard: a rule that fires on perfect data would bury a real user."""
    ctx = I.context(scenarios.minute_frame())
    assert RuleBasedInsightEngine().derive(ctx.report, ctx.activity, ctx.summary) == []


def test_a_rule_that_raises_in_build_does_not_take_down_the_others() -> None:
    result = RuleBasedInsightEngine(rules=(_Exploding(), _Fixed())).derive_with_failures(
        QualityReport.empty(Frequency.DAILY)
    )
    assert [i.id for i in result.insights] == ["_fixed"]
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.rule_id == "_exploding"
    assert failure.error_type == "RuntimeError"
    assert "boom" in failure.error
    assert "RuntimeError" in failure.traceback


def test_a_rule_that_raises_in_applies_is_isolated_too() -> None:
    result = RuleBasedInsightEngine(rules=(_ExplodingApplies(), _Fixed())).derive_with_failures(
        QualityReport.empty(Frequency.DAILY)
    )
    assert [i.id for i in result.insights] == ["_fixed"]
    assert result.failures[0].error_type == "ValueError"


def test_a_rule_returning_the_wrong_type_is_isolated_rather_than_trusted() -> None:
    result = RuleBasedInsightEngine(rules=(_WrongShape(),)).derive_with_failures(
        QualityReport.empty(Frequency.DAILY)
    )
    assert result.insights == []
    assert result.failures[0].error_type == "TypeError"
    assert "expected Insight" in result.failures[0].error


def test_failures_are_never_dressed_up_as_insights() -> None:
    """A business user reading the Insights page must never see our stack traces."""
    insights = RuleBasedInsightEngine(rules=(_Exploding(),)).derive(
        QualityReport.empty(Frequency.DAILY)
    )
    assert insights == []


def test_a_rule_failure_serialises_for_an_operator() -> None:
    failure = RuleFailure("r", "RuntimeError", "boom", "tb")
    assert failure.to_dict() == {
        "rule_id": "r",
        "error_type": "RuntimeError",
        "error": "boom",
        "traceback": "tb",
    }


def test_a_rule_without_an_id_is_still_reported_by_class_name() -> None:
    class _Anonymous:
        def applies(self, ctx: InsightContext) -> bool:
            raise RuntimeError("boom")

        def build(self, ctx: InsightContext) -> Insight:  # pragma: no cover - never run
            return _insight("x", 1.0)

    result = RuleBasedInsightEngine(rules=(_Anonymous(),)).derive_with_failures(  # type: ignore[arg-type]
        QualityReport.empty(Frequency.DAILY)
    )
    assert result.failures[0].rule_id == "_Anonymous"


def test_insights_are_ordered_strongest_evidence_first_then_by_id() -> None:
    class _Weak(_Fixed):
        id: ClassVar[str] = "_weak"
        confidence: ClassVar[float] = 0.1

    class _AlsoStrong(_Fixed):
        id: ClassVar[str] = "_also_strong"
        confidence: ClassVar[float] = 0.4

    engine = RuleBasedInsightEngine(rules=(_Weak(), _Fixed(), _AlsoStrong()))
    assert [i.id for i in engine.derive(QualityReport.empty(Frequency.DAILY))] == [
        "_also_strong",
        "_fixed",
        "_weak",
    ]


def test_a_rule_that_does_not_apply_contributes_nothing() -> None:
    result = RuleBasedInsightEngine(rules=(_Silent(),)).derive_with_failures(
        QualityReport.empty(Frequency.DAILY)
    )
    assert result.insights == []
    assert result.failures == []


def test_the_default_engine_runs_every_registered_rule() -> None:
    """`rules=None` is what the application uses; a test passes an explicit tuple."""
    engine = RuleBasedInsightEngine()
    assert engine.rules is None
    report = I.report_of(
        [
            Finding(
                "duplicate_timestamp",
                Severity.INFO,
                "ESH26",
                Frequency.DAILY,
                "m",
                count=2,
                session_date=date(2026, 3, 3),
                start_utc=datetime(2026, 3, 3, tzinfo=UTC),
                evidence={"row_ids": [0, 1], "conflicting": False, "distinct_timestamps": 1},
            )
        ]
    )
    assert [i.id for i in engine.derive(report)] == ["duplicate_timestamp_shape"]


def test_insight_result_reports_its_size_and_serialises() -> None:
    result = InsightResult(insights=[_insight("a", 1.0)])
    assert len(result) == 1
    assert result.to_dicts()[0]["id"] == "a"
    assert result.failures == []


def test_load_rules_is_idempotent() -> None:
    """It is called on every registry access; a second import pass must be a no-op."""
    before = RuleRegistry.ids()
    engine_module.load_rules()
    assert RuleRegistry.ids() == before
