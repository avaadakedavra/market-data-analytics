"""Synthetic bar builder — the precision guard for every check.

Everything this module emits is **perfectly clean**: dense, ordered, uniquely
timestamped, strictly positive prices, coherent OHLC, positive volume, no duplicates,
no nulls (bar `open_interest` on minute bars, which the schema declares null), and no
DST anomalies. That is the point: the standing property is *clean synthetic data
yields zero findings*, so any finding a check raises on this data is a false positive.

The builder is paranoid about its own output — it localises through the real
`mdq.time.localise` code path and raises if any bar turns out to be non-existent or
ambiguous, so a test can never silently be built on dirty "clean" data.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

import polars as pl

from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, BarFrame, C
from mdq.time.localise import localise_wall_clock
from mdq.time.sessions import DEFAULT_SESSIONS, SessionProfile, session_date_expr

__all__ = [
    "as_bar_frame",
    "daily_series",
    "minute_sessions",
    "root_of",
    "session_start",
]

#: CME month codes, used to split a contract code into root + expiry.
_EXPIRY_RE = re.compile(r"[FGHJKMNQUVXZ]\d{1,2}$")

DEFAULT_EXCHANGE = "CME"
BARS_PER_CME_SESSION = 1380


def root_of(contract: str) -> str:
    """`"ESH26" -> "ES"`, `"SR3H26" -> "SR3"` — strip the month code and year."""
    return _EXPIRY_RE.sub("", contract.upper()) or contract.upper()


def session_start(session: date, roll_hour: int) -> datetime:
    """The local wall clock at which the session labelled `session` opens.

    With a 17:00 roll the Monday session opens at 17:00 on Sunday; with roll hour 0
    (calendar sessions) it opens at midnight on the day itself.
    """
    if roll_hour == 0:
        return datetime.combine(session, datetime.min.time())
    return datetime.combine(session - timedelta(days=1), datetime.min.time()) + timedelta(
        hours=roll_hour
    )


def _walk(rng: random.Random, price: float, tick: float) -> tuple[float, float, float, float]:
    """One OHLC bar from a random walk, guaranteed coherent and strictly positive."""
    open_ = price
    close = max(tick, round(open_ * (1.0 + rng.uniform(-0.0008, 0.0008)), 2))
    high = round(max(open_, close) + abs(rng.gauss(0, 0.4)), 2)
    low = round(min(open_, close) - abs(rng.gauss(0, 0.4)), 2)
    # Rounding can pull high/low back inside the body; re-assert the invariant.
    high = max(high, open_, close)
    low = max(tick, min(low, open_, close))
    return open_, high, low, close


def _finalise(rows: list[dict[str, object]], profile: SessionProfile) -> pl.DataFrame:
    """Localise, derive `session_date`, and assert the result really is clean."""
    raw = pl.DataFrame(
        rows,
        schema={
            C.ROW_ID: pl.UInt32,
            C.CONTRACT: pl.String,
            C.EXCHANGE: pl.String,
            C.ROOT: pl.String,
            "wall": pl.Datetime("us"),
            C.OPEN: pl.Float64,
            C.HIGH: pl.Float64,
            C.LOW: pl.Float64,
            C.CLOSE: pl.Float64,
            C.VOLUME: pl.Int64,
            C.OPEN_INTEREST: pl.Int64,
        },
    )
    kept, rejects = localise_wall_clock(raw.lazy(), "wall")
    bad = rejects.collect()
    if bad.height:
        raise ValueError(
            f"synthetic data would contain {bad.height} non-existent local times "
            f"(first: {bad['wall'][0]}); pick dates away from the spring-forward gap"
        )
    frame = kept.with_columns(session_date_expr(profile)).collect()
    if bool(frame[C.IS_AMBIGUOUS].any()):
        raise ValueError(
            "synthetic data would contain ambiguous local times; pick dates away "
            "from the fall-back hour"
        )
    return frame.select(BAR_SCHEMA.names()).cast(dict(BAR_SCHEMA))  # type: ignore[arg-type]


def minute_sessions(
    contract: str = "ESH26",
    dates: Sequence[date] | Iterable[date] = (),
    bars_per_session: int = BARS_PER_CME_SESSION,
    roll_hour: int = 17,
    seed: int = 0,
    exchange: str = DEFAULT_EXCHANGE,
    start_price: float = 5000.0,
) -> pl.DataFrame:
    """Dense one-minute bars for whole trading sessions, in `BAR_SCHEMA`.

    Args:
        contract: Contract code; `root` is derived from it.
        dates: Session dates (the date the session *settles* on, i.e. `session_date`).
        bars_per_session: Consecutive minutes from the session open. 1380 is a full
            CME 23-hour session (17:00 → 15:59 the next day).
        roll_hour: Local hour at which the session rolls; 0 for calendar sessions.
        seed: Makes the price path reproducible.
        exchange: Exchange code written into the frame.
        start_price: First open.

    Raises:
        ValueError: if the requested dates would produce a non-existent or ambiguous
            local time — i.e. if the "clean" data would not actually be clean.
    """
    sessions = list(dates)
    if bars_per_session < 1:
        raise ValueError(f"bars_per_session must be >= 1, got {bars_per_session}")
    rng = random.Random(seed)
    profile = _profile_for(roll_hour)
    rows: list[dict[str, object]] = []
    price = start_price
    for session in sessions:
        opened = session_start(session, roll_hour)
        for minute in range(bars_per_session):
            open_, high, low, close = _walk(rng, price, tick=0.25)
            rows.append(
                {
                    C.ROW_ID: len(rows),
                    C.CONTRACT: contract,
                    C.EXCHANGE: exchange,
                    C.ROOT: root_of(contract),
                    "wall": opened + timedelta(minutes=minute),
                    C.OPEN: open_,
                    C.HIGH: high,
                    C.LOW: low,
                    C.CLOSE: close,
                    C.VOLUME: rng.randint(1, 5_000),
                    C.OPEN_INTEREST: None,
                }
            )
            price = close
    return _finalise(rows, profile)


def daily_series(
    contract: str = "ESH26",
    dates: Sequence[date] | Iterable[date] = (),
    seed: int = 0,
    exchange: str = DEFAULT_EXCHANGE,
    start_price: float = 5000.0,
) -> pl.DataFrame:
    """One clean daily bar per date, in `BAR_SCHEMA`.

    Daily `ts_utc` is the session date at 00:00 UTC — a *label*, not a trading
    instant — and `session_date` equals the date, per PLAN §2.1.
    """
    rng = random.Random(seed)
    rows: list[dict[str, object]] = []
    price = start_price
    for day in dates:
        open_, high, low, close = _walk(rng, price, tick=0.25)
        rows.append(
            {
                C.ROW_ID: len(rows),
                C.CONTRACT: contract,
                C.EXCHANGE: exchange,
                C.ROOT: root_of(contract),
                C.TS_UTC: datetime.combine(day, datetime.min.time()),
                C.TS_LOCAL: datetime.combine(day, datetime.min.time()),
                C.SESSION_DATE: day,
                C.OPEN: open_,
                C.HIGH: high,
                C.LOW: low,
                C.CLOSE: close,
                C.VOLUME: rng.randint(1_000, 500_000),
                C.OPEN_INTEREST: rng.randint(1_000, 2_000_000),
            }
        )
        price = close
    return (
        pl.DataFrame(rows, schema=_DAILY_BUILD_SCHEMA)
        .with_columns(pl.col(C.TS_UTC).dt.replace_time_zone("UTC"))
        .select(BAR_SCHEMA.names())
        .cast(dict(BAR_SCHEMA))  # type: ignore[arg-type]
    )


def as_bar_frame(
    frame: pl.DataFrame,
    frequency: Frequency,
    source: str = "<synthetic>",
) -> BarFrame:
    """Wrap a synthetic frame in the domain carrier."""
    return BarFrame(frame.lazy(), frequency, source)


_DAILY_BUILD_SCHEMA = pl.Schema(
    [
        (C.ROW_ID, pl.UInt32),
        (C.CONTRACT, pl.String),
        (C.EXCHANGE, pl.String),
        (C.ROOT, pl.String),
        (C.TS_UTC, pl.Datetime("us")),
        (C.TS_LOCAL, pl.Datetime("us")),
        (C.SESSION_DATE, pl.Date),
        (C.OPEN, pl.Float64),
        (C.HIGH, pl.Float64),
        (C.LOW, pl.Float64),
        (C.CLOSE, pl.Float64),
        (C.VOLUME, pl.Int64),
        (C.OPEN_INTEREST, pl.Int64),
    ]
)


def _profile_for(roll_hour: int) -> SessionProfile:
    """A session profile matching `roll_hour`, reusing the CME table where it fits."""
    cme = DEFAULT_SESSIONS["CME"]
    if roll_hour == cme.roll_hour_local:
        return cme
    return SessionProfile(roll_hour_local=roll_hour, break_local=None, expected_bars=None)
