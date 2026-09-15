"""The regime model doing its job: the same shape of data, judged differently.

These are the cases that decide whether the tool is usable. 47% of daily rows and 48% of
minute rows in the real corpus are flat bars, 81% of CLG26's daily bars are flat (71%
untraded outright), and minute coverage is a median 222 bars of the 1,380 a full CME
session would hold. Every assertion below is the difference
between a report a business user can read and a report with a million WARNINGs in it.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import polars as pl
import pytest

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality import CheckContext, run_checks
from mdq.quality.context import A, Regime
from support import faults as F
from support import scenarios, synth

pytestmark = pytest.mark.unit

#: Rows 240–359 are the whole third session of `scenarios.minute_frame()`.
THIRD_SESSION = list(range(240, 360))


def _only(report, check_id: str) -> dict:
    subset = report.filter(check_id=check_id)
    assert len(subset) == 1, subset.to_dicts()
    return subset.findings.row(0, named=True)


# --------------------------------------------------------------------------- #
# staleness
# --------------------------------------------------------------------------- #


def test_a_stale_bar_in_a_dormant_session_is_info() -> None:
    """A whole untraded session: the exchange published settlements, nobody traded. That
    is 14,152 rows of the real corpus and it must not shout."""
    frame, _ = F.inject(scenarios.minute_frame(), F.FlatZeroVolumeBar(THIRD_SESSION))
    _, _, report = scenarios.checked(frame)
    finding = _only(report, "stale_bar")

    assert finding[C.SEVERITY] == Severity.INFO.label
    assert finding[C.COUNT] == len(THIRD_SESSION)
    evidence = json.loads(finding[C.EVIDENCE])
    assert evidence["regime"] == Regime.DORMANT.value
    # The numbers that caused the downgrade travel with the finding.
    assert evidence["session_volume"] == 0
    assert evidence["dormant_threshold"] == pytest.approx(12.0)


def test_the_same_stale_bar_in_an_active_session_is_a_warning() -> None:
    """Identical row shape, different regime: here the feed stopped updating while the
    market was moving, which is a real defect."""
    frame, _ = F.inject(scenarios.minute_frame(), F.FlatZeroVolumeBar([19]))
    _, _, report = scenarios.checked(frame)
    finding = _only(report, "stale_bar")

    assert finding[C.SEVERITY] == Severity.WARNING.label
    assert json.loads(finding[C.EVIDENCE])["regime"] == Regime.ACTIVE.value


def test_zero_volume_with_a_range_is_downgraded_in_a_dormant_session_too() -> None:
    frame = scenarios.minute_frame()
    frame, _ = F.inject(frame, F.ZeroVolumeWithRange(THIRD_SESSION))
    _, _, report = scenarios.checked(frame)
    finding = _only(report, "zero_volume_with_range")
    assert finding[C.SEVERITY] == Severity.INFO.label


def test_the_carried_forward_settlement_signature_is_downgraded_not_suppressed() -> None:
    """The 39-of-43 corpus signature. Downgraded to WARNING because the session was
    dormant — but still reported, because the settlement really is outside its own bar."""
    frame, _ = F.inject(scenarios.daily_frame(), F.CarriedForwardSettlement([1]))
    _, _, report = scenarios.checked(frame, Frequency.DAILY)
    finding = _only(report, "invalid_ohlc")

    assert finding[C.SEVERITY] == Severity.WARNING.label
    assert json.loads(finding[C.EVIDENCE])["carried_forward_settlement"] == 1


def test_the_same_broken_ohlc_in_an_active_session_stays_an_error() -> None:
    frame, _ = F.inject(scenarios.daily_frame(), F.CloseOutsideRange([1]))
    _, _, report = scenarios.checked(frame, Frequency.DAILY)
    assert _only(report, "invalid_ohlc")[C.SEVERITY] == Severity.ERROR.label


# --------------------------------------------------------------------------- #
# gaps
# --------------------------------------------------------------------------- #


def test_a_forty_minute_gap_in_an_active_session_reports_thirty_nine_missing_minutes() -> None:
    frame, expected = F.inject(
        scenarios.minute_frame(),
        F.DropRange(datetime(2026, 3, 3, 17, 30), datetime(2026, 3, 3, 18, 9)),
    )
    _, _, report = scenarios.checked(frame)
    F.assert_findings_match(report, expected)

    finding = _only(report, "intrabar_gap")
    evidence = json.loads(finding[C.EVIDENCE])
    assert evidence["minutes_missing"] == 39
    assert evidence["gap_minutes"] == 40
    assert evidence["break_minutes_excluded"] == 0
    assert finding[C.COUNT] == 39
    assert finding[C.SESSION_DATE] == date(2026, 3, 4)
    # The bracketing bars are named so a user can jump straight to the edge of the hole.
    # 17:30 on 2026-03-03 opens the session dated 2026-03-04, i.e. rows 120-239.
    assert set(report.filter(check_id="intrabar_gap").rows_affected(Severity.INFO)) == {149, 189}


def test_the_same_gap_in_a_dormant_session_yields_nothing() -> None:
    """ESH26's median is 2 bars/day in Jun-2025 and 1,379 in Mar-2026. A hole in the first
    is the contract not trading yet, and reporting it would make the tool useless."""
    frame, expected = F.inject(
        scenarios.minute_frame(),
        F.DropRange(
            datetime(2026, 3, 4, 17, 5), datetime(2026, 3, 4, 18, 55), expect_finding=False
        ),
    )
    _, ctx, report = scenarios.checked(frame)
    regimes = dict(
        zip(
            ctx.activity.collect()[C.SESSION_DATE],
            ctx.activity.collect()[A.REGIME],
            strict=True,
        )
    )
    assert regimes[date(2026, 3, 5)] == Regime.DORMANT.value
    F.assert_findings_match(report, expected)
    assert report.is_empty()


def test_the_maintenance_break_is_not_a_gap() -> None:
    """16:00–17:00 CT, inside a calendar-dated session so the break really does fall in
    the middle of it rather than at its edge."""
    frame, expected = F.inject(
        scenarios.calendar_day_minutes(),
        F.DropRange(datetime(2026, 3, 3, 16, 0), datetime(2026, 3, 3, 17, 0), expect_finding=False),
    )
    _, _, report = scenarios.checked(frame)
    F.assert_findings_match(report, expected)
    assert report.is_empty()


def test_a_hole_around_the_break_reports_only_the_minutes_outside_it() -> None:
    """Break minutes are subtracted from `minutes_missing`, not just from the threshold —
    a user reading "30 missing" must be able to trust the number."""
    frame, _ = F.inject(
        scenarios.calendar_day_minutes(),
        F.DropRange(
            datetime(2026, 3, 3, 15, 45), datetime(2026, 3, 3, 17, 15), expect_finding=False
        ),
    )
    _, _, report = scenarios.checked(frame)
    evidence = json.loads(_only(report, "intrabar_gap")[C.EVIDENCE])
    assert evidence["break_minutes_excluded"] == 60
    assert evidence["gap_minutes"] == 91
    assert evidence["minutes_missing"] == 30


def test_the_weekend_closure_is_not_a_gap() -> None:
    """Friday 15:59 → Sunday 17:00. The bars belong to different sessions, so the hole
    never enters the arithmetic — no weekend calendar is needed."""
    _, _, report = scenarios.checked(scenarios.full_sessions())
    assert report.is_empty(), report.to_dicts()


# --------------------------------------------------------------------------- #
# missing sessions
# --------------------------------------------------------------------------- #


def test_a_session_missing_while_siblings_traded_is_a_warning() -> None:
    frame, expected = F.inject(
        scenarios.two_contract_frame(),
        F.DropSession(date(2026, 3, 4), contract="ESH26", severity=Severity.WARNING),
    )
    _, _, report = scenarios.checked(frame)
    F.assert_findings_match(report, expected)
    assert (
        "other contracts on the exchange did trade" in _only(report, "missing_session")[C.MESSAGE]
    )


def test_a_session_missing_across_the_whole_exchange_is_info() -> None:
    """A holiday, detected by corroboration instead of by shipping a holiday calendar."""
    frame, expected = F.inject(
        scenarios.two_contract_frame(),
        F.DropSession(date(2026, 3, 4), severity=Severity.INFO),
    )
    _, _, report = scenarios.checked(frame)
    F.assert_findings_match(report, expected)
    messages = report.filter(check_id="missing_session").findings.get_column(C.MESSAGE).to_list()
    assert all("holiday-like" in m for m in messages)


def test_weekends_are_never_missing_sessions() -> None:
    """Session dates are already rolled, so Saturday and Sunday sessions do not exist."""
    frame = scenarios.two_contract_frame()
    _, _, report = scenarios.checked(frame)
    assert report.is_empty(), report.to_dicts()


def test_a_contract_is_not_missing_sessions_before_it_started_trading() -> None:
    """The window is the contract's own first-to-last ACTIVE session, observed from the
    data — no listing calendar, and no findings for a contract that had not listed yet."""
    frame, _ = F.inject(scenarios.two_contract_frame(), F.FlatZeroVolumeBar(list(range(0, 60))))
    _, _, report = scenarios.checked(frame)
    assert "missing_session" not in report.check_ids()


def test_sibling_corroboration_can_come_from_outside_the_frame() -> None:
    """When the service holds several datasets it supplies the exchange-wide session
    list, so a single-contract frame can still tell a hole from a holiday."""
    frame, _ = F.inject(
        scenarios.minute_frame(), F.DropSession(date(2026, 3, 4), severity=Severity.WARNING)
    )
    bars = synth.as_bar_frame(frame, Frequency.MINUTE)
    siblings = pl.DataFrame(
        {
            C.EXCHANGE: ["CME"],
            C.SESSION_DATE: [date(2026, 3, 4)],
            "has_bars": [True],
        }
    ).lazy()
    ctx = CheckContext.build(bars, sibling_contracts=siblings)
    report = run_checks(bars, ctx)
    assert _only(report, "missing_session")[C.SEVERITY] == Severity.WARNING.label
