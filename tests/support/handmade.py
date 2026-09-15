"""Hand-placed bars, for the cases a session builder cannot express.

`support.synth` builds whole, perfectly dense sessions — exactly right for "clean data
yields nothing surprising" properties, and useless for "what happens at 16:59 and
17:00", "what happens across a 40-minute hole" or "what happens to the two 01:30 bars
on the fall-back night". Those need bars placed one at a time.

Like `synth`, this goes through the real `mdq.time.localise` code path rather than
fabricating `ts_utc`, so a test can never accidentally assert against a timestamp the
production path would not have produced.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import polars as pl

from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, BarFrame, C
from mdq.time.localise import localise_wall_clock
from mdq.time.sessions import session_date_expr, session_profile_for

__all__ = ["Bar", "bar_frame"]


@dataclass(frozen=True)
class Bar:
    """One bar, positioned by its **local wall clock** just as a vendor file is.

    Every price defaults to a coherent 99/101 bar so a test need only state the thing
    it is actually about — usually the wall clock and the volume.
    """

    wall: datetime
    open: float | None = 100.0
    high: float | None = 101.0
    low: float | None = 99.0
    close: float | None = 100.0
    volume: int | None = 10
    contract: str = "ESH26"


_BUILD_SCHEMA = pl.Schema(
    [
        (C.ROW_ID, pl.UInt32),
        (C.CONTRACT, pl.String),
        (C.EXCHANGE, pl.String),
        (C.ROOT, pl.String),
        ("wall", pl.Datetime("us")),
        (C.OPEN, pl.Float64),
        (C.HIGH, pl.Float64),
        (C.LOW, pl.Float64),
        (C.CLOSE, pl.Float64),
        (C.VOLUME, pl.Int64),
        (C.OPEN_INTEREST, pl.Int64),
    ]
)


def bar_frame(
    bars: Iterable[Bar],
    *,
    exchange: str = "CME",
    frequency: Frequency = Frequency.MINUTE,
    source: str = "<handmade>",
) -> BarFrame:
    """Localise hand-placed bars and derive their `session_date`.

    Args:
        bars: The bars, in whatever order — analytics must not depend on input order.
        exchange: Chooses the session profile (CME rolls at 17:00 local).
        frequency: Tag for the resulting `BarFrame`.
        source: Provenance string.

    Returns:
        A `BarFrame` conforming to `BAR_SCHEMA`.

    Raises:
        ValueError: if any wall clock does not exist (inside the spring-forward gap).
    """
    rows = [
        {
            C.ROW_ID: index,
            C.CONTRACT: bar.contract,
            C.EXCHANGE: exchange,
            C.ROOT: bar.contract[:2],
            "wall": bar.wall,
            C.OPEN: bar.open,
            C.HIGH: bar.high,
            C.LOW: bar.low,
            C.CLOSE: bar.close,
            C.VOLUME: bar.volume,
            C.OPEN_INTEREST: None,
        }
        for index, bar in enumerate(bars)
    ]
    raw = pl.DataFrame(rows, schema=_BUILD_SCHEMA)
    kept, rejects = localise_wall_clock(raw.lazy(), "wall")
    dropped = rejects.collect()
    if dropped.height:
        raise ValueError(
            f"{dropped.height} hand-placed bar(s) have a wall clock that never happened "
            f"(first: {dropped['wall'][0]})"
        )
    frame = (
        kept.with_columns(session_date_expr(session_profile_for(exchange)))
        .select(BAR_SCHEMA.names())
        .collect()
        .cast(dict(BAR_SCHEMA))  # type: ignore[arg-type]
    )
    return BarFrame(frame.lazy(), frequency, source)
