"""Cleansing — what actually gets removed, and the audit trail that says why."""

from __future__ import annotations

import json

import pytest

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, BarFrame, C
from mdq.quality import MAX_EVIDENCE_ROW_IDS, QualityReport, cleanse
from mdq.quality.cleanse import CleansePolicy
from support import faults as F
from support import scenarios

pytestmark = pytest.mark.unit


def _row_ids(bars: BarFrame) -> list[int]:
    return sorted(int(i) for i in bars.collect().get_column(C.ROW_ID).to_list())


def test_exact_duplicates_collapse_to_the_first_row_id() -> None:
    clean_frame = scenarios.minute_frame()
    frame, _ = F.inject(clean_frame, F.ExactDuplicate(n=2))
    bars, _, report = scenarios.checked(frame)
    clean, log = cleanse(bars, report)

    assert log.rows_in == clean_frame.height + 2
    assert log.duplicates_dropped == 2
    assert log.error_rows_dropped == 0
    # The originals survive; the re-delivered copies (appended with fresh ids) do not.
    copies = {clean_frame.height, clean_frame.height + 1}
    assert _row_ids(clean) == sorted(set(range(clean_frame.height + 2)) - copies)


def test_conflicting_duplicates_lose_both_copies() -> None:
    """There is no principled way to pick between two contradictory prices, and keeping
    one silently would be a fabrication."""
    frame, _ = F.inject(scenarios.minute_frame(), F.ConflictingDuplicate(n=1))
    bars, _, report = scenarios.checked(frame)
    original, copy = sorted(report.rows_affected())
    clean, log = cleanse(bars, report)

    assert log.duplicates_dropped == 0
    assert log.error_rows_dropped == 2
    assert original not in _row_ids(clean)
    assert copy not in _row_ids(clean)
    assert log.dropped_by_check == {"duplicate_timestamp": 2}


def test_error_rows_are_dropped_and_warnings_are_not() -> None:
    frame, _ = F.inject(
        scenarios.minute_frame(),
        F.SwapHighLow([10]),  # ERROR
        F.OffGridSeconds([20]),  # WARNING
        F.FlatZeroVolumeBar([30]),  # WARNING
    )
    bars, _, report = scenarios.checked(frame)
    clean, log = cleanse(bars, report)

    surviving = set(_row_ids(clean))
    assert 10 not in surviving
    assert {20, 30} <= surviving
    assert log.rows_dropped == 1
    assert log.dropped_by_check == {"invalid_ohlc": 1}


def test_a_regime_downgrade_keeps_the_row() -> None:
    """The 39-of-43 settlement signature is WARNING in a dormant session, so cleansing
    must leave those bars alone — deleting real settlement prints would be worse than
    the defect."""
    frame, _ = F.inject(scenarios.daily_frame(), F.CarriedForwardSettlement([1]))
    bars, _, report = scenarios.checked(frame, Frequency.DAILY)
    clean, log = cleanse(bars, report)
    assert log.rows_dropped == 0
    assert 1 in _row_ids(clean)


def test_policy_can_switch_every_removal_off() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.ExactDuplicate(n=1), F.SwapHighLow([10]))
    bars, _, report = scenarios.checked(frame)
    clean, log = cleanse(
        bars, report, CleansePolicy(drop_exact_duplicates=False, drop_error_rows=False)
    )
    assert log.rows_dropped == 0
    assert clean.collect().height == frame.height


def test_exempt_checks_spare_their_rows() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.SwapHighLow([10]), F.NegativeVolume([11]))
    bars, _, report = scenarios.checked(frame)
    _, log = cleanse(bars, report, CleansePolicy(exempt_checks=frozenset({"invalid_ohlc"})))
    assert log.dropped_by_check == {"negative_volume": 1}
    assert log.error_rows_dropped == 1


def test_exempting_every_firing_check_drops_nothing() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.SwapHighLow([10]))
    bars, _, report = scenarios.checked(frame)
    _, log = cleanse(bars, report, CleansePolicy(exempt_checks=frozenset({"invalid_ohlc"})))
    assert log.error_rows_dropped == 0


def test_cleansing_preserves_the_schema_and_the_original_row_ids() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.SwapHighLow([10]))
    bars, _, report = scenarios.checked(frame)
    clean, _ = cleanse(bars, report)
    assert clean.collect().schema == BAR_SCHEMA
    assert clean.frequency is bars.frequency
    # Traceability back to the input file survives cleansing.
    assert _row_ids(clean)[:3] == [0, 1, 2]


def test_clean_data_is_returned_untouched() -> None:
    bars, _, report = scenarios.checked(scenarios.minute_frame())
    _, log = cleanse(bars, report)
    assert log.rows_dropped == 0
    assert log.dropped_by_check == {}
    assert log.incomplete_checks == ()


def test_empty_frame_cleanses_to_an_empty_frame() -> None:
    bars = BarFrame.empty(Frequency.MINUTE)
    clean, log = cleanse(bars, QualityReport.empty(Frequency.MINUTE))
    assert clean.collect().height == 0
    assert log.to_dict()["rows_in"] == 0


def test_a_report_for_the_wrong_frequency_is_refused() -> None:
    bars, _, _ = scenarios.checked(scenarios.minute_frame())
    with pytest.raises(ValueError, match="does not match bars frequency"):
        cleanse(bars, QualityReport.empty(Frequency.DAILY))


def test_the_log_serialises_for_the_api() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.SwapHighLow([10]))
    bars, _, report = scenarios.checked(frame)
    _, log = cleanse(bars, report)
    payload = log.to_dict()
    assert payload["rows_dropped"] == 1
    assert payload["policy"]["min_severity"] == Severity.ERROR.label
    assert payload["dropped_by_check"] == {"invalid_ohlc": 1}


def test_cleanse_admits_when_a_findings_evidence_was_truncated() -> None:
    """Evidence carries at most `MAX_EVIDENCE_ROW_IDS` ids per finding. Beyond that,
    cleanse cannot remove what it was never told about — so it says so instead of letting
    the caller believe the cleansed frame is complete."""
    rows = list(range(1_100))
    frame, _ = F.inject(scenarios.calendar_day_minutes(), F.NullField(C.CLOSE, rows))
    bars, _, report = scenarios.checked(frame)

    finding = report.filter(check_id="missing_value").findings.row(0, named=True)
    assert finding[C.COUNT] == len(rows)
    assert json.loads(finding[C.EVIDENCE])["row_ids_truncated"] is True

    clean, log = cleanse(bars, report)
    assert log.error_rows_dropped == MAX_EVIDENCE_ROW_IDS
    assert log.incomplete_checks == ("missing_value",)
    assert clean.collect().height == frame.height - MAX_EVIDENCE_ROW_IDS
