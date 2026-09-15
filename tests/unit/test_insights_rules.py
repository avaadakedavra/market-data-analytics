"""Every shipped pattern rule, twice: once on data that shows the pattern, once on data
that looks similar and does not.

The negative half is the important one. A rule that fires whenever its check produced any
finding at all is not an insight, it is a rename — so each negative case here is
deliberately *close* to its positive: stale bars that are not explained by dormancy,
incoherent bars without the settlement signature, two contracts instead of three, holes
spread across the week instead of piled on one day. Those are the cases that separate a
pattern from a coincidence, and they are what the thresholds exist for.

Every report is built by running the **real** quality engine over bars broken by
`support.faults`, so a rule that reads an evidence key a check stopped writing fails here
rather than silently returning nothing.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import polars as pl
import pytest

from mdq.domain.findings import Finding, Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality import CheckRegistry
from support import faults as F
from support import insights as I
from support import scenarios, synth

pytestmark = pytest.mark.unit

#: A DST-free Monday-to-Friday week of CME sessions.
WEEK = (
    date(2026, 3, 2),
    date(2026, 3, 3),
    date(2026, 3, 4),
    date(2026, 3, 5),
    date(2026, 3, 6),
)
MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY = WEEK

#: Enough daily bars that a couple of broken ones are a minority, not the whole file.
DAILY_DATES = (*WEEK, date(2026, 3, 9), date(2026, 3, 10), date(2026, 3, 11))


def _daily(contract: str = "ESH26", seed: int = 7) -> pl.DataFrame:
    return synth.daily_series(contract, DAILY_DATES, seed=seed)


def _calendar_minutes(dates: tuple[date, ...], bars: int = 1_440) -> pl.DataFrame:
    """Whole calendar days of minute bars, so a mid-day hole is inside one session."""
    return synth.minute_sessions("ESH26", dates, bars_per_session=bars, roll_hour=0, seed=3)


def _row_ids_on(frame: pl.DataFrame, session: date, contract: str | None = None) -> list[int]:
    """The `row_id`s of one session — how a fault is aimed after contracts are merged."""
    selected = frame.filter(pl.col(C.SESSION_DATE) == session)
    if contract is not None:
        selected = selected.filter(pl.col(C.CONTRACT) == contract)
    return [int(i) for i in selected.get_column(C.ROW_ID).to_list()]


def _hole(day: date, hour: int, minutes: int = 30) -> F.DropRange:
    """A `minutes`-long hole starting at `hour:00` local on `day`."""
    start = datetime(day.year, day.month, day.day, hour, 0)
    return F.DropRange(start=start, end=start.replace(minute=minutes))


def _mismatch(field: str, difference: float = 0.02, contract: str = "ESH26") -> Finding:
    """One `cross_frequency_mismatch` finding in the shape PLAN §5 rule 10 declares."""
    return Finding(
        check_id="cross_frequency_mismatch",
        severity=Severity.WARNING,
        contract=contract,
        frequency=Frequency.DAILY,
        message=f"{field} disagrees between the daily and minute files",
        session_date=date(2026, 3, 4),
        evidence={"row_ids": [], "field": field, "relative_difference": difference},
    )


def _params(insight: Any) -> dict[str, Any]:
    return dict(insight.rule.params)


# --------------------------------------------------------------------------- #
# 1 — dormancy explains staleness
# --------------------------------------------------------------------------- #

RULE_1 = "dormancy_explains_staleness"


def test_flat_bars_on_untraded_days_are_explained_by_dormancy() -> None:
    frame, _ = F.inject(_daily(), F.FlatZeroVolumeBar(rows=[2, 3], severity=Severity.INFO))
    insight = I.insight_from(RULE_1, I.context(frame, Frequency.DAILY))

    assert insight is not None
    assert insight.rule.rule_id == "classify_settlement_only_bars"
    assert insight.rule.kind == "cleansing"
    assert insight.confidence == 1.0
    assert insight.evidence["bars_affected"] == 2
    assert insight.evidence["bars_in_dormant_sessions"] == 2
    assert insight.evidence["bars_in_active_sessions"] == 0
    assert insight.affected_contracts == ["ESH26"]


def test_flat_bars_inside_a_busy_session_are_not_explained_away() -> None:
    """The negative case the whole rule turns on: 29 of the corpus's 14,152 flat bars sit
    in active sessions, and those are the ones a desk should actually see."""
    frame, _ = F.inject(
        scenarios.minute_frame(), F.FlatZeroVolumeBar(rows=[10, 11], severity=Severity.WARNING)
    )
    assert I.insight_from(RULE_1, I.context(frame)) is None


def test_no_flat_bars_at_all_is_not_an_insight() -> None:
    assert I.insight_from(RULE_1, I.context(_daily(), Frequency.DAILY)) is None


# --------------------------------------------------------------------------- #
# 2 — settlement outside a carried-forward range
# --------------------------------------------------------------------------- #

RULE_2 = "carried_forward_settlement"


def test_incoherent_bars_carrying_the_settlement_signature_are_explained() -> None:
    frame, _ = F.inject(_daily(), F.CarriedForwardSettlement(rows=[2, 3]))
    insight = I.insight_from(RULE_2, I.context(frame, Frequency.DAILY))

    assert insight is not None
    assert insight.rule.rule_id == "ohlc_bounds"
    assert insight.rule.kind == "validation"
    assert insight.confidence == 1.0
    assert insight.evidence["bars_with_carried_forward_range"] == 2
    assert insight.evidence["bars_with_zero_volume"] == 2
    assert insight.evidence["bars_without_the_signature"] == 0
    assert _params(insight)["on_exempt"]["trust"] == ["close"]


def test_a_high_below_its_low_carries_no_settlement_signature() -> None:
    """`high < low` never occurs in the real corpus — 0 of 43 — and is a different fault
    entirely: there is no convention that explains it, so the rule must stay silent."""
    frame, _ = F.inject(_daily(), F.SwapHighLow(rows=[2, 3]))
    assert I.insight_from(RULE_2, I.context(frame, Frequency.DAILY)) is None


def test_the_signature_must_account_for_a_majority_before_it_is_offered() -> None:
    frame, _ = F.inject(
        _daily(), F.CarriedForwardSettlement(rows=[2]), F.SwapHighLow(rows=[4, 5, 6])
    )
    assert I.insight_from(RULE_2, I.context(frame, Frequency.DAILY)) is None


# --------------------------------------------------------------------------- #
# 3 — feed incident: one date, a whole family
# --------------------------------------------------------------------------- #

RULE_3 = "feed_incident_date_cluster"


def _family(*contracts: str) -> pl.DataFrame:
    return F.merge_contracts(
        *(synth.daily_series(c, DAILY_DATES, seed=i) for i, c in enumerate(contracts))
    )


def test_one_date_breaking_a_whole_product_family_is_a_feed_incident() -> None:
    frame = _family("CLG26", "CLH26", "CLJ26")
    frame, _ = F.inject(frame, F.SwapHighLow(rows=_row_ids_on(frame, WEDNESDAY)))
    insight = I.insight_from(RULE_3, I.context(frame, Frequency.DAILY))

    assert insight is not None
    assert insight.rule.rule_id == "cross_contract_date_quarantine"
    assert insight.confidence == 1.0
    assert insight.evidence["incident_date_count"] == 1
    assert insight.evidence["incident_dates"] == [WEDNESDAY.isoformat()]
    assert insight.evidence["roots"] == ["CL"]
    assert insight.affected_contracts == ["CLG26", "CLH26", "CLJ26"]
    assert _params(insight)["quarantine"] == [{"root": "CL", "dates": [WEDNESDAY.isoformat()]}]
    # The summary supplies the denominator, so the prose can say "3 of the 3".
    assert "3 of the 3 CL contracts" in insight.pattern


def test_two_contracts_sharing_a_date_is_a_coincidence_not_an_incident() -> None:
    """Three, not two: with several contracts per root, two overlapping is unremarkable."""
    frame = _family("CLG26", "CLH26")
    frame, _ = F.inject(frame, F.SwapHighLow(rows=_row_ids_on(frame, WEDNESDAY)))
    assert I.insight_from(RULE_3, I.context(frame, Frequency.DAILY)) is None


def test_a_family_breaking_on_different_dates_is_not_an_incident() -> None:
    frame = _family("CLG26", "CLH26", "CLJ26")
    rows = [
        _row_ids_on(frame, TUESDAY, "CLG26")[0],
        _row_ids_on(frame, WEDNESDAY, "CLH26")[0],
        _row_ids_on(frame, THURSDAY, "CLJ26")[0],
    ]
    frame, _ = F.inject(frame, F.SwapHighLow(rows=rows))
    assert I.insight_from(RULE_3, I.context(frame, Frequency.DAILY)) is None


def test_findings_the_activity_model_already_downgraded_are_not_an_incident() -> None:
    """The gate that keeps the rule usable: 14,152 flat bars are INFO because nothing was
    trading, and every one of them would otherwise look like a family-wide feed fault."""
    frame = _family("CLG26", "CLH26", "CLJ26")
    frame, _ = F.inject(
        frame,
        F.FlatZeroVolumeBar(rows=_row_ids_on(frame, WEDNESDAY), severity=Severity.INFO),
    )
    assert I.insight_from(RULE_3, I.context(frame, Frequency.DAILY)) is None


def test_without_a_summary_the_insight_never_invents_a_denominator() -> None:
    frame = _family("CLG26", "CLH26", "CLJ26")
    frame, _ = F.inject(frame, F.SwapHighLow(rows=_row_ids_on(frame, WEDNESDAY)))
    insight = I.insight_from(RULE_3, I.context(frame, Frequency.DAILY, with_summary=False))
    assert insight is not None
    assert "of the" not in insight.pattern.split("simultaneously")[1]


# --------------------------------------------------------------------------- #
# 4 — holes cluster at the same time of day
# --------------------------------------------------------------------------- #

RULE_4 = "gaps_cluster_by_time_of_day"


def test_holes_that_start_in_the_same_hour_every_day_are_a_closure() -> None:
    frame, _ = F.inject(
        _calendar_minutes((TUESDAY, WEDNESDAY, THURSDAY)),
        _hole(TUESDAY, 10),
        _hole(WEDNESDAY, 10),
        _hole(THURSDAY, 10),
        _hole(THURSDAY, 13),
    )
    insight = I.insight_from(RULE_4, I.context(frame))

    assert insight is not None
    assert insight.rule.rule_id == "session_break_window"
    assert insight.confidence == 0.75
    assert insight.evidence["gap_count"] == 4
    assert insight.evidence["gaps_in_window"] == 3
    # A hole is bracketed by the surviving bars, so one that swallows 10:00-10:29 is
    # reported as the data stopping at 09:59 and resuming at 10:30 — which is the window
    # a venue would actually configure.
    assert insight.evidence["busiest_local_hour"] == "09:00"
    assert insight.evidence["window_local"] == "09:59-10:30"
    assert insight.evidence["gaps_by_local_hour"] == {"09:00": 3, "12:00": 1}
    assert _params(insight)["break_local"] == {"start": "09:59", "end": "10:30"}
    assert _params(insight)["exchange"] == "CME"


def test_holes_spread_through_the_day_are_outages_not_a_closure() -> None:
    """What the real minute files look like: ESH26's busiest hour holds 16% of its 70
    holes, which is thin overnight liquidity rather than a session break."""
    frame, _ = F.inject(
        _calendar_minutes((TUESDAY, WEDNESDAY)),
        _hole(TUESDAY, 9),
        _hole(TUESDAY, 11),
        _hole(WEDNESDAY, 13),
        _hole(WEDNESDAY, 15),
    )
    assert I.insight_from(RULE_4, I.context(frame)) is None


def test_no_holes_at_all_is_not_an_insight() -> None:
    assert I.insight_from(RULE_4, I.context(scenarios.minute_frame())) is None


def test_a_daily_report_never_produces_a_gap_insight() -> None:
    """`intrabar_gap` is minute-only, so this rule is silent on the 40 daily files."""
    assert I.insight_from(RULE_4, I.context(_daily(), Frequency.DAILY)) is None


# --------------------------------------------------------------------------- #
# 5 — holes cluster on one weekday
# --------------------------------------------------------------------------- #

RULE_5 = "gaps_cluster_by_weekday"


def test_holes_piled_on_one_weekday_are_a_calendar() -> None:
    frame, _ = F.inject(
        _calendar_minutes(WEEK),
        _hole(FRIDAY, 9),
        _hole(FRIDAY, 11),
        _hole(FRIDAY, 13),
        _hole(MONDAY, 9),
        _hole(TUESDAY, 11),
    )
    insight = I.insight_from(RULE_5, I.context(frame))

    assert insight is not None
    assert insight.rule.rule_id == "session_calendar"
    assert insight.confidence == 0.6
    assert insight.evidence["busiest_weekday"] == "Friday"
    assert insight.evidence["gaps_on_busiest_weekday"] == 3
    assert insight.evidence["gaps_by_weekday"] == {"Friday": 3, "Monday": 1, "Tuesday": 1}
    # The median hole on a Friday, not its extremes: one long outage must not stretch
    # the suggested calendar.
    assert _params(insight)["sessions"] == [
        {"weekday": "Friday", "close": "10:59", "open": "11:30"}
    ]


def test_holes_spread_across_the_week_are_not_a_calendar() -> None:
    frame, _ = F.inject(
        _calendar_minutes(WEEK),
        _hole(MONDAY, 9),
        _hole(TUESDAY, 10),
        _hole(WEDNESDAY, 11),
        _hole(THURSDAY, 13),
        _hole(FRIDAY, 14),
    )
    assert I.insight_from(RULE_5, I.context(frame)) is None


def test_the_weekday_skew_that_ordinary_liquidity_produces_is_not_a_calendar() -> None:
    """The gate is calibrated against real data, not against a uniform distribution. On
    the vendor's own ESH26 minute file the busiest weekday holds 38.6% of the holes with
    no closure involved, so anything at or below that has to stay silent."""
    frame, _ = F.inject(
        _calendar_minutes(WEEK),
        _hole(FRIDAY, 9),
        _hole(FRIDAY, 11),
        _hole(FRIDAY, 13),
        _hole(MONDAY, 9),
        _hole(MONDAY, 11),
        _hole(TUESDAY, 9),
        _hole(TUESDAY, 11),
        _hole(WEDNESDAY, 9),
    )
    assert I.insight_from(RULE_5, I.context(frame)) is None


def test_one_weekday_of_data_cannot_demonstrate_a_weekday_pattern() -> None:
    """100% of the holes fall on the only weekday present. That says nothing at all."""
    frame, _ = F.inject(
        _calendar_minutes((FRIDAY,)),
        _hole(FRIDAY, 9),
        _hole(FRIDAY, 10),
        _hole(FRIDAY, 11),
        _hole(FRIDAY, 12),
        _hole(FRIDAY, 13),
        _hole(FRIDAY, 14),
    )
    assert I.insight_from(RULE_5, I.context(frame)) is None


def test_too_few_holes_to_cluster_is_not_a_calendar() -> None:
    frame, _ = F.inject(
        _calendar_minutes((THURSDAY, FRIDAY)), _hole(FRIDAY, 9), _hole(THURSDAY, 11)
    )
    assert I.insight_from(RULE_5, I.context(frame)) is None


# --------------------------------------------------------------------------- #
# 6 — duplicates: re-delivery or contradiction
# --------------------------------------------------------------------------- #

RULE_6 = "duplicate_timestamp_shape"


def test_identical_duplicates_are_safe_to_collapse() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.ExactDuplicate(n=2))
    insight = I.insight_from(RULE_6, I.context(frame))

    assert insight is not None
    assert insight.rule.rule_id == "drop_exact_duplicates"
    assert insight.rule.kind == "cleansing"
    assert insight.confidence == 1.0
    assert insight.evidence["bars_in_exact_duplicates"] == 4
    assert insight.evidence["bars_in_conflicting_duplicates"] == 0
    assert insight.evidence["duplicated_instants"] == 2
    assert insight.evidence["largest_duplicate_group"] == 2
    assert _params(insight)["keep"] == "first"


def test_contradictory_duplicates_are_refused_rather_than_guessed_at() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.ConflictingDuplicate(n=2))
    insight = I.insight_from(RULE_6, I.context(frame))

    assert insight is not None
    assert insight.rule.rule_id == "reject_conflicting_duplicates"
    assert insight.rule.kind == "validation"
    assert insight.confidence == 1.0
    assert insight.evidence["bars_in_conflicting_duplicates"] == 4
    assert _params(insight)["keep"] == "none"


def test_one_contradiction_among_many_copies_still_gets_the_strict_rule() -> None:
    """Safety, not majority: a permissive rule would silently keep whichever
    contradictory copy happened to arrive first."""
    frame, _ = F.inject(
        scenarios.minute_frame(),
        F.ExactDuplicate(rows=[0, 1, 2]),
        F.ConflictingDuplicate(rows=[5]),
    )
    insight = I.insight_from(RULE_6, I.context(frame))

    assert insight is not None
    assert insight.rule.rule_id == "reject_conflicting_duplicates"
    assert insight.evidence["bars_in_exact_duplicates"] == 6
    assert insight.evidence["bars_in_conflicting_duplicates"] == 2
    assert insight.confidence == pytest.approx(0.25)


def test_the_repeated_fall_back_hour_is_named_rather_than_blamed_on_a_re_delivery() -> None:
    """Under `ambiguous="earliest"` both passes of the repeated hour get the same instant,
    so they trip this check by construction. No dedupe rule can recover them."""
    frame, _ = F.inject(scenarios.minute_frame(), F.AmbiguousLocalTime(bars=2, passes=2))
    insight = I.insight_from(RULE_6, I.context(frame))

    assert insight is not None
    assert insight.evidence["bars_on_an_ambiguous_wall_clock"] == 4
    assert "clock change" in insight.pattern
    assert "only an offset from the vendor can" in insight.pattern


def test_a_dataset_with_no_duplicates_produces_no_duplicate_insight() -> None:
    """The real sample is this case: 0 duplicates, reproduced against the vendor's own
    published count on 40 of 40 daily files."""
    assert I.insight_from(RULE_6, I.context(scenarios.minute_frame())) is None


# --------------------------------------------------------------------------- #
# 7 — absent sessions corroborated across the exchange
# --------------------------------------------------------------------------- #

RULE_7 = "missing_sessions_are_holidays"


def test_a_day_no_contract_traded_is_a_holiday() -> None:
    frame, _ = F.inject(
        scenarios.two_contract_frame(), F.DropSession(WEDNESDAY, severity=Severity.INFO)
    )
    insight = I.insight_from(RULE_7, I.context(frame))

    assert insight is not None
    assert insight.rule.rule_id == "holiday_calendar"
    assert insight.confidence == 1.0
    assert insight.evidence["holiday_dates"] == [WEDNESDAY.isoformat()]
    assert insight.evidence["sessions_missing_exchange_wide"] == 2
    assert insight.evidence["dates_by_weekday"] == {"Wednesday": 1}
    assert _params(insight)["calendars"] == [
        {"exchange": "CME", "dates": [WEDNESDAY.isoformat()], "date_count": 1}
    ]


def test_one_contract_absent_while_its_siblings_traded_is_a_hole_not_a_holiday() -> None:
    frame, _ = F.inject(
        scenarios.two_contract_frame(),
        F.DropSession(WEDNESDAY, contract="ESM26", severity=Severity.WARNING),
    )
    assert I.insight_from(RULE_7, I.context(frame)) is None


def test_a_single_contract_cannot_corroborate_its_own_absence() -> None:
    """With no siblings every absence is vacuously exchange-wide, and a holiday calendar
    assembled from one contract's quiet days would be nonsense."""
    frame = synth.minute_sessions("ESH26", WEEK, bars_per_session=120, seed=4)
    frame, _ = F.inject(frame, F.DropSession(WEDNESDAY, severity=Severity.INFO))
    assert I.insight_from(RULE_7, I.context(frame)) is None


def test_a_dataset_with_no_absent_sessions_produces_no_calendar() -> None:
    assert I.insight_from(RULE_7, I.context(scenarios.two_contract_frame())) is None


# --------------------------------------------------------------------------- #
# 8 — zero volume with a range, while the market was busy
# --------------------------------------------------------------------------- #

RULE_8 = "zero_volume_with_range_while_active"


def test_an_untraded_bar_that_moved_in_a_busy_session_is_a_contradiction() -> None:
    frame, _ = F.inject(
        scenarios.minute_frame(), F.ZeroVolumeWithRange(rows=[10, 11], severity=Severity.WARNING)
    )
    insight = I.insight_from(RULE_8, I.context(frame))

    assert insight is not None
    assert insight.rule.rule_id == "zero_volume_requires_flat"
    assert insight.rule.kind == "validation"
    assert insight.confidence == 1.0
    assert insight.evidence["bars_in_active_sessions"] == 2
    assert insight.evidence["bars_in_quiet_sessions"] == 0
    assert insight.evidence["bars_by_regime"] == {"ACTIVE": 2}
    assert insight.evidence["widest_range_in_an_active_session"] > 0
    assert _params(insight)["assert"] == "volume > 0 or high == low"


def test_the_same_shape_on_a_day_nothing_traded_is_ordinary() -> None:
    """All 400 of these in the real corpus are dormant-session carried quotes, and none
    is worth chasing."""
    frame, _ = F.inject(_daily(), F.ZeroVolumeWithRange(rows=[2], severity=Severity.INFO))
    assert I.insight_from(RULE_8, I.context(frame, Frequency.DAILY)) is None


def test_no_untraded_moving_bars_at_all_is_not_an_insight() -> None:
    assert I.insight_from(RULE_8, I.context(scenarios.minute_frame())) is None


# --------------------------------------------------------------------------- #
# 9 — bad input concentrated in one field
# --------------------------------------------------------------------------- #

RULE_9 = "bad_input_concentrates_in_one_field"


def test_nulls_piling_up_in_one_column_argue_for_a_schema_contract() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.NullField(C.VOLUME, rows=[10, 11, 12]))
    insight = I.insight_from(RULE_9, I.context(frame))

    assert insight is not None
    assert insight.rule.rule_id == "schema_contract"
    assert insight.confidence == 1.0
    assert insight.evidence["worst_field"] == "volume"
    assert insight.evidence["declared_type"] == "integer"
    assert insight.evidence["bad_values_by_field"] == {"volume": 3}
    assert _params(insight)["column"] == "volume"
    assert _params(insight)["type"] == "integer"
    assert _params(insight)["required"] is True


def test_lines_that_never_became_fields_argue_for_a_layout_contract() -> None:
    frame, rejects, _ = F.inject_with_rejects(
        scenarios.minute_frame(), F.MalformedLine(row_ids=(900_100, 900_101, 900_102))
    )
    insight = I.insight_from(RULE_9, I.context(frame, rejects=rejects))

    assert insight is not None
    assert insight.evidence["worst_field"] is None
    assert insight.evidence["declared_type"] == "record"
    assert insight.evidence["rejects_by_reason"] == {"MALFORMED_LINE": 3}
    assert insight.evidence["sources"] == ["<fault>"]
    assert insight.evidence["example_value"].startswith("ESH26,")
    assert _params(insight)["column"] is None
    assert _params(insight)["applies_to"] == "the whole row"
    assert "never split into columns" in insight.pattern


def test_an_unplaceable_timestamp_names_the_timestamp_column() -> None:
    frame, rejects, _ = F.inject_with_rejects(scenarios.minute_frame(), F.NonexistentLocalTime())
    insight = I.insight_from(RULE_9, I.context(frame, rejects=rejects))

    assert insight is not None
    assert insight.evidence["worst_field"] == "timestamp"
    assert insight.evidence["declared_type"] == "timestamp"


def test_a_little_of_everything_wrong_is_a_different_problem() -> None:
    """Spread damage must not send anyone to argue with a supplier about one column."""
    frame, _ = F.inject(
        scenarios.minute_frame(),
        F.NullField(C.OPEN, rows=[10]),
        F.NullField(C.HIGH, rows=[11]),
        F.NullField(C.LOW, rows=[12]),
        F.NullField(C.CLOSE, rows=[13]),
    )
    assert I.insight_from(RULE_9, I.context(frame)) is None


def test_a_dataset_with_no_bad_input_produces_no_schema_contract() -> None:
    """The real sample is this case: 0 rejects and 0 nulls across 5.3M values."""
    assert I.insight_from(RULE_9, I.context(scenarios.minute_frame())) is None


# --------------------------------------------------------------------------- #
# 10 — the daily close is a settlement price
# --------------------------------------------------------------------------- #

RULE_10 = "daily_close_is_a_settlement_price"


def test_the_check_this_rule_consumes_does_not_exist_yet() -> None:
    """PLAN §4.3 defers `cross_frequency_mismatch` to WP8. The rule must survive that."""
    assert "cross_frequency_mismatch" not in CheckRegistry.ids()


def test_the_rule_degrades_to_silence_when_its_check_has_not_run() -> None:
    cases = ((scenarios.minute_frame(), Frequency.MINUTE), (_daily(), Frequency.DAILY))
    for frame, frequency in cases:
        assert I.insight_from(RULE_10, I.context(frame, frequency)) is None


def test_breaks_concentrated_in_the_close_are_the_settlement_convention() -> None:
    report = I.report_of(
        [
            _mismatch("close", 0.004),
            _mismatch("close", 0.006),
            _mismatch("close", 0.003),
            _mismatch("high", 0.0012),
        ]
    )
    insight = I.insight_from(RULE_10, I.context_for(report))

    assert insight is not None
    assert insight.rule.rule_id == "reconcile_daily_vs_minute"
    assert insight.confidence == 0.75
    assert insight.evidence["breaks_on_close"] == 3
    assert insight.evidence["breaks_on_other_fields"] == 1
    assert insight.evidence["breaks_by_field"] == {"close": 3, "high": 1}
    assert _params(insight)["skip"] == ["close"]
    assert _params(insight)["fields"] == ["high", "low", "volume"]
    # Sized from the breaks the proposed rule will actually make, not from the close.
    assert _params(insight)["tolerance"]["price_relative"] == 0.0012
    assert _params(insight)["tolerance"]["volume_ratio"] == [0.8, 1.05]


def test_breaks_that_are_mostly_elsewhere_are_a_real_reconciliation_problem() -> None:
    report = I.report_of([_mismatch("close"), _mismatch("volume"), _mismatch("high")])
    assert I.insight_from(RULE_10, I.context_for(report)) is None


def test_a_break_with_no_field_is_counted_but_attributed_to_nothing() -> None:
    """A partial WP8 implementation must not be silently rounded up into a clean story."""
    unattributed = Finding(
        check_id="cross_frequency_mismatch",
        severity=Severity.WARNING,
        contract="ESH26",
        frequency=Frequency.DAILY,
        message="something disagrees",
        evidence={"row_ids": []},
    )
    report = I.report_of([_mismatch("close"), _mismatch("close"), _mismatch("close"), unattributed])
    insight = I.insight_from(RULE_10, I.context_for(report))

    assert insight is not None
    assert insight.evidence["breaks_by_field"] == {"(unattributed)": 1, "close": 3}
    assert insight.confidence == 0.75
    # No comparable break to measure, so the documented default rather than zero.
    assert _params(insight)["tolerance"]["price_relative"] == 0.0005


# --------------------------------------------------------------------------- #
# the edges: collateral damage, wider datasets, and evidence that moved
# --------------------------------------------------------------------------- #


def test_an_incident_names_the_other_products_that_broke_the_same_day() -> None:
    """What turns "the CL family broke" into "that day's delivery was bad": on the real
    2022-01-13 an ES and a ZC contract failed the same check on the same date."""
    frame = _family("CLG26", "CLH26", "CLJ26", "ESH26")
    frame, _ = F.inject(frame, F.SwapHighLow(rows=_row_ids_on(frame, WEDNESDAY)))
    insight = I.insight_from(RULE_3, I.context(frame, Frequency.DAILY))

    assert insight is not None
    assert "ESH26 failed the same check on the same date" in insight.pattern
    assert insight.evidence["incidents"][0]["contracts_other_roots"] == ["ESH26"]


def _exchange_frame(*exchanges: str) -> pl.DataFrame:
    """Two contracts on each of several exchanges, over one clean week."""
    return F.merge_contracts(
        *(
            synth.minute_sessions(
                f"{code}{month}26",
                WEEK[:4],
                bars_per_session=60,
                seed=index,
                exchange=exchange,
            )
            for index, (exchange, code) in enumerate(zip(exchanges, "ABCDE", strict=False))
            for month in ("H", "M")
        )
    )


def test_a_holiday_across_two_exchanges_names_both() -> None:
    frame, _ = F.inject(
        _exchange_frame("CME", "NYMEX"), F.DropSession(WEDNESDAY, severity=Severity.INFO)
    )
    insight = I.insight_from(RULE_7, I.context(frame))

    assert insight is not None
    assert insight.evidence["exchanges"] == ["CME", "NYMEX"]
    assert "CME and NYMEX" in insight.pattern
    assert [c["exchange"] for c in _params(insight)["calendars"]] == ["CME", "NYMEX"]


def test_a_holiday_across_many_exchanges_is_summarised_not_listed() -> None:
    """The real corpus spans six exchanges; the prose must not become a list of them."""
    frame, _ = F.inject(
        _exchange_frame("CBOT", "CFE", "CME", "NYMEX"),
        F.DropSession(WEDNESDAY, severity=Severity.INFO),
    )
    insight = I.insight_from(RULE_7, I.context(frame))

    assert insight is not None
    assert "CBOT, CFE and 2 other exchange(s)" in insight.pattern
    assert len(_params(insight)["calendars"]) == 4


def test_bad_input_arriving_across_several_files_says_so() -> None:
    """`malformed_record` samples one source per (contract, reason) group, so the count
    is a lower bound and the prose says so rather than overclaiming."""
    rejects = F.rejects_frame(
        [
            {
                C.ROW_ID: 900_100 + i,
                C.CONTRACT: contract,
                C.REASON: "MALFORMED_LINE",
                "raw_values": f"{contract},2026-03-03 17:00:00,5000.0",
                "source": source,
            }
            for i, (contract, source) in enumerate(
                [("ESH26", "monday.csv"), ("ESM26", "tuesday.csv"), ("ESU26", "tuesday.csv")]
            )
        ]
    )
    insight = I.insight_from(RULE_9, I.context(scenarios.minute_frame(), rejects=rejects))

    assert insight is not None
    assert insight.evidence["sources"] == ["monday.csv", "tuesday.csv"]
    assert "at least 2 different files" in insight.pattern


def _gap_finding(evidence: dict[str, Any]) -> Finding:
    return Finding(
        check_id="intrabar_gap",
        severity=Severity.WARNING,
        contract="ESH26",
        frequency=Frequency.MINUTE,
        message="a hole",
        count=30,
        session_date=WEDNESDAY,
        evidence={"row_ids": [], **evidence},
    )


@pytest.mark.parametrize("rule_id", [RULE_4, RULE_5])
def test_a_gap_finding_without_a_start_time_is_ignored_rather_than_fatal(
    rule_id: str,
) -> None:
    """Both gap rules read an evidence key the check writes today. If a future check
    stopped writing it they must fall silent, not explode or invent a window."""
    report = I.report_of([_gap_finding({"gap_minutes": 31}) for _ in range(6)])
    assert I.insight_from(rule_id, I.context_for(report)) is None


def test_an_untraded_moving_bar_with_no_measured_range_omits_the_number() -> None:
    report = I.report_of(
        [
            Finding(
                check_id="zero_volume_with_range",
                severity=Severity.WARNING,
                contract="ESH26",
                frequency=Frequency.MINUTE,
                message="untraded but moving",
                count=2,
                session_date=WEDNESDAY,
                evidence={"row_ids": [], "regime": "ACTIVE"},
            )
        ],
        Frequency.MINUTE,
    )
    insight = I.insight_from(RULE_8, I.context_for(report))

    assert insight is not None
    assert insight.evidence["widest_range_in_an_active_session"] is None
    assert "The widest of them" not in insight.pattern


def test_a_reconciliation_break_with_no_measured_difference_uses_the_default() -> None:
    unmeasured = Finding(
        check_id="cross_frequency_mismatch",
        severity=Severity.WARNING,
        contract="ESH26",
        frequency=Frequency.DAILY,
        message="high disagrees",
        evidence={"row_ids": [], "field": "high"},
    )
    report = I.report_of([_mismatch("close"), _mismatch("close"), _mismatch("close"), unmeasured])
    insight = I.insight_from(RULE_10, I.context_for(report))

    assert insight is not None
    assert _params(insight)["tolerance"]["price_relative"] == 0.0005
