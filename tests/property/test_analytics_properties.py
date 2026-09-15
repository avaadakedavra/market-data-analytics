"""Properties that must hold for *any* bars, not just the ones we thought to write.

Two of these are the load-bearing ones:

* a VWAP is an average, so it can never escape the high/low envelope of the bars it
  averaged — if it does, the weighting is wrong somewhere;
* the gap-aware, time-based window must agree exactly with the naive "last N rows"
  calculation **when there are no gaps**, which is the only situation in which the
  naive calculation is correct. That pins the sophistication to the cases that need
  it rather than letting it quietly change every number.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mdq.analytics.daily_bars import BAR_COUNT, daily_bars
from mdq.analytics.vwap import (
    BARS_IN_WINDOW,
    VWAP,
    WINDOW_VOLUME,
    PriceSource,
    rolling_vwap,
)
from mdq.domain.schema import C
from support.handmade import Bar, bar_frame

pytestmark = pytest.mark.property

#: 17:00 CT opens a CME session, so every generated bar lands in one session.
OPEN_WALL = datetime(2026, 3, 3, 17, 0)

#: (low, range, close position within the range, volume) — always a coherent bar.
_BAR = st.tuples(
    st.floats(min_value=1.0, max_value=1_000.0, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    st.integers(min_value=0, max_value=10_000),
)
_BARS = st.lists(_BAR, min_size=1, max_size=40)
_WINDOW_MINUTES = st.integers(min_value=1, max_value=20)


def _dense(entries: list[tuple[float, float, float, int]]) -> list[Bar]:
    """Consecutive one-minute bars — no gaps, so the naive calculation is valid."""
    bars = []
    for index, (low, span, position, volume) in enumerate(entries):
        high = low + span
        close = low + span * position
        bars.append(
            Bar(
                OPEN_WALL + timedelta(minutes=index),
                open=close,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    return bars


def _tolerance(value: float) -> float:
    """Relative tolerance; VWAP sums in a different order than the reference does."""
    return 1e-9 * max(1.0, abs(value))


@settings(max_examples=50, deadline=None)
@given(entries=_BARS, minutes=_WINDOW_MINUTES, price=st.sampled_from(list(PriceSource)))
def test_a_vwap_never_escapes_the_window_it_averaged(
    entries: list[tuple[float, float, float, int]],
    minutes: int,
    price: PriceSource,
) -> None:
    """`min(low) <= vwap <= max(high)` over the same window, whenever there is volume."""
    bars = bar_frame(_dense(entries))
    window = f"{minutes}m"
    result = rolling_vwap(bars, {"window": window, "price": price})

    envelope = (
        bars.lf.sort(C.CONTRACT, C.SESSION_DATE, C.TS_UTC, C.ROW_ID)
        .rolling(
            index_column=C.TS_UTC,
            period=window,
            group_by=[C.CONTRACT, C.SESSION_DATE],
            closed="right",
        )
        .agg(pl.col(C.LOW).min().alias("floor"), pl.col(C.HIGH).max().alias("ceiling"))
        .collect()
    )

    for row in result.hstack(envelope.select("floor", "ceiling")).iter_rows(named=True):
        if row[WINDOW_VOLUME] == 0:
            assert row[VWAP] is None
            continue
        assert row[VWAP] is not None
        assert row[VWAP] >= row["floor"] - _tolerance(row["floor"])
        assert row[VWAP] <= row["ceiling"] + _tolerance(row["ceiling"])


@settings(max_examples=50, deadline=None)
@given(entries=_BARS, minutes=_WINDOW_MINUTES)
def test_on_dense_data_the_gap_aware_window_equals_a_naive_row_window(
    entries: list[tuple[float, float, float, int]],
    minutes: int,
) -> None:
    """With one bar every minute, "last N minutes" and "last N rows" must coincide."""
    bars = bar_frame(_dense(entries))
    result = rolling_vwap(bars, {"window": f"{minutes}m"})

    typical = (pl.col(C.HIGH) + pl.col(C.LOW) + pl.col(C.CLOSE)) / 3.0
    naive = (
        bars.lf.sort(C.TS_UTC, C.ROW_ID)
        .select(
            (typical * pl.col(C.VOLUME))
            .rolling_sum(window_size=minutes, min_samples=1)
            .alias("product"),
            pl.col(C.VOLUME).rolling_sum(window_size=minutes, min_samples=1).alias("volume"),
            pl.int_range(pl.len()).alias("index"),
        )
        .with_columns(
            pl.min_horizontal(pl.col("index") + 1, pl.lit(minutes))
            .cast(pl.UInt32)
            .alias("expected_bars")
        )
        .collect()
    )

    assert result[BARS_IN_WINDOW].to_list() == naive["expected_bars"].to_list()
    assert result[WINDOW_VOLUME].to_list() == naive["volume"].to_list()
    for computed, product, volume in zip(
        result[VWAP], naive["product"], naive["volume"], strict=True
    ):
        if volume == 0:
            assert computed is None
        else:
            expected = product / volume
            assert computed == pytest.approx(expected, rel=1e-9, abs=1e-9)


@settings(max_examples=50, deadline=None)
@given(entries=_BARS)
def test_session_aggregation_conserves_volume_and_bounds_the_prices(
    entries: list[tuple[float, float, float, int]],
) -> None:
    bars = bar_frame(_dense(entries))
    source = bars.collect()
    result = daily_bars(bars)

    assert result[BAR_COUNT].sum() == source.height
    assert result[C.VOLUME].sum() == source[C.VOLUME].sum()
    for row in result.iter_rows(named=True):
        assert row[C.LOW] <= row[C.OPEN] <= row[C.HIGH]
        assert row[C.LOW] <= row[C.CLOSE] <= row[C.HIGH]
