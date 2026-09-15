"""The check registry and the runner — including the promise that one broken check
cannot cost the user the other thirteen."""

from __future__ import annotations

import json
from typing import ClassVar

import polars as pl
import pytest

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import FINDING_SCHEMA, BarFrame, C, empty_finding_df
from mdq.quality import registry as registry_module
from mdq.quality.registry import (
    CHECK_FAILED_ID,
    Check,
    CheckContext,
    CheckRegistry,
    UnknownCheckError,
    register_check,
    run_checks,
)
from support import scenarios

pytestmark = pytest.mark.unit

#: Every check PLAN §4.3 asks for, minus the WP8 stretch `cross_frequency_mismatch`.
EXPECTED_CHECKS = {
    "ambiguous_local_time",
    "duplicate_timestamp",
    "intrabar_gap",
    "invalid_ohlc",
    "malformed_record",
    "missing_session",
    "missing_value",
    "negative_volume",
    "non_positive_price",
    "off_grid_timestamp",
    "outlier_return",
    "stale_bar",
    "timestamp_out_of_range",
    "zero_volume_with_range",
}


class _Exploding:
    """A check that always raises — the whole point is that it must not be fatal."""

    id: ClassVar[str] = "_exploding"
    title: ClassVar[str] = "Deliberately broken check"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.ERROR
    suggested_rule_id: ClassVar[str | None] = None

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        raise RuntimeError("boom")


class _WrongShape:
    """A check that returns something that is not a findings frame."""

    id: ClassVar[str] = "_wrong_shape"
    title: ClassVar[str] = "Returns the wrong schema"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.ERROR
    suggested_rule_id: ClassVar[str | None] = None

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        return pl.DataFrame({"not_a_finding": [1]})


class _Silent:
    """A well-behaved check that finds nothing."""

    id: ClassVar[str] = "_silent"
    title: ClassVar[str] = "Finds nothing"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset({Frequency.DAILY})
    default_severity: ClassVar[Severity] = Severity.INFO
    suggested_rule_id: ClassVar[str | None] = None

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        return empty_finding_df()


# --- registry -------------------------------------------------------------- #


def test_every_planned_check_is_discovered_without_an_import_list() -> None:
    assert set(CheckRegistry.ids()) == EXPECTED_CHECKS


def test_registered_classes_satisfy_the_check_protocol() -> None:
    for check in CheckRegistry.all():
        assert isinstance(check, Check)
        assert check.title
        assert check.frequencies
        assert isinstance(check.default_severity, Severity)


def test_for_frequency_filters_by_declaration() -> None:
    minute_only = {"off_grid_timestamp", "ambiguous_local_time", "intrabar_gap"}
    daily_ids = {c.id for c in CheckRegistry.for_frequency(Frequency.DAILY)}
    assert daily_ids == EXPECTED_CHECKS - minute_only
    assert {c.id for c in CheckRegistry.for_frequency(Frequency.MINUTE)} == EXPECTED_CHECKS


def test_get_unknown_check_names_what_is_available() -> None:
    with pytest.raises(UnknownCheckError, match="unknown check 'nope'"):
        CheckRegistry.get("nope")


def test_duplicate_ids_are_rejected() -> None:
    class _First:
        id: ClassVar[str] = "_dupe"
        title: ClassVar[str] = "first"
        frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
        default_severity: ClassVar[Severity] = Severity.INFO
        suggested_rule_id: ClassVar[str | None] = None

        def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
            return empty_finding_df()

    class _Second(_First):
        pass

    register_check(_First)
    try:
        with pytest.raises(ValueError, match="duplicate check id"):
            register_check(_Second)
    finally:
        registry_module._REGISTRY.pop("_dupe", None)


def test_registering_the_same_class_twice_is_idempotent() -> None:
    """Module re-import must not explode; only a genuine id collision is an error."""
    try:
        register_check(_Silent)
        register_check(_Silent)
        assert CheckRegistry.get("_silent") is not None
    finally:
        registry_module._REGISTRY.pop("_silent", None)


# --- run_checks ------------------------------------------------------------ #


def test_a_raising_check_becomes_one_error_finding_and_the_rest_still_run() -> None:
    bars, ctx, _ = scenarios.checked(scenarios.minute_frame())
    report = run_checks(
        bars, ctx, checks=[*CheckRegistry.for_frequency(bars.frequency), _Exploding()]
    )

    failures = report.filter(check_id=CHECK_FAILED_ID)
    assert len(failures) == 1
    row = failures.findings.row(0, named=True)
    assert row[C.SEVERITY] == Severity.ERROR.label
    assert "_exploding" in row[C.MESSAGE]
    assert "RuntimeError" in row[C.MESSAGE]
    evidence = json.loads(row[C.EVIDENCE])
    assert evidence["error_type"] == "RuntimeError"
    assert "boom" in evidence["traceback"]
    # The genuine checks still ran and still found nothing on clean data.
    assert report.checks_run[-1] == "_exploding"
    assert report.check_ids() == [CHECK_FAILED_ID]


def test_a_check_returning_the_wrong_schema_is_isolated_too() -> None:
    bars, ctx, _ = scenarios.checked(scenarios.minute_frame())
    report = run_checks(bars, ctx, checks=[_WrongShape()])
    assert report.check_ids() == [CHECK_FAILED_ID]
    assert "missing columns" in report.findings.row(0, named=True)[C.MESSAGE]


def test_checks_may_be_selected_by_id_or_instance() -> None:
    bars, ctx, _ = scenarios.checked(scenarios.minute_frame())
    report = run_checks(bars, ctx, checks=["stale_bar", _Exploding()])
    assert report.checks_run == ("stale_bar", "_exploding")


def test_a_selected_check_that_does_not_apply_to_the_frequency_is_skipped() -> None:
    bars, ctx, _ = scenarios.checked(scenarios.daily_frame(), Frequency.DAILY)
    report = run_checks(bars, ctx, checks=["intrabar_gap", "stale_bar"])
    assert report.checks_run == ("stale_bar",)


def test_a_frequency_mismatch_is_refused_rather_than_silently_wrong() -> None:
    bars, _, _ = scenarios.checked(scenarios.minute_frame())
    daily_ctx = CheckContext(frequency=Frequency.DAILY)
    with pytest.raises(ValueError, match="does not match bars frequency"):
        run_checks(bars, daily_ctx)


@pytest.mark.parametrize("frequency", list(Frequency))
def test_empty_bar_frame_yields_an_empty_report_with_the_right_schema(
    frequency: Frequency,
) -> None:
    bars = BarFrame.empty(frequency)
    report = run_checks(bars, CheckContext.build(bars))
    assert report.is_empty()
    assert report.findings.schema == FINDING_SCHEMA
    assert report.frequency is frequency
    assert set(report.checks_run) == {c.id for c in CheckRegistry.for_frequency(frequency)}


def test_run_checks_with_no_applicable_checks_still_returns_a_report() -> None:
    bars, ctx, _ = scenarios.checked(scenarios.minute_frame())
    report = run_checks(bars, ctx, checks=[])
    assert report.is_empty()
    assert report.checks_run == ()


def test_the_report_carries_the_thresholds_that_produced_its_severities() -> None:
    _, _, report = scenarios.checked(scenarios.minute_frame())
    assert report.thresholds["config"]["max_gap_minutes"] == 5
    assert report.thresholds["activity"]["metric"] == "bar_count"


def test_a_context_without_an_activity_profile_still_works() -> None:
    """`CheckContext(frequency=...)` alone must be usable — no regime knowledge simply
    means no downgrades, never a crash."""
    bars, _, _ = scenarios.checked(scenarios.minute_frame())
    bare = CheckContext(frequency=Frequency.MINUTE)
    assert bare.activity is None
    assert bare.profile.collect().height == 0
    report = run_checks(bars, bare, checks=["stale_bar", "invalid_ohlc"])
    assert report.is_empty()


class _LazyReturn:
    """A check that returns a LazyFrame — a natural mistake, and still not fatal."""

    id: ClassVar[str] = "_lazy_return"
    title: ClassVar[str] = "Returns a LazyFrame"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.ERROR
    suggested_rule_id: ClassVar[str | None] = None

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        return empty_finding_df().lazy()  # type: ignore[return-value]


def test_a_check_returning_a_lazy_frame_is_isolated() -> None:
    bars, ctx, _ = scenarios.checked(scenarios.minute_frame())
    report = run_checks(bars, ctx, checks=[_LazyReturn()])
    assert report.check_ids() == [CHECK_FAILED_ID]
    assert "expected a polars DataFrame" in report.findings.row(0, named=True)[C.MESSAGE]
