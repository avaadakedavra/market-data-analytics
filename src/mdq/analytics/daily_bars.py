"""Session aggregation: minute bars → one bar per trading session.

The whole point of this module is that it does **not** group by calendar date. It
groups by `session_date`, which the time layer derived with a 17:00 CT roll — verified
in PLAN §0.1 to reproduce the vendor's daily `open` on 193/197 days against 36/197 for
calendar-date aggregation.

`open` and `close` are the *first* and *last* bar by `ts_utc` (ties broken by `row_id`,
which matters on the DST fall-back hour where two bars share an instant), not the
minimum and maximum, so a session whose price fell still reports the open it opened at.

On DAILY input the grouping is a no-op — one row per `(contract, session_date)` — so
the output is the identity with `bar_count == 1`. That is deliberate: one endpoint
serves both frequencies and nothing downstream branches on frequency.

Caveats worth stating, because a reviewer will ask:

* `close` here is the **last traded price**, not a settlement price. The vendor's daily
  `close` is a settlement and agrees with this only ~1% of the time (PLAN §0).
* Minute coverage is sparse in thin sessions (corpus median 222 bars against 1,380
  possible), so a session's true high or low may simply be absent from the minute
  file. `bar_count` is emitted precisely so that sparsity is visible rather than
  silently baked into the numbers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Final

import polars as pl
from pydantic import BaseModel, ConfigDict

from mdq.analytics.registry import coerce_params, register_analytic
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame, C

__all__ = [
    "BAR_COUNT",
    "DAILY_BARS_SCHEMA",
    "FIRST_TS",
    "LAST_TS",
    "NULL_PRICE_BARS",
    "DailyBars",
    "DailyBarsParams",
    "daily_bars",
]

#: Number of source bars that fell into the session.
BAR_COUNT: Final = "bar_count"
#: First and last instant actually observed in the session.
FIRST_TS: Final = "first_ts"
LAST_TS: Final = "last_ts"
#: Source bars with a null in any of open/high/low/close.
NULL_PRICE_BARS: Final = "null_price_bars"

#: The shape of the output. Order is part of the contract, as with `BAR_SCHEMA`.
DAILY_BARS_SCHEMA: Final[pl.Schema] = pl.Schema(
    [
        (C.CONTRACT, pl.String),
        (C.SESSION_DATE, pl.Date),
        (C.OPEN, pl.Float64),
        (C.HIGH, pl.Float64),
        (C.LOW, pl.Float64),
        (C.CLOSE, pl.Float64),
        (C.VOLUME, pl.Int64),
        (BAR_COUNT, pl.UInt32),
        (FIRST_TS, pl.Datetime("us", "UTC")),
        (LAST_TS, pl.Datetime("us", "UTC")),
        (NULL_PRICE_BARS, pl.UInt32),
    ]
)

#: Time order within a session. `row_id` breaks ties, which is what makes the two
#: fall-back-hour bars that share a `ts_utc` aggregate deterministically.
_TIME_ORDER: Final = (C.TS_UTC, C.ROW_ID)


def _session_volume() -> pl.Expr:
    """Summed volume, but null when *every* bar's volume was null.

    Polars sums an all-null group to 0, which would claim a session traded nothing
    when in truth we simply do not know. Analytics must not invent observations.
    """
    return (
        pl.when(pl.col(C.VOLUME).is_null().all())
        .then(pl.lit(None, pl.Int64))
        .otherwise(pl.col(C.VOLUME).sum())
        .alias(C.VOLUME)
    )


def _null_price_bars() -> pl.Expr:
    """How many bars in the session had a null in any price column."""
    return (
        pl.any_horizontal(*(pl.col(column).is_null() for column in C.PRICES))
        .sum()
        .cast(pl.UInt32)
        .alias(NULL_PRICE_BARS)
    )


def daily_bars(bars: BarFrame) -> pl.DataFrame:
    """Aggregate `bars` to one row per `(contract, session_date)`.

    Args:
        bars: Any `BarFrame`; minute bars are aggregated, daily bars pass through.

    Returns:
        A frame conforming to `DAILY_BARS_SCHEMA`, sorted by contract then session
        date. An empty input yields an empty frame with that schema, never an error.
    """
    aggregated = (
        bars.lf.group_by(C.CONTRACT, C.SESSION_DATE)
        .agg(
            pl.col(C.OPEN).sort_by(*_TIME_ORDER).first().alias(C.OPEN),
            pl.col(C.HIGH).max().alias(C.HIGH),
            pl.col(C.LOW).min().alias(C.LOW),
            pl.col(C.CLOSE).sort_by(*_TIME_ORDER).last().alias(C.CLOSE),
            _session_volume(),
            pl.len().cast(pl.UInt32).alias(BAR_COUNT),
            pl.col(C.TS_UTC).min().alias(FIRST_TS),
            pl.col(C.TS_UTC).max().alias(LAST_TS),
            _null_price_bars(),
        )
        .sort(C.CONTRACT, C.SESSION_DATE)
        .select(DAILY_BARS_SCHEMA.names())
    )
    return aggregated.collect().cast(dict(DAILY_BARS_SCHEMA))  # type: ignore[arg-type]


class DailyBarsParams(BaseModel):
    """No knobs: the session roll is a property of the exchange, not a user choice.

    The model exists anyway so the registry's contract holds and the API can generate
    a route (with no query parameters) without special-casing this analytic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


@register_analytic
class DailyBars:
    """Session OHLCV, with the coverage metadata needed to judge how much to trust it."""

    name: ClassVar[str] = "daily_bars"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset({Frequency.MINUTE, Frequency.DAILY})
    Params: ClassVar[type[BaseModel]] = DailyBarsParams

    def run(
        self,
        bars: BarFrame,
        params: BaseModel | Mapping[str, Any] | None = None,
    ) -> pl.DataFrame:
        """Run the analytic; `params` is validated for symmetry with the others."""
        coerce_params(DailyBarsParams, params)
        return daily_bars(bars)
