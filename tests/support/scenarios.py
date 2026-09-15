"""Named scenarios the quality tests share.

Each builder returns a frame with one property the tests care about (dense and active,
sparse and dormant, two contracts, a whole CME week including the DST weekend), so the
tests read as statements about behaviour rather than as data setup.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from mdq.domain.config import QualityConfig
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame
from mdq.quality import CheckContext, QualityReport, run_checks
from support import synth
from support.faults import merge_contracts

__all__ = [
    "CALENDAR_DAY",
    "DST_WEEK",
    "SESSION_DATES",
    "calendar_day_minutes",
    "checked",
    "daily_frame",
    "full_sessions",
    "minute_frame",
    "two_contract_frame",
]

#: Three consecutive DST-free CME sessions (Tue–Thu).
SESSION_DATES: tuple[date, ...] = (date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5))

#: A calendar-session day, so the 16:00–17:00 CT break falls *inside* one session
#: instead of at its edge. That is the only way to exercise break exclusion directly.
CALENDAR_DAY: tuple[date, ...] = (date(2026, 3, 3), date(2026, 3, 4))

#: A Friday session and the Monday session that follows it across the 2026-03-08
#: spring-forward weekend.
DST_WEEK: tuple[date, ...] = (date(2026, 3, 6), date(2026, 3, 9))


def minute_frame(bars_per_session: int = 120, seed: int = 7) -> pl.DataFrame:
    """Three dense, perfectly clean CME minute sessions for ESH26."""
    return synth.minute_sessions(
        "ESH26", SESSION_DATES, bars_per_session=bars_per_session, seed=seed
    )


def daily_frame(seed: int = 7) -> pl.DataFrame:
    """Three perfectly clean daily bars for ESH26."""
    return synth.daily_series("ESH26", SESSION_DATES, seed=seed)


def two_contract_frame(bars_per_session: int = 60) -> pl.DataFrame:
    """Two CME contracts over four weekday sessions — enough for sibling corroboration."""
    dates = (*SESSION_DATES, date(2026, 3, 6))
    return merge_contracts(
        synth.minute_sessions("ESH26", dates, bars_per_session=bars_per_session, seed=1),
        synth.minute_sessions("ESM26", dates, bars_per_session=bars_per_session, seed=2),
    )


def calendar_day_minutes() -> pl.DataFrame:
    """Whole calendar days of minute bars, CME exchange, 00:00–23:59 sessions."""
    return synth.minute_sessions("ESH26", CALENDAR_DAY, bars_per_session=1440, roll_hour=0, seed=3)


def full_sessions() -> pl.DataFrame:
    """Two complete 1,380-bar CME sessions either side of a weekend closure."""
    return synth.minute_sessions("ESH26", DST_WEEK, bars_per_session=1380, seed=5)


def checked(
    frame: pl.DataFrame,
    frequency: Frequency = Frequency.MINUTE,
    *,
    rejects: pl.DataFrame | None = None,
    config: QualityConfig | None = None,
) -> tuple[BarFrame, CheckContext, QualityReport]:
    """Wrap a frame, build its context and run every applicable check."""
    bars = synth.as_bar_frame(frame, frequency)
    ctx = CheckContext.build(bars, config, rejects=rejects)
    return bars, ctx, run_checks(bars, ctx)
