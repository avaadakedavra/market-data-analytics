"""The session roll — the difference between a calendar day and a trading day."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, time

import polars as pl
import pytest

from mdq.domain.schema import C
from mdq.time.sessions import (
    CALENDAR_SESSION,
    DEFAULT_SESSIONS,
    SessionProfile,
    session_date_expr,
    session_profile_for,
)

pytestmark = pytest.mark.unit

CME = DEFAULT_SESSIONS["CME"]


def _session_dates(profile: SessionProfile, *walls: datetime) -> list[date]:
    frame = pl.DataFrame({C.TS_LOCAL: list(walls)}, schema={C.TS_LOCAL: pl.Datetime("us")})
    return frame.select(session_date_expr(profile))[C.SESSION_DATE].to_list()


def test_the_roll_splits_one_wall_date_into_two_sessions() -> None:
    """16:59 and 17:00 on the same wall date belong to *different* sessions."""
    before, after = datetime(2026, 3, 4, 16, 59), datetime(2026, 3, 4, 17, 0)
    assert _session_dates(CME, before, after) == [date(2026, 3, 4), date(2026, 3, 5)]


def test_a_whole_cme_session_carries_one_session_date() -> None:
    walls = [
        datetime(2026, 3, 3, 17, 0),  # Tuesday 17:00 — the session opens
        datetime(2026, 3, 3, 23, 59),
        datetime(2026, 3, 4, 0, 0),  # midnight does not end the session
        datetime(2026, 3, 4, 15, 59),  # the last bar before the break
    ]
    assert _session_dates(CME, *walls) == [date(2026, 3, 4)] * 4


def test_sunday_evening_belongs_to_the_monday_session() -> None:
    assert _session_dates(CME, datetime(2026, 3, 8, 17, 0)) == [date(2026, 3, 9)]


def test_calendar_profile_never_rolls() -> None:
    walls = [
        datetime(2026, 3, 4, 16, 59),
        datetime(2026, 3, 4, 17, 0),
        datetime(2026, 3, 4, 23, 59),
    ]
    assert _session_dates(CALENDAR_SESSION, *walls) == [date(2026, 3, 4)] * 3


def test_session_date_expr_reads_ts_local_by_default_and_honours_an_override() -> None:
    frame = pl.DataFrame(
        {"other": [datetime(2026, 3, 4, 18, 0)]},
        schema={"other": pl.Datetime("us")},
    )
    assert frame.select(session_date_expr(CME, "other"))[C.SESSION_DATE][0] == date(2026, 3, 5)


def test_cme_family_shares_one_verified_profile() -> None:
    for exchange in ("CME", "CBOT", "COMEX", "NYMEX", "CFE"):
        profile = DEFAULT_SESSIONS[exchange]
        assert profile.roll_hour_local == 17
        assert profile.break_local == (time(16, 0), time(17, 0))
        assert profile.expected_bars == 1380  # 23 hours of minute bars
        assert profile.rolls


def test_iceus_uses_calendar_dates() -> None:
    profile = DEFAULT_SESSIONS["ICEUS"]
    assert profile.roll_hour_local == 0
    assert profile.break_local is None
    assert not profile.rolls


@pytest.mark.parametrize("exchange", ["CME", "cme", " Cme "])
def test_lookup_is_case_and_whitespace_insensitive(exchange: str) -> None:
    assert session_profile_for(exchange) is CME


def test_unknown_or_absent_exchange_falls_back_to_the_calendar() -> None:
    assert session_profile_for("EUREX") is CALENDAR_SESSION
    assert session_profile_for(None) is CALENDAR_SESSION


def test_lookup_accepts_an_override_table() -> None:
    custom = {"EUREX": SessionProfile(roll_hour_local=22, break_local=None, expected_bars=None)}
    assert session_profile_for("EUREX", custom).roll_hour_local == 22
    assert session_profile_for("CME", custom) is CALENDAR_SESSION


@pytest.mark.parametrize("roll_hour", [-1, 24])
def test_an_impossible_roll_hour_is_refused(roll_hour: int) -> None:
    with pytest.raises(ValueError, match=r"roll_hour_local must be in 0\.\.23"):
        SessionProfile(roll_hour_local=roll_hour, break_local=None, expected_bars=None)


def test_profiles_are_frozen_and_hashable() -> None:
    assert len({CME, CALENDAR_SESSION}) == 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        CME.roll_hour_local = 18  # type: ignore[misc]
