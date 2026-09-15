"""Rolling VWAP: the window must respect time, sessions, and missing volume."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from mdq.analytics.vwap import (
    BARS_IN_WINDOW,
    VWAP,
    VWAP_SCHEMA,
    WINDOW_START_UTC,
    WINDOW_VOLUME,
    PriceSource,
    RollingVwap,
    VwapParams,
    rolling_vwap,
)
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame, C
from support.handmade import Bar, bar_frame

pytestmark = pytest.mark.unit

#: 17:00 on the 3rd opens the session that settles on the 4th.
OPEN_WALL = datetime(2026, 3, 3, 17, 0)


def _minutes(count: int, *, start: datetime = OPEN_WALL, **kwargs: object) -> list[Bar]:
    """`count` consecutive one-minute bars from `start`."""
    return [Bar(start + timedelta(minutes=i), **kwargs) for i in range(count)]  # type: ignore[arg-type]


# --- the arithmetic ---------------------------------------------------------- #


def test_vwap_is_volume_weighted_over_the_window() -> None:
    bars = bar_frame(
        [
            Bar(OPEN_WALL, high=12.0, low=6.0, close=12.0, volume=1),  # typical 10
            Bar(OPEN_WALL.replace(minute=1), high=22.0, low=16.0, close=22.0, volume=3),  # 20
        ]
    )
    result = rolling_vwap(bars, {"window": "15m"})

    assert result.schema == VWAP_SCHEMA
    assert result[VWAP].to_list() == [10.0, pytest.approx((10 * 1 + 20 * 3) / 4)]
    assert result[WINDOW_VOLUME].to_list() == [1, 4]
    assert result[BARS_IN_WINDOW].to_list() == [1, 2]


def test_the_default_price_is_the_typical_price_not_the_close() -> None:
    bars = bar_frame([Bar(OPEN_WALL, high=30.0, low=0.0, close=30.0, volume=1)])

    typical = rolling_vwap(bars)[VWAP][0]
    closing = rolling_vwap(bars, {"price": PriceSource.CLOSE})[VWAP][0]

    assert typical == pytest.approx(20.0)
    assert closing == pytest.approx(30.0)


def test_the_window_is_measured_in_time_not_in_rows() -> None:
    """Sixty one-minute bars, a 15-minute window: never more than 15 bars."""
    bars = bar_frame(_minutes(60))
    result = rolling_vwap(bars, {"window": "15m"})

    assert result[BARS_IN_WINDOW].max() == 15
    assert result[BARS_IN_WINDOW].to_list()[:3] == [1, 2, 3]


def test_input_order_does_not_change_the_answer() -> None:
    forwards = _minutes(10)
    backwards = list(reversed(forwards))
    assert rolling_vwap(bar_frame(forwards)).equals(rolling_vwap(bar_frame(backwards)))


# --- never divide by zero ---------------------------------------------------- #


def test_a_window_of_only_zero_volume_bars_has_no_vwap() -> None:
    bars = bar_frame(_minutes(3, volume=0))
    result = rolling_vwap(bars, {"window": "15m"})

    assert result[VWAP].to_list() == [None, None, None]
    assert result[WINDOW_VOLUME].to_list() == [0, 0, 0]
    assert result[BARS_IN_WINDOW].to_list() == [1, 2, 3]  # the bars are still visible


def test_volume_arriving_revives_the_vwap_and_never_falls_back_to_price() -> None:
    bars = bar_frame(
        [
            Bar(OPEN_WALL, high=12.0, low=6.0, close=12.0, volume=0),
            Bar(OPEN_WALL.replace(minute=1), high=22.0, low=16.0, close=22.0, volume=2),
        ]
    )
    result = rolling_vwap(bars, {"window": "15m"})

    assert result[VWAP][0] is None  # not 10.0, the bar's own price
    assert result[VWAP][1] == pytest.approx(20.0)  # the zero-volume bar contributes nothing


def test_bars_with_a_null_price_leave_the_average_alone() -> None:
    bars = bar_frame(
        [
            Bar(OPEN_WALL, high=12.0, low=6.0, close=12.0, volume=1),
            Bar(OPEN_WALL.replace(minute=1), high=None, low=None, close=None, volume=1_000),
        ]
    )
    result = rolling_vwap(bars, {"window": "15m"})

    assert result[VWAP].to_list() == [10.0, 10.0]
    assert result[WINDOW_VOLUME].to_list() == [1, 1]  # the unusable 1,000 is excluded
    assert result[BARS_IN_WINDOW].to_list() == [1, 2]  # but the bar is still counted


def test_bars_with_a_null_volume_are_excluded_too() -> None:
    bars = bar_frame([Bar(OPEN_WALL, volume=None)])
    result = rolling_vwap(bars)

    assert result[VWAP][0] is None
    assert result[WINDOW_VOLUME][0] == 0


def test_min_volume_suppresses_a_thinly_traded_window() -> None:
    bars = bar_frame(_minutes(3, volume=10))
    result = rolling_vwap(bars, {"window": "15m", "min_volume": 25})

    assert result[WINDOW_VOLUME].to_list() == [10, 20, 30]
    assert result[VWAP][0] is None
    assert result[VWAP][1] is None
    assert result[VWAP][2] is not None


# --- gaps and session boundaries --------------------------------------------- #


def test_a_gap_longer_than_the_window_leaves_a_bar_alone() -> None:
    bars = bar_frame(
        [
            Bar(OPEN_WALL),
            Bar(OPEN_WALL.replace(minute=1)),
            Bar(datetime(2026, 3, 3, 17, 41)),  # 40 minutes later
        ]
    )
    result = rolling_vwap(bars, {"window": "15m"})

    assert result[BARS_IN_WINDOW].to_list() == [1, 2, 1]
    assert result[WINDOW_START_UTC][2] == result[C.TS_UTC][2]


def test_window_start_reports_the_earliest_bar_present_not_the_nominal_boundary() -> None:
    bars = bar_frame(_minutes(20))
    result = rolling_vwap(bars, {"window": "15m"})
    last = result.row(result.height - 1, named=True)

    # 15-minute window, closed on the right: the earliest bar inside it is t - 14min.
    assert (last[C.TS_UTC] - last[WINDOW_START_UTC]).total_seconds() == 14 * 60


def test_a_window_never_reaches_across_the_maintenance_break() -> None:
    """15:59 and 17:00 are 61 minutes apart — and, more importantly, two sessions."""
    bars = bar_frame(
        [
            Bar(datetime(2026, 3, 4, 15, 58)),
            Bar(datetime(2026, 3, 4, 15, 59)),
            Bar(datetime(2026, 3, 4, 17, 0)),
            Bar(datetime(2026, 3, 4, 17, 1)),
        ]
    )
    # A two-hour window would happily span the break on elapsed time alone; only the
    # session grouping stops it.
    result = rolling_vwap(bars, {"window": "2h"})

    assert result[C.SESSION_DATE].to_list() == [date(2026, 3, 4)] * 2 + [date(2026, 3, 5)] * 2
    assert result[BARS_IN_WINDOW].to_list() == [1, 2, 1, 2]


def test_a_window_never_reaches_from_sunday_evening_back_into_friday() -> None:
    bars = bar_frame(
        [
            Bar(datetime(2026, 3, 6, 15, 58)),  # Friday, session 2026-03-06
            Bar(datetime(2026, 3, 6, 15, 59)),
            Bar(datetime(2026, 3, 8, 17, 0)),  # Sunday open, session 2026-03-09
            Bar(datetime(2026, 3, 8, 17, 1)),
        ]
    )
    result = rolling_vwap(bars, {"window": "5d"})

    assert result[C.SESSION_DATE].to_list() == [date(2026, 3, 6)] * 2 + [date(2026, 3, 9)] * 2
    assert result[BARS_IN_WINDOW].to_list() == [1, 2, 1, 2]


def test_the_1700_roll_separates_two_bars_a_minute_apart() -> None:
    bars = bar_frame([Bar(datetime(2026, 3, 4, 16, 59)), Bar(datetime(2026, 3, 4, 17, 0))])
    result = rolling_vwap(bars, {"window": "1h"})

    assert result[C.SESSION_DATE].to_list() == [date(2026, 3, 4), date(2026, 3, 5)]
    assert result[BARS_IN_WINDOW].to_list() == [1, 1]


def test_contracts_do_not_bleed_into_each_others_windows() -> None:
    bars = bar_frame(
        [
            Bar(OPEN_WALL, high=12.0, low=6.0, close=12.0, volume=1, contract="ESH26"),
            Bar(OPEN_WALL, high=42.0, low=36.0, close=42.0, volume=1, contract="NQH26"),
        ]
    )
    result = rolling_vwap(bars, {"window": "15m"})

    assert result[C.CONTRACT].to_list() == ["ESH26", "NQH26"]
    assert result[VWAP].to_list() == [10.0, 40.0]
    assert result[BARS_IN_WINDOW].to_list() == [1, 1]


def test_the_two_fall_back_bars_share_an_instant_and_therefore_a_window() -> None:
    """Under `ambiguous="earliest"` the repeated 01:30 resolves to one instant.

    The vendor gives no UTC offset, so the two passes of the fall-back hour are
    genuinely indistinguishable; they land in the same window and the *ambiguity* is
    what the quality layer reports, not a fabricated 60-minute separation.
    """
    bars = bar_frame(
        [
            Bar(datetime(2025, 11, 2, 1, 30), high=12.0, low=6.0, close=12.0, volume=1),
            Bar(datetime(2025, 11, 2, 1, 30), high=22.0, low=16.0, close=22.0, volume=1),
        ]
    )
    result = rolling_vwap(bars, {"window": "15m"})

    assert result[C.TS_UTC][0] == result[C.TS_UTC][1]
    assert result[C.SESSION_DATE].to_list() == [date(2025, 11, 2)] * 2
    assert result[BARS_IN_WINDOW].to_list() == [2, 2]
    assert result[VWAP].to_list() == [pytest.approx(15.0)] * 2


# --- parameters and edges ----------------------------------------------------- #


def test_empty_input_yields_an_empty_frame_with_the_right_schema() -> None:
    result = rolling_vwap(BarFrame.empty(Frequency.MINUTE))

    assert result.height == 0
    assert result.schema == VWAP_SCHEMA


@pytest.mark.parametrize("window", ["15m", "1h", "1h30m", "500ms", "1d"])
def test_valid_duration_strings_are_accepted(window: str) -> None:
    assert VwapParams(window=window).window == window


@pytest.mark.parametrize("window", ["15 minutes", "fifteen", "15", "-15m", "", "15mo"])
def test_a_window_that_is_not_a_duration_is_refused(window: str) -> None:
    with pytest.raises(ValidationError, match="not a polars duration string"):
        VwapParams(window=window)


@pytest.mark.parametrize("window", ["0m", "0s0m"])
def test_a_zero_length_window_is_refused(window: str) -> None:
    with pytest.raises(ValidationError, match="must be a positive duration"):
        VwapParams(window=window)


def test_surrounding_whitespace_is_forgiven() -> None:
    assert VwapParams(window=" 15m ").window == "15m"


def test_a_negative_minimum_volume_is_refused() -> None:
    with pytest.raises(ValidationError):
        VwapParams(min_volume=-1)


def test_unknown_parameters_are_refused_so_the_api_answers_422() -> None:
    with pytest.raises(ValidationError):
        VwapParams(windwo="15m")  # type: ignore[call-arg]


def test_the_registered_analytic_is_minute_only_and_wraps_the_function() -> None:
    bars = bar_frame(_minutes(5))
    analytic = RollingVwap()

    assert analytic.frequencies == frozenset({Frequency.MINUTE})
    assert analytic.run(bars).equals(rolling_vwap(bars))
    assert analytic.run(bars, VwapParams(window="5m")).equals(rolling_vwap(bars, {"window": "5m"}))
