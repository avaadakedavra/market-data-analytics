"""Trading sessions: which *session* a bar belongs to.

A CME trading day does not line up with a calendar day. The session that settles on
Monday opens at 17:00 CT on Sunday, so bars must be rolled forward: everything at or
after the roll hour belongs to the *next* session date.

This is verified, not assumed. Aggregating ESH26 minute bars into sessions and joining
to the vendor's own daily file (197 overlapping days) reproduces the vendor `open` on
193/197 days with a 17:00 roll, versus 36/197 using the calendar date.

Only the roll hour, the maintenance break and an informational bar count live here.
*Coverage expectations* are never hardcoded — they are observed per contract from the
data — which is what lets gap detection survive 23-hour sessions, listing and expiry
boundaries, and holidays without shipping a holiday calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

import polars as pl

from mdq.domain.schema import C

__all__ = [
    "CALENDAR_SESSION",
    "DEFAULT_SESSIONS",
    "SessionProfile",
    "session_date_expr",
    "session_profile_for",
]


@dataclass(frozen=True)
class SessionProfile:
    """How one exchange's trading day is shaped.

    Attributes:
        roll_hour_local: Bars at or after this local hour belong to the **next**
            `session_date`. ``0`` means "session date == calendar date".
        break_local: Daily maintenance window `[start, end)` in local time, excluded
            from gap detection. `None` when the venue has no intraday break.
        expected_bars: Informational baseline only (1380 for a CME 23-hour session).
            Never used as a threshold — regimes are observed from the data.
    """

    roll_hour_local: int
    break_local: tuple[time, time] | None
    expected_bars: int | None

    def __post_init__(self) -> None:
        if not 0 <= self.roll_hour_local <= 23:
            raise ValueError(f"roll_hour_local must be in 0..23, got {self.roll_hour_local}")

    @property
    def rolls(self) -> bool:
        """True when the session date differs from the calendar date after the roll."""
        return self.roll_hour_local != 0


#: CME-family venues: 23-hour session, 17:00 CT roll, 16:00–17:00 CT maintenance break.
_CME_FAMILY = SessionProfile(
    roll_hour_local=17,
    break_local=(time(16, 0), time(17, 0)),
    expected_bars=1380,
)

#: Fallback for venues we have not verified: the session date is the calendar date.
CALENDAR_SESSION = SessionProfile(roll_hour_local=0, break_local=None, expected_bars=None)

#: Per-exchange session table. Verified for the CME family; ICEUS uses calendar dates.
DEFAULT_SESSIONS: dict[str, SessionProfile] = {
    "CME": _CME_FAMILY,
    "CBOT": _CME_FAMILY,
    "COMEX": _CME_FAMILY,
    "NYMEX": _CME_FAMILY,
    "CFE": _CME_FAMILY,
    "ICEUS": CALENDAR_SESSION,
}


def session_profile_for(
    exchange: str | None,
    sessions: dict[str, SessionProfile] | None = None,
) -> SessionProfile:
    """The profile for `exchange`, falling back to calendar dates when unknown."""
    table = DEFAULT_SESSIONS if sessions is None else sessions
    if exchange is None:
        return CALENDAR_SESSION
    return table.get(exchange.strip().upper(), CALENDAR_SESSION)


def session_date_expr(profile: SessionProfile, col: str = C.TS_LOCAL) -> pl.Expr:
    """The `session_date` a bar belongs to, from its **local** wall clock.

    Uses `ts_local` rather than `ts_utc` deliberately: the roll is defined in local
    time, so it must stay put across DST rather than drifting by an hour.
    """
    local = pl.col(col)
    if not profile.rolls:
        return local.dt.date().alias(C.SESSION_DATE)
    return (
        pl.when(local.dt.hour() >= profile.roll_hour_local)
        .then(local.dt.date().dt.offset_by("1d"))
        .otherwise(local.dt.date())
        .alias(C.SESSION_DATE)
    )
