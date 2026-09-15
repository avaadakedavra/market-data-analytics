"""BAR_SCHEMA / FINDING_SCHEMA contracts and the BarFrame carrier."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime

import polars as pl
import pytest

from mdq.domain.frequency import Frequency
from mdq.domain.schema import (
    BAR_SCHEMA,
    FINDING_SCHEMA,
    BarFrame,
    C,
    SchemaValidationError,
    empty_bar_lf,
    empty_finding_df,
)

pytestmark = pytest.mark.unit


def test_bar_schema_is_the_twelve_column_canonical_schema() -> None:
    assert BAR_SCHEMA.names() == [
        "row_id",
        "contract",
        "exchange",
        "root",
        "ts_utc",
        "ts_local",
        "session_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
    ]
    assert BAR_SCHEMA["row_id"] == pl.UInt32
    assert BAR_SCHEMA["ts_utc"] == pl.Datetime("us", "UTC")
    assert BAR_SCHEMA["ts_local"] == pl.Datetime("us")
    assert BAR_SCHEMA["ts_local"].time_zone is None
    assert BAR_SCHEMA["session_date"] == pl.Date
    assert all(BAR_SCHEMA[c] == pl.Float64 for c in C.PRICES)
    assert BAR_SCHEMA["volume"] == pl.Int64 and BAR_SCHEMA["open_interest"] == pl.Int64


def test_column_constants_cover_the_schema() -> None:
    names = {
        getattr(C, attr) for attr in dir(C) if attr.isupper() and isinstance(getattr(C, attr), str)
    }
    assert set(BAR_SCHEMA.names()) <= names
    assert set(FINDING_SCHEMA.names()) <= names


def test_finding_schema_shape() -> None:
    assert FINDING_SCHEMA.names() == [
        "check_id",
        "severity",
        "contract",
        "frequency",
        "start_utc",
        "end_utc",
        "session_date",
        "count",
        "message",
        "evidence",
        "suggested_rule_id",
    ]
    assert FINDING_SCHEMA["start_utc"] == pl.Datetime("us", "UTC")
    assert FINDING_SCHEMA["count"] == pl.UInt32
    # evidence is JSON text, so a finding row is always flat and serialisable.
    assert FINDING_SCHEMA["evidence"] == pl.String


def test_empty_helpers_carry_the_schema() -> None:
    assert empty_bar_lf().collect_schema() == BAR_SCHEMA
    assert empty_finding_df().schema == FINDING_SCHEMA
    assert empty_finding_df().height == 0


def test_bar_frame_accepts_a_conforming_lazy_frame(minute_df: pl.DataFrame) -> None:
    bars = BarFrame(minute_df.lazy(), Frequency.MINUTE, "unit-test")
    assert not bars.is_empty()
    assert bars.contracts() == ["ESH26"]
    assert bars.collect().height == minute_df.height
    assert bars.source == "unit-test"


def test_bar_frame_accepts_an_eager_frame_and_makes_it_lazy(minute_df: pl.DataFrame) -> None:
    bars = BarFrame(minute_df, Frequency.MINUTE)  # type: ignore[arg-type]
    assert isinstance(bars.lf, pl.LazyFrame)


def test_bar_frame_is_frozen(minute_bars: BarFrame) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        minute_bars.source = "mutated"  # type: ignore[misc]


def test_empty_bar_frame_is_usable() -> None:
    bars = BarFrame.empty(Frequency.DAILY)
    assert bars.is_empty()
    assert bars.contracts() == []
    assert bars.collect().schema == BAR_SCHEMA


def test_contracts_are_sorted_and_distinct() -> None:
    df = pl.DataFrame(
        {
            **{c: [] for c in BAR_SCHEMA.names()},
        },
        schema=BAR_SCHEMA,
    )
    rows = pl.DataFrame(
        {
            "row_id": [0, 1, 2],
            "contract": ["ZNH26", "ESH26", "ESH26"],
            "exchange": ["CBOT", "CME", "CME"],
            "root": ["ZN", "ES", "ES"],
            "ts_utc": [datetime(2026, 3, 4, tzinfo=UTC)] * 3,
            "ts_local": [datetime(2026, 3, 3, 18)] * 3,
            "session_date": [date(2026, 3, 4)] * 3,
            "open": [1.0, 2.0, 3.0],
            "high": [1.0, 2.0, 3.0],
            "low": [1.0, 2.0, 3.0],
            "close": [1.0, 2.0, 3.0],
            "volume": [1, 2, 3],
            "open_interest": [None, None, None],
        },
        schema=BAR_SCHEMA,
    )
    assert BarFrame(pl.concat([df, rows]).lazy(), Frequency.DAILY).contracts() == [
        "ESH26",
        "ZNH26",
    ]


@pytest.mark.parametrize(
    ("mutate", "expected_fragment"),
    [
        (lambda df: df.drop("volume"), "missing columns ['volume']"),
        (lambda df: df.with_columns(extra=pl.lit(1)), "unexpected columns ['extra']"),
        (
            lambda df: df.with_columns(pl.col("volume").cast(pl.Float64)),
            "wrong dtypes",
        ),
        (
            lambda df: df.with_columns(pl.col("ts_utc").dt.replace_time_zone(None)),
            "expected Datetime(time_unit='us', time_zone='UTC')",
        ),
        (
            lambda df: df.select(["contract", *[c for c in BAR_SCHEMA.names() if c != "contract"]]),
            "wrong column order",
        ),
    ],
)
def test_bar_frame_rejects_non_conforming_frames(
    minute_df: pl.DataFrame,
    mutate: object,
    expected_fragment: str,
) -> None:
    broken = mutate(minute_df)  # type: ignore[operator]
    with pytest.raises(SchemaValidationError) as excinfo:
        BarFrame(broken.lazy(), Frequency.MINUTE, "broken.parquet")
    message = str(excinfo.value)
    assert "broken.parquet" in message
    assert expected_fragment in message


def test_bar_frame_rejects_a_non_frame() -> None:
    with pytest.raises(SchemaValidationError, match="must be a polars LazyFrame"):
        BarFrame({"row_id": [1]}, Frequency.MINUTE)  # type: ignore[arg-type]


def test_with_lf_revalidates(minute_bars: BarFrame) -> None:
    same = minute_bars.with_lf(minute_bars.lf.filter(pl.col("row_id") < 3))
    assert same.frequency is minute_bars.frequency
    assert same.collect().height == 3
    with pytest.raises(SchemaValidationError):
        minute_bars.with_lf(minute_bars.lf.drop("close"))
