"""Rolling VWAP — time-based, gap-aware, and honest about thin windows.

Three decisions carry this module.

**The window is a duration, not a bar count.** A 15-minute VWAP over minute bars is
*not* "the last 15 rows": in a thin session the previous 15 rows can span two hours.
`LazyFrame.rolling` with `period=` and `index_column="ts_utc"` measures real elapsed
time, so a bar that follows a 40-minute hole correctly sees only itself.

**The window is bounded by the session.** Grouping by `(contract, session_date)` is
what stops a window spanning the 16:00–17:00 CT maintenance break, or reaching from
Sunday's 17:00 open back into Friday's 15:59 close. No calendar, no holiday table —
the session boundary does the work, and it is already in the frame.

**A window with no volume has no VWAP.** `vwap` is null there, never 0 and never
"fall back to the close". A settlement-only bar with zero volume genuinely has no
volume-weighted price, and silently substituting one would corrupt every chart and
every downstream comparison it touches.

`bars_in_window` is emitted for the same reason: a VWAP computed from one bar is a
price, not an average, and the consumer is entitled to see that.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar, Final

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mdq.analytics.registry import coerce_params, register_analytic
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame, C

__all__ = [
    "BARS_IN_WINDOW",
    "VWAP",
    "VWAP_SCHEMA",
    "WINDOW_START_UTC",
    "WINDOW_VOLUME",
    "PriceSource",
    "RollingVwap",
    "VwapParams",
    "rolling_vwap",
]

#: The volume-weighted average price over the window, or null when there is no volume.
VWAP: Final = "vwap"
#: Volume that actually contributed to `vwap` (bars with both a price and a volume).
WINDOW_VOLUME: Final = "window_volume"
#: Every bar in the window, including ones that contributed nothing.
BARS_IN_WINDOW: Final = "bars_in_window"
#: The instant of the *earliest bar present* in the window — not the nominal
#: `ts_utc - period` boundary. After a gap this equals `ts_utc`, which is how a
#: consumer sees that a "15-minute" VWAP actually covers 30 seconds of data.
WINDOW_START_UTC: Final = "window_start_utc"

#: The shape of the output. Order is part of the contract.
VWAP_SCHEMA: Final[pl.Schema] = pl.Schema(
    [
        (C.CONTRACT, pl.String),
        (C.SESSION_DATE, pl.Date),
        (C.TS_UTC, pl.Datetime("us", "UTC")),
        (VWAP, pl.Float64),
        (WINDOW_VOLUME, pl.Int64),
        (BARS_IN_WINDOW, pl.UInt32),
        (WINDOW_START_UTC, pl.Datetime("us", "UTC")),
    ]
)

_PRODUCT: Final = "_price_volume"

#: Polars duration literals we accept, e.g. `15m`, `1h30m`, `500ms`.
_DURATION_UNIT = re.compile(r"(\d+)(ns|us|ms|s|m|h|d|w)")
_DURATION = re.compile(r"^(?:\d+(?:ns|us|ms|s|m|h|d|w))+$")


class PriceSource(StrEnum):
    """Which price each bar contributes to the average.

    `TYPICAL` — `(high + low + close) / 3` — is the default because it uses the whole
    bar rather than only its final print, which matters when bars are sparse.
    """

    TYPICAL = "typical"
    CLOSE = "close"


def _price_expr(source: PriceSource) -> pl.Expr:
    """The per-bar price for `source`."""
    if source is PriceSource.CLOSE:
        return pl.col(C.CLOSE)
    return (pl.col(C.HIGH) + pl.col(C.LOW) + pl.col(C.CLOSE)) / 3.0


class VwapParams(BaseModel):
    """Knobs for `rolling_vwap`, exposed by the API as query parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Polars duration string measured on `ts_utc`, e.g. `"15m"`, `"1h"`, `"1h30m"`.
    window: str = "15m"
    #: Which price each bar contributes.
    price: PriceSource = PriceSource.TYPICAL
    #: Volume a window must reach before a VWAP is reported; 0 still requires > 0.
    min_volume: int = Field(default=0, ge=0)

    @field_validator("window")
    @classmethod
    def _window_must_be_a_positive_duration(cls, value: str) -> str:
        text = value.strip()
        if not _DURATION.match(text):
            raise ValueError(
                f"window {value!r} is not a polars duration string "
                "(e.g. '15m', '1h', '1h30m', '500ms')"
            )
        if not any(int(amount) > 0 for amount, _ in _DURATION_UNIT.findall(text)):
            raise ValueError(f"window {value!r} must be a positive duration")
        return text


def rolling_vwap(
    bars: BarFrame,
    params: VwapParams | Mapping[str, Any] | None = None,
) -> pl.DataFrame:
    """A per-bar rolling VWAP that never spans a session boundary or a gap.

    Args:
        bars: Any `BarFrame`. Rows are sorted internally, so an unsorted frame is fine.
        params: A `VwapParams`, a mapping of its fields, or None for the defaults.

    Returns:
        One row per input bar, conforming to `VWAP_SCHEMA`, ordered by contract,
        session date and instant. Empty input yields an empty frame with that schema.

    Raises:
        pydantic.ValidationError: if `params` is invalid (422 at the API).
    """
    settings = coerce_params(VwapParams, params)
    price = _price_expr(settings.price)
    # Only bars carrying *both* a price and a volume can contribute; including a bar
    # with a null price in the denominator would bias the average toward zero.
    contributes = price.is_not_null() & pl.col(C.VOLUME).is_not_null()

    rolled = (
        bars.lf.sort(C.CONTRACT, C.SESSION_DATE, C.TS_UTC, C.ROW_ID)
        .rolling(
            index_column=C.TS_UTC,
            period=settings.window,
            group_by=[C.CONTRACT, C.SESSION_DATE],
            closed="right",
        )
        .agg(
            (price * pl.col(C.VOLUME)).filter(contributes).sum().alias(_PRODUCT),
            pl.col(C.VOLUME).filter(contributes).sum().alias(WINDOW_VOLUME),
            pl.len().cast(pl.UInt32).alias(BARS_IN_WINDOW),
            pl.col(C.TS_UTC).min().alias(WINDOW_START_UTC),
        )
        .with_columns(_vwap_expr(settings.min_volume))
        .select(VWAP_SCHEMA.names())
    )
    return rolled.collect().cast(dict(VWAP_SCHEMA))  # type: ignore[arg-type]


def _vwap_expr(min_volume: int) -> pl.Expr:
    """`sum(price * volume) / sum(volume)`, or null when the window cannot support it."""
    volume = pl.col(WINDOW_VOLUME)
    usable = (volume > 0) & (volume >= min_volume)
    return (
        pl.when(usable)
        .then(pl.col(_PRODUCT) / volume)
        .otherwise(pl.lit(None, pl.Float64))
        .alias(VWAP)
    )


@register_analytic
class RollingVwap:
    """Time-based rolling VWAP, bounded by the trading session."""

    name: ClassVar[str] = "rolling_vwap"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset({Frequency.MINUTE})
    Params: ClassVar[type[BaseModel]] = VwapParams

    def run(
        self,
        bars: BarFrame,
        params: BaseModel | Mapping[str, Any] | None = None,
    ) -> pl.DataFrame:
        """Run the analytic against `bars`."""
        return rolling_vwap(bars, coerce_params(VwapParams, params))
