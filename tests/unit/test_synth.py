"""The synthetic builder must be perfectly clean — every later check trusts it.

If these tests fail, every "clean data yields zero findings" property downstream is
measuring the builder's bugs rather than the checks'.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl
import pytest

from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, BarFrame, C
from support import synth

pytestmark = pytest.mark.unit

MONDAY = date(2026, 3, 9)
WEEK = (date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5))


def test_minute_frame_conforms_to_bar_schema() -> None:
    df = synth.minute_sessions("ESH26", [MONDAY], bars_per_session=10)
    assert df.schema == BAR_SCHEMA
    assert BarFrame(df.lazy(), Frequency.MINUTE).collect().height == 10


def test_daily_frame_conforms_to_bar_schema() -> None:
    df = synth.daily_series("ESH26", WEEK)
    assert df.schema == BAR_SCHEMA
    assert BarFrame(df.lazy(), Frequency.DAILY).collect().height == 3


def test_a_full_cme_session_is_1380_bars_from_1700_to_1559() -> None:
    df = synth.minute_sessions("ESH26", [MONDAY], bars_per_session=1380)
    assert df.height == 1380
    assert df[C.TS_LOCAL][0] == datetime(2026, 3, 8, 17, 0)
    assert df[C.TS_LOCAL][-1] == datetime(2026, 3, 9, 15, 59)
    assert df[C.SESSION_DATE].unique().to_list() == [MONDAY]


def test_minute_bars_are_dense_unique_and_ordered() -> None:
    df = synth.minute_sessions("ESH26", WEEK[:2], bars_per_session=60)
    deltas = df[C.TS_UTC].diff().drop_nulls().unique().to_list()
    # Only two spacings: one minute inside a session, and the overnight jump.
    assert timedelta(minutes=1) in deltas
    assert len(deltas) == 2
    assert df[C.TS_UTC].n_unique() == df.height
    assert df[C.TS_UTC].is_sorted()
    assert df[C.ROW_ID].to_list() == list(range(df.height))


def test_prices_are_coherent_and_strictly_positive() -> None:
    df = synth.minute_sessions("ESH26", WEEK, bars_per_session=200, seed=3)
    assert df.select(
        high_tops=(pl.col(C.HIGH) >= pl.max_horizontal(C.OPEN, C.CLOSE)).all(),
        low_bottoms=(pl.col(C.LOW) <= pl.min_horizontal(C.OPEN, C.CLOSE)).all(),
        ordered=(pl.col(C.HIGH) >= pl.col(C.LOW)).all(),
        positive=(pl.min_horizontal(*C.PRICES) > 0).all(),
        traded=(pl.col(C.VOLUME) > 0).all(),
    ).row(0) == (True, True, True, True, True)


def test_no_nulls_except_minute_open_interest() -> None:
    df = synth.minute_sessions("ESH26", [MONDAY], bars_per_session=30)
    nulls = df.null_count().row(0, named=True)
    assert nulls.pop(C.OPEN_INTEREST) == 30
    assert set(nulls.values()) == {0}
    assert synth.daily_series("ESH26", WEEK).null_count().sum_horizontal()[0] == 0


@pytest.mark.parametrize(
    ("session", "offset_hours"),
    [(date(2026, 2, 10), 6), (date(2026, 7, 14), 5)],  # CST, then CDT
)
def test_utc_is_derived_from_the_chicago_wall_clock_not_copied(
    session: date, offset_hours: int
) -> None:
    df = synth.minute_sessions("ESH26", [session], bars_per_session=1)
    local, utc = df[C.TS_LOCAL][0], df[C.TS_UTC][0]
    assert utc.replace(tzinfo=None) - local == timedelta(hours=offset_hours)


def test_the_seed_makes_the_path_reproducible() -> None:
    a = synth.minute_sessions("ESH26", [MONDAY], bars_per_session=20, seed=11)
    b = synth.minute_sessions("ESH26", [MONDAY], bars_per_session=20, seed=11)
    c = synth.minute_sessions("ESH26", [MONDAY], bars_per_session=20, seed=12)
    assert a.equals(b)
    assert not a[C.CLOSE].equals(c[C.CLOSE])


def test_calendar_roll_starts_at_midnight() -> None:
    df = synth.minute_sessions("SBH26", [MONDAY], bars_per_session=3, roll_hour=0, exchange="ICEUS")
    assert df[C.TS_LOCAL][0] == datetime(2026, 3, 9, 0, 0)
    assert df[C.SESSION_DATE].unique().to_list() == [MONDAY]


def test_the_builder_refuses_to_emit_a_nonexistent_local_time() -> None:
    """2026-03-08 02:00–02:59 CT does not exist; a calendar session would span it."""
    with pytest.raises(ValueError, match="non-existent local times"):
        synth.minute_sessions("ESH26", [date(2026, 3, 8)], bars_per_session=300, roll_hour=0)


def test_the_builder_refuses_to_emit_an_ambiguous_local_time() -> None:
    """2025-11-02 01:00–01:59 CT happens twice."""
    with pytest.raises(ValueError, match="ambiguous local times"):
        synth.minute_sessions("ESH26", [date(2025, 11, 2)], bars_per_session=120, roll_hour=0)


def test_no_dates_yields_an_empty_but_valid_frame() -> None:
    df = synth.minute_sessions("ESH26", [])
    assert df.height == 0
    assert df.schema == BAR_SCHEMA


def test_bars_per_session_must_be_positive() -> None:
    with pytest.raises(ValueError, match="bars_per_session"):
        synth.minute_sessions("ESH26", [MONDAY], bars_per_session=0)


def test_daily_ts_utc_is_the_session_date_label_at_midnight() -> None:
    df = synth.daily_series("ESH26", WEEK)
    assert [d.date() for d in df[C.TS_UTC].dt.replace_time_zone(None).to_list()] == list(WEEK)
    assert df[C.TS_UTC].dt.hour().unique().to_list() == [0]
    assert df[C.SESSION_DATE].to_list() == list(WEEK)
    assert df[C.TS_UTC].dtype.time_zone == "UTC"


@pytest.mark.parametrize(
    ("contract", "expected_root"),
    [("ESH26", "ES"), ("SR3H26", "SR3"), ("CLG26", "CL"), ("VXX25", "VX"), ("ZC", "ZC")],
)
def test_root_is_derived_from_the_contract_code(contract: str, expected_root: str) -> None:
    assert synth.root_of(contract) == expected_root


def test_session_start_matches_the_roll() -> None:
    assert synth.session_start(MONDAY, 17) == datetime(2026, 3, 8, 17, 0)
    assert synth.session_start(MONDAY, 0) == datetime(2026, 3, 9, 0, 0)


def test_as_bar_frame_tags_provenance() -> None:
    bars = synth.as_bar_frame(synth.daily_series("ESH26", WEEK), Frequency.DAILY, "unit")
    assert bars.frequency is Frequency.DAILY
    assert bars.source == "unit"
    assert bars.contracts() == ["ESH26"]
