"""Session aggregation: the roll, the ordering, and the identity on daily input."""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from mdq.analytics.daily_bars import (
    BAR_COUNT,
    DAILY_BARS_SCHEMA,
    FIRST_TS,
    LAST_TS,
    NULL_PRICE_BARS,
    DailyBars,
    daily_bars,
)
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame, C
from support import synth
from support.handmade import Bar, bar_frame

pytestmark = pytest.mark.unit


def test_a_dense_session_collapses_to_one_row(minute_bars: BarFrame) -> None:
    source = minute_bars.collect()
    result = daily_bars(minute_bars)

    assert result.schema == DAILY_BARS_SCHEMA
    assert result.height == source[C.SESSION_DATE].n_unique()
    assert result[C.CONTRACT].to_list() == ["ESH26"] * result.height


def test_ohlcv_matches_the_session_it_summarises() -> None:
    session = date(2026, 3, 4)
    frame = synth.minute_sessions("ESH26", [session], bars_per_session=60, seed=11)
    bars = synth.as_bar_frame(frame, Frequency.MINUTE)

    row = daily_bars(bars).row(0, named=True)
    ordered = frame.sort(C.TS_UTC, C.ROW_ID)

    assert row[C.OPEN] == ordered[C.OPEN][0]
    assert row[C.CLOSE] == ordered[C.CLOSE][-1]
    assert row[C.HIGH] == frame[C.HIGH].max()
    assert row[C.LOW] == frame[C.LOW].min()
    assert row[C.VOLUME] == frame[C.VOLUME].sum()
    assert row[BAR_COUNT] == 60
    assert row[FIRST_TS] == ordered[C.TS_UTC][0]
    assert row[LAST_TS] == ordered[C.TS_UTC][-1]
    assert row[NULL_PRICE_BARS] == 0


def test_open_is_the_first_bar_in_time_not_the_cheapest() -> None:
    """A session that falls all day must still report the price it opened at."""
    bars = bar_frame(
        [
            Bar(datetime(2026, 3, 3, 17, 0), open=100.0, high=100.0, low=100.0, close=100.0),
            Bar(datetime(2026, 3, 3, 17, 1), open=90.0, high=90.0, low=80.0, close=85.0),
            Bar(datetime(2026, 3, 3, 17, 2), open=85.0, high=86.0, low=84.0, close=84.0),
        ]
    )
    row = daily_bars(bars).row(0, named=True)

    assert row[C.OPEN] == 100.0  # not min(open) == 85.0
    assert row[C.CLOSE] == 84.0  # not max/min close
    assert row[C.HIGH] == 100.0
    assert row[C.LOW] == 80.0


def test_input_order_does_not_change_the_answer() -> None:
    walls = [datetime(2026, 3, 3, 17, minute) for minute in range(5)]
    forwards = [Bar(w, open=100.0 + i, close=100.0 + i) for i, w in enumerate(walls)]
    backwards = list(reversed(forwards))

    assert daily_bars(bar_frame(forwards)).equals(daily_bars(bar_frame(backwards)))


def test_the_1700_roll_splits_one_wall_date_into_two_sessions() -> None:
    """16:59 and 17:00 CT on the same wall date belong to different trading days."""
    bars = bar_frame(
        [
            Bar(datetime(2026, 3, 4, 16, 59), close=10.0),
            Bar(datetime(2026, 3, 4, 17, 0), close=20.0),
        ]
    )
    result = daily_bars(bars)

    assert result[C.SESSION_DATE].to_list() == [date(2026, 3, 4), date(2026, 3, 5)]
    assert result[C.CLOSE].to_list() == [10.0, 20.0]
    assert result[BAR_COUNT].to_list() == [1, 1]


def test_contracts_are_aggregated_independently() -> None:
    bars = bar_frame(
        [
            Bar(datetime(2026, 3, 3, 17, 0), close=1.0, volume=5, contract="ESH26"),
            Bar(datetime(2026, 3, 3, 17, 1), close=2.0, volume=7, contract="ESH26"),
            Bar(datetime(2026, 3, 3, 17, 0), close=3.0, volume=9, contract="NQH26"),
        ]
    )
    result = daily_bars(bars)

    assert result[C.CONTRACT].to_list() == ["ESH26", "NQH26"]
    assert result[C.VOLUME].to_list() == [12, 9]


def test_bars_with_a_null_price_are_counted_not_dropped() -> None:
    bars = bar_frame(
        [
            Bar(datetime(2026, 3, 3, 17, 0), close=10.0),
            Bar(datetime(2026, 3, 3, 17, 1), open=None, close=11.0),
            Bar(datetime(2026, 3, 3, 17, 2), high=None, close=12.0),
        ]
    )
    row = daily_bars(bars).row(0, named=True)

    assert row[BAR_COUNT] == 3
    assert row[NULL_PRICE_BARS] == 2
    assert row[C.CLOSE] == 12.0


def test_a_null_open_on_the_first_bar_is_preserved_rather_than_back_filled() -> None:
    bars = bar_frame(
        [
            Bar(datetime(2026, 3, 3, 17, 0), open=None),
            Bar(datetime(2026, 3, 3, 17, 1), open=42.0),
        ]
    )
    assert daily_bars(bars).row(0, named=True)[C.OPEN] is None


def test_a_session_with_no_volume_reported_at_all_is_null_not_zero() -> None:
    """Zero volume is a claim about the market; null is a claim about the data."""
    bars = bar_frame(
        [
            Bar(datetime(2026, 3, 3, 17, 0), volume=None),
            Bar(datetime(2026, 3, 3, 17, 1), volume=None),
        ]
    )
    assert daily_bars(bars).row(0, named=True)[C.VOLUME] is None


def test_partially_reported_volume_sums_what_is_known() -> None:
    bars = bar_frame(
        [
            Bar(datetime(2026, 3, 3, 17, 0), volume=None),
            Bar(datetime(2026, 3, 3, 17, 1), volume=4),
        ]
    )
    assert daily_bars(bars).row(0, named=True)[C.VOLUME] == 4


def test_daily_input_is_the_identity_with_bar_count_one(daily_bars_frame: BarFrame) -> None:
    source = daily_bars_frame.collect().sort(C.CONTRACT, C.SESSION_DATE)
    result = daily_bars(daily_bars_frame)

    assert result[BAR_COUNT].to_list() == [1] * source.height
    for column in (C.CONTRACT, C.SESSION_DATE, *C.PRICES, C.VOLUME):
        assert result[column].to_list() == source[column].to_list()
    assert result[FIRST_TS].to_list() == source[C.TS_UTC].to_list()
    assert result[LAST_TS].to_list() == source[C.TS_UTC].to_list()
    assert result[NULL_PRICE_BARS].to_list() == [0] * source.height


def test_minute_and_daily_views_of_one_session_agree_on_shape() -> None:
    """One endpoint serves both frequencies because both produce the same columns."""
    session = [date(2026, 3, 4)]
    minute = synth.as_bar_frame(
        synth.minute_sessions("ESH26", session, bars_per_session=30, seed=3),
        Frequency.MINUTE,
    )
    daily = synth.as_bar_frame(synth.daily_series("ESH26", session, seed=3), Frequency.DAILY)

    from_minute, from_daily = daily_bars(minute), daily_bars(daily)
    assert from_minute.schema == from_daily.schema == DAILY_BARS_SCHEMA
    assert from_minute[C.SESSION_DATE].to_list() == from_daily[C.SESSION_DATE].to_list()
    assert from_minute[BAR_COUNT][0] == 30
    assert from_daily[BAR_COUNT][0] == 1


@pytest.mark.parametrize("frequency", [Frequency.MINUTE, Frequency.DAILY])
def test_empty_input_yields_an_empty_frame_with_the_right_schema(frequency: Frequency) -> None:
    result = daily_bars(BarFrame.empty(frequency))

    assert result.height == 0
    assert result.schema == DAILY_BARS_SCHEMA


def test_the_registered_analytic_wraps_the_function(minute_bars: BarFrame) -> None:
    analytic = DailyBars()

    assert analytic.run(minute_bars).equals(daily_bars(minute_bars))
    assert analytic.run(minute_bars, {}).equals(daily_bars(minute_bars))
    assert analytic.frequencies == frozenset({Frequency.MINUTE, Frequency.DAILY})


def test_the_analytic_rejects_parameters_it_does_not_have(minute_bars: BarFrame) -> None:
    with pytest.raises(ValueError, match="window"):
        DailyBars().run(minute_bars, {"window": "15m"})


@pytest.fixture
def daily_bars_frame() -> BarFrame:
    """Daily bars for two contracts, to prove the identity holds per contract."""
    dates = [date(2026, 3, 3), date(2026, 3, 4)]
    frame = pl.concat(
        [
            synth.daily_series("ESH26", dates, seed=1),
            synth.daily_series("NQH26", dates, seed=2),
        ]
    )
    return synth.as_bar_frame(frame, Frequency.DAILY)
