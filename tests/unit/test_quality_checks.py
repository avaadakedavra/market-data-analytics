"""Every check, twice: it must say nothing about clean data, and exactly the right thing
about one injected fault.

The first half is the **precision guard**. `tests.support.synth` builds bars that are
dense, ordered, uniquely stamped, coherent and fully traded, so any finding at all on
that data is a false positive — and a check that fires on clean data will bury a real
user under noise long before it helps them. It is parametrised over the registry, so a
check added later is covered the moment it is registered.

The second half is **recall with precision**: each fault asserts that its check fires
*and* that no other check does.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import polars as pl
import pytest

from mdq.domain.findings import RejectReason, Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality import CheckRegistry, run_checks
from mdq.quality.registry import CHECK_FAILED_ID, Check
from support import faults as F
from support import scenarios

pytestmark = pytest.mark.unit


def _ids(frequency: Frequency) -> list[str]:
    return [c.id for c in CheckRegistry.for_frequency(frequency)]


# --------------------------------------------------------------------------- #
# precision: clean data yields nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("check_id", _ids(Frequency.MINUTE))
def test_clean_minute_data_yields_no_findings(check_id: str) -> None:
    bars, ctx, _ = scenarios.checked(scenarios.minute_frame())
    report = run_checks(bars, ctx, checks=[check_id])
    assert report.is_empty(), report.to_dicts()


@pytest.mark.parametrize("check_id", _ids(Frequency.DAILY))
def test_clean_daily_data_yields_no_findings(check_id: str) -> None:
    bars, ctx, _ = scenarios.checked(scenarios.daily_frame(), Frequency.DAILY)
    report = run_checks(bars, ctx, checks=[check_id])
    assert report.is_empty(), report.to_dicts()


def test_clean_full_length_sessions_yield_no_findings() -> None:
    """Two complete 1,380-bar CME sessions across the 2026-03-08 DST weekend."""
    _, _, report = scenarios.checked(scenarios.full_sessions())
    assert report.is_empty(), report.to_dicts()


def test_clean_two_contract_dataset_yields_no_findings() -> None:
    _, _, report = scenarios.checked(scenarios.two_contract_frame())
    assert report.is_empty(), report.to_dicts()


@pytest.mark.parametrize("frequency", list(Frequency))
def test_no_check_ever_raises_on_clean_data(frequency: Frequency) -> None:
    """A `check_failed` finding here would mean a check crashed rather than passed."""
    frame = scenarios.minute_frame() if frequency is Frequency.MINUTE else scenarios.daily_frame()
    _, _, report = scenarios.checked(frame, frequency)
    assert CHECK_FAILED_ID not in report.check_ids()


# --------------------------------------------------------------------------- #
# recall: one fault, exactly one finding
# --------------------------------------------------------------------------- #

MINUTE_FAULTS: list[tuple[str, F.Fault]] = [
    ("exact_duplicate", F.ExactDuplicate(n=2)),
    ("conflicting_duplicate", F.ConflictingDuplicate(n=1)),
    ("swap_high_low", F.SwapHighLow([10])),
    ("close_outside_range", F.CloseOutsideRange([11])),
    ("negative_price", F.NegativePrice([12])),
    ("negative_volume", F.NegativeVolume([13])),
    ("null_close", F.NullField(C.CLOSE, [14])),
    ("null_volume", F.NullField(C.VOLUME, [15])),
    ("off_grid", F.OffGridSeconds([16])),
    ("future_timestamp", F.FutureTimestamp([17])),
    ("zero_volume_with_range", F.ZeroVolumeWithRange([18])),
    ("flat_zero_volume", F.FlatZeroVolumeBar([19])),
    ("price_spike", F.PriceSpike(60, factor=1.5)),
    ("gap", F.DropRange(datetime(2026, 3, 3, 17, 30), datetime(2026, 3, 3, 18, 9))),
]


@pytest.mark.parametrize(("name", "fault"), MINUTE_FAULTS, ids=[name for name, _ in MINUTE_FAULTS])
def test_each_minute_fault_produces_exactly_its_finding(name: str, fault: F.Fault) -> None:
    frame, expected = F.inject(scenarios.minute_frame(), fault)
    _, _, report = scenarios.checked(frame)
    F.assert_findings_match(report, expected)


DAILY_FAULTS: list[tuple[str, F.Fault]] = [
    ("swap_high_low", F.SwapHighLow([0])),
    ("close_outside_range", F.CloseOutsideRange([2])),
    ("carried_forward_settlement", F.CarriedForwardSettlement([1])),
    ("negative_volume", F.NegativeVolume([1])),
    ("null_open", F.NullField(C.OPEN, [1])),
]


@pytest.mark.parametrize(("name", "fault"), DAILY_FAULTS, ids=[name for name, _ in DAILY_FAULTS])
def test_each_daily_fault_produces_exactly_its_finding(name: str, fault: F.Fault) -> None:
    frame, expected = F.inject(scenarios.daily_frame(), fault)
    _, _, report = scenarios.checked(frame, Frequency.DAILY)
    F.assert_findings_match(report, expected)


# --------------------------------------------------------------------------- #
# individual checks, where the detail matters
# --------------------------------------------------------------------------- #


def test_invalid_ohlc_evidence_exposes_the_corpus_signature() -> None:
    """39 of the real 43 violations are `open == high == low` with a moved close and zero
    volume. WP5 recognises the pattern from this evidence without re-reading the bars."""
    frame, _ = F.inject(scenarios.daily_frame(), F.CarriedForwardSettlement([1]))
    _, _, report = scenarios.checked(frame, Frequency.DAILY)
    row = report.filter(check_id="invalid_ohlc").findings.row(0, named=True)
    evidence = json.loads(row[C.EVIDENCE])

    assert evidence["carried_forward_settlement"] == 1
    assert evidence["zero_volume"] == 1
    assert evidence["high_lt_max_open_close"] == 1
    assert evidence["high_lt_low"] == 0
    # Both axes of the feed-incident insight must be populated on every finding.
    assert row[C.CONTRACT] == "ESH26"
    assert row[C.SESSION_DATE] == date(2026, 3, 4)


def test_invalid_ohlc_reports_every_clause() -> None:
    frame, _ = F.inject(scenarios.daily_frame(), F.SwapHighLow([0]))
    _, _, report = scenarios.checked(frame, Frequency.DAILY)
    evidence = json.loads(report.findings.row(0, named=True)[C.EVIDENCE])
    assert evidence["high_lt_low"] == 1
    assert evidence["carried_forward_settlement"] == 0


def test_non_positive_price_exempts_the_documented_roots() -> None:
    """WTI settled negative in April 2020; flagging CL would be wrong. The exemption is
    configuration, so the test reads it from the config rather than hardcoding CL."""
    frame = scenarios.daily_frame().with_columns(
        pl.lit("CL").alias(C.ROOT), pl.lit("CLG26").alias(C.CONTRACT)
    )
    frame, _ = F.inject(frame, F.NegativePrice([1]))
    _, _, report = scenarios.checked(frame, Frequency.DAILY)
    assert "non_positive_price" not in report.check_ids()


def test_non_positive_price_flags_a_root_that_is_not_exempt() -> None:
    frame, expected = F.inject(scenarios.daily_frame(), F.NegativePrice([1]))
    _, _, report = scenarios.checked(frame, Frequency.DAILY)
    F.assert_findings_match(report, expected)


def test_timestamp_out_of_range_catches_the_lower_bound_too() -> None:
    frame = scenarios.daily_frame().with_columns(
        pl.when(pl.col(C.ROW_ID) == 1)
        .then(pl.col(C.TS_UTC).dt.offset_by("-200y"))
        .otherwise(pl.col(C.TS_UTC))
        .alias(C.TS_UTC)
    )
    _, _, report = scenarios.checked(frame, Frequency.DAILY)
    evidence = json.loads(
        report.filter(check_id="timestamp_out_of_range").findings.row(0, named=True)[C.EVIDENCE]
    )
    assert evidence["ancient"] == 1
    assert evidence["future"] == 0


def test_duplicate_timestamp_separates_exact_from_conflicting_in_one_pass() -> None:
    frame, _ = F.inject(
        scenarios.minute_frame(), F.ExactDuplicate(rows=[5]), F.ConflictingDuplicate(rows=[6])
    )
    _, _, report = scenarios.checked(frame)
    severities = dict(
        zip(
            report.findings.get_column(C.SEVERITY),
            report.findings.get_column(C.SUGGESTED_RULE_ID),
            strict=True,
        )
    )
    assert severities == {
        Severity.INFO.label: "drop_exact_duplicates",
        Severity.ERROR.label: "reject_conflicting_duplicates",
    }


def test_stale_bar_and_zero_volume_with_range_never_report_the_same_bar() -> None:
    """One requires `high == low`, the other `high != low`; no bar may be reported twice."""
    frame, _ = F.inject(
        scenarios.minute_frame(), F.FlatZeroVolumeBar([30]), F.ZeroVolumeWithRange([31])
    )
    _, _, report = scenarios.checked(frame)
    stale = set(report.filter(check_id="stale_bar").rows_affected(Severity.INFO))
    ranged = set(report.filter(check_id="zero_volume_with_range").rows_affected(Severity.INFO))
    assert stale == {30}
    assert ranged == {31}


def test_outlier_return_ignores_a_degenerate_baseline() -> None:
    """A run of identical closes gives a zero MAD; every move would then be "infinitely
    many MADs". Those windows must produce nothing — flat runs are `stale_bar`'s job."""
    frame = scenarios.minute_frame()
    frame = frame.with_columns(
        *(pl.lit(5000.0).alias(col) for col in C.PRICES),
    )
    _, _, report = scenarios.checked(frame)
    assert "outlier_return" not in report.check_ids()


def test_off_grid_and_ambiguous_checks_are_not_run_on_daily_frames() -> None:
    bars, ctx, _ = scenarios.checked(scenarios.daily_frame(), Frequency.DAILY)
    report = run_checks(bars, ctx)
    assert "off_grid_timestamp" not in report.checks_run
    assert "ambiguous_local_time" not in report.checks_run


# --------------------------------------------------------------------------- #
# malformed_record — the interface WP2 will wire up
# --------------------------------------------------------------------------- #


def test_malformed_record_reads_the_documented_rejects_shape() -> None:
    frame, rejects, expected = F.inject_with_rejects(
        scenarios.minute_frame(),
        F.NonexistentLocalTime(),
        F.MalformedLine(row_ids=(900_100, 900_101)),
    )
    _, _, report = scenarios.checked(frame, rejects=rejects)
    F.assert_findings_match(report, expected)

    reasons = {
        json.loads(row[C.EVIDENCE])["reason"]
        for row in report.filter(check_id="malformed_record").to_dicts()
    }
    assert reasons == {
        RejectReason.NONEXISTENT_LOCAL_TIME.value,
        RejectReason.MALFORMED_LINE.value,
    }


def test_malformed_record_row_ids_reach_cleanse() -> None:
    frame, rejects, _ = F.inject_with_rejects(scenarios.minute_frame(), F.MalformedLine())
    _, _, report = scenarios.checked(frame, rejects=rejects)
    assert report.rows_affected() == [900_100]


def test_malformed_record_tolerates_a_minimal_rejects_frame() -> None:
    """Only `reason` is required; WP2 may enrich the frame without touching this check."""
    minimal = pl.DataFrame({C.REASON: [RejectReason.NULL_CONTRACT.value] * 3})
    _, _, report = scenarios.checked(scenarios.minute_frame(), rejects=minimal)
    finding = report.filter(check_id="malformed_record").findings.row(0, named=True)
    assert finding[C.COUNT] == 3
    assert finding[C.CONTRACT] is None


def test_malformed_record_without_a_reason_column_is_isolated_not_fatal() -> None:
    broken = pl.DataFrame({"something_else": [1]})
    _, _, report = scenarios.checked(scenarios.minute_frame(), rejects=broken)
    assert report.check_ids() == [CHECK_FAILED_ID]
    assert "reason" in report.findings.row(0, named=True)[C.MESSAGE]


def test_no_rejects_means_no_malformed_findings() -> None:
    _, _, report = scenarios.checked(scenarios.minute_frame(), rejects=F.no_rejects())
    assert "malformed_record" not in report.check_ids()


# --------------------------------------------------------------------------- #
# declarations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("check", CheckRegistry.all(), ids=lambda c: c.id)
def test_every_check_declares_a_suggested_rule_for_the_insights_layer(check: Check) -> None:
    assert check.suggested_rule_id, f"{check.id} has no suggested rule"


def test_findings_carry_the_suggested_rule_id_through_to_the_report() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.SwapHighLow([10]))
    _, _, report = scenarios.checked(frame)
    assert report.findings.get_column(C.SUGGESTED_RULE_ID).to_list() == ["ohlc_bounds"]
