"""Frequency carries the bar period; nothing else branches on frequency."""

from __future__ import annotations

from datetime import timedelta

import pytest

from mdq.domain.frequency import Frequency

pytestmark = pytest.mark.unit


def test_minute_bars_are_one_minute_apart() -> None:
    assert Frequency.MINUTE.bar_period == timedelta(minutes=1)


def test_daily_bars_have_no_fixed_period() -> None:
    # Weekends and holidays mean a daily series has no constant spacing; callers must
    # fall back to sessions rather than multiplying by a period.
    assert Frequency.DAILY.bar_period is None


def test_values_are_stable_wire_strings() -> None:
    assert [f.value for f in Frequency] == ["daily", "minute"]
    assert str(Frequency.MINUTE) == "minute"


@pytest.mark.parametrize("raw", ["minute", "MINUTE", " Minute ", Frequency.MINUTE])
def test_parse_is_case_insensitive(raw: str | Frequency) -> None:
    assert Frequency.parse(raw) is Frequency.MINUTE


def test_parse_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown frequency 'hourly'"):
        Frequency.parse("hourly")
