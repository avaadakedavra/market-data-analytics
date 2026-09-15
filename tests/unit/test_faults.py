"""The fault harness itself.

A test harness that quietly does nothing is worse than no harness at all — every test
built on it would pass. These tests check that each fault really corrupts the frame it
claims to, that the driver composes faults, and that `assert_findings_match` fails when
it should.
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, C
from support import faults as F
from support import scenarios

pytestmark = pytest.mark.unit


def test_every_shipped_fault_is_exported() -> None:
    for fault in F.ALL_FAULTS:
        assert fault.__name__ in F.__all__


def test_injection_preserves_the_bar_schema() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.ExactDuplicate(n=1), F.SwapHighLow([3]))
    assert frame.schema == BAR_SCHEMA


def test_faults_compose_and_accumulate_their_expectations() -> None:
    frame, expected = F.inject(
        scenarios.minute_frame(), F.SwapHighLow([10]), F.NegativeVolume([11])
    )
    assert {e.check_id for e in expected} == {"invalid_ohlc", "negative_volume"}
    _, _, report = scenarios.checked(frame)
    F.assert_findings_match(report, expected)


def test_duplicates_are_appended_with_fresh_row_ids() -> None:
    clean = scenarios.minute_frame()
    frame, _ = F.inject(clean, F.ExactDuplicate(n=2))
    assert frame.height == clean.height + 2
    assert frame.get_column(C.ROW_ID).n_unique() == frame.height


def test_conflicting_duplicates_differ_only_in_volume() -> None:
    """Deliberately surgical: a contradictory *price* would also break the OHLC relation
    and the return series, and the fault would stop isolating one check."""
    frame, _ = F.inject(scenarios.minute_frame(), F.ConflictingDuplicate(n=1))
    pair = frame.filter(pl.col(C.TS_UTC) == frame.get_column(C.TS_UTC).min())
    assert pair.height == 2
    assert pair.get_column(C.CLOSE).n_unique() == 1
    assert pair.get_column(C.VOLUME).n_unique() == 2


def test_drop_range_removes_exactly_the_requested_window() -> None:
    clean = scenarios.minute_frame()
    frame, _ = F.inject(
        clean, F.DropRange(datetime(2026, 3, 3, 17, 30), datetime(2026, 3, 3, 18, 9))
    )
    assert frame.height == clean.height - 39


def test_drop_range_that_matches_nothing_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="matched no bars"):
        F.inject(
            scenarios.minute_frame(),
            F.DropRange(datetime(2030, 1, 1, 0, 0), datetime(2030, 1, 1, 1, 0)),
        )


def test_drop_session_that_matches_nothing_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="matched no bars"):
        F.inject(scenarios.minute_frame(), F.DropSession(date(2030, 1, 1)))


def test_picking_absent_row_ids_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="not all present"):
        F.inject(scenarios.minute_frame(), F.ExactDuplicate(rows=[999_999]))


def test_carried_forward_settlement_reproduces_the_corpus_signature() -> None:
    frame, _ = F.inject(scenarios.daily_frame(), F.CarriedForwardSettlement([1]))
    row = frame.filter(pl.col(C.ROW_ID) == 1).row(0, named=True)
    assert row[C.OPEN] == row[C.HIGH] == row[C.LOW]
    assert row[C.CLOSE] != row[C.OPEN]
    assert row[C.VOLUME] == 0


def test_reject_producing_faults_do_not_touch_the_bars() -> None:
    clean = scenarios.minute_frame()
    frame, rejects, _ = F.inject_with_rejects(clean, F.NonexistentLocalTime(), F.MalformedLine())
    assert frame.equals(clean)
    assert rejects.schema == F.REJECTS_SCHEMA
    assert rejects.height == 2


def test_rejects_frame_is_empty_when_no_fault_produces_one() -> None:
    _, rejects, _ = F.inject_with_rejects(scenarios.minute_frame(), F.SwapHighLow([1]))
    assert rejects.height == 0
    assert F.no_rejects().schema == F.REJECTS_SCHEMA


def test_ambiguous_local_time_builds_bars_synth_refuses_to() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.AmbiguousLocalTime(bars=2, passes=2))
    appended = frame.filter(pl.col(C.SESSION_DATE) == date(2025, 11, 2))
    assert appended.height == 4
    # Both passes of the repeated hour collapse onto the same instant, which is exactly
    # why they also trip `duplicate_timestamp`.
    assert appended.get_column(C.TS_UTC).n_unique() == 2


def test_ambiguous_local_time_needs_a_frame_to_copy_metadata_from() -> None:
    with pytest.raises(ValueError, match="non-empty frame"):
        F.inject(pl.DataFrame(schema=BAR_SCHEMA), F.AmbiguousLocalTime())


def test_merge_contracts_renumbers_so_row_ids_stay_unique() -> None:
    frame = scenarios.two_contract_frame(bars_per_session=10)
    assert frame.get_column(C.ROW_ID).n_unique() == frame.height
    assert frame.get_column(C.CONTRACT).n_unique() == 2


def test_shift_wall_clock_to_utc_moves_sessions_but_breaks_no_row_level_invariant() -> None:
    """The trap the whole time layer exists to prevent. Every row stays internally
    consistent, so no row-level check can see it — only cross-frequency reconciliation
    can, which is why that check exists."""
    clean = scenarios.minute_frame()
    frame, expected = F.inject(clean, F.ShiftWallClockToUtc())
    assert expected == []
    assert not frame.get_column(C.SESSION_DATE).equals(clean.get_column(C.SESSION_DATE))
    _, _, report = scenarios.checked(frame)
    assert report.is_empty(), report.to_dicts()


def test_assert_findings_match_fails_on_an_unexpected_finding() -> None:
    """Precision: a report with something extra in it must not pass."""
    frame, _ = F.inject(scenarios.minute_frame(), F.SwapHighLow([10]))
    _, _, report = scenarios.checked(frame)
    with pytest.raises(AssertionError, match="unexpected findings"):
        F.assert_findings_match(report, [])


def test_assert_findings_match_fails_on_a_missing_finding() -> None:
    """Recall: a report that stayed silent must not pass either."""
    _, _, report = scenarios.checked(scenarios.minute_frame())
    with pytest.raises(AssertionError, match="expected findings from"):
        F.assert_findings_match(report, [F.Expected("invalid_ohlc", 1, (10,))])


def test_assert_findings_match_fails_on_the_wrong_count() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.SwapHighLow([10]))
    _, _, report = scenarios.checked(frame)
    with pytest.raises(AssertionError, match="expected count 5"):
        F.assert_findings_match(report, [F.Expected("invalid_ohlc", 5, (10,))])


def test_assert_findings_match_fails_on_the_wrong_severity() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.SwapHighLow([10]))
    _, _, report = scenarios.checked(frame)
    with pytest.raises(AssertionError, match="expected severities"):
        F.assert_findings_match(report, [F.Expected("invalid_ohlc", 1, (10,), Severity.INFO)])


def test_assert_findings_match_fails_on_the_wrong_row_ids() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.SwapHighLow([10]))
    _, _, report = scenarios.checked(frame)
    with pytest.raises(AssertionError, match="missing row_ids"):
        F.assert_findings_match(report, [F.Expected("invalid_ohlc", 1, (11,))])


def test_scenarios_cover_both_frequencies() -> None:
    _, ctx, _ = scenarios.checked(scenarios.daily_frame(), Frequency.DAILY)
    assert ctx.frequency is Frequency.DAILY
