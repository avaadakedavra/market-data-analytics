"""Wall-clock localisation — the correctness-critical module.

Both US DST transitions are exercised explicitly. The reference answers are computed
with the standard library (`zoneinfo`, `fold`) rather than with polars, so these tests
are an independent oracle and not a restatement of the implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from mdq.domain.findings import RejectReason
from mdq.time.localise import CHICAGO, localise_wall_clock

pytestmark = pytest.mark.unit

CT = ZoneInfo(CHICAGO)

#: 2026-03-08 02:00 CT never happens; 2025-11-02 01:00–01:59 CT happens twice.
SPRING_FORWARD = datetime(2026, 3, 8)
FALL_BACK = datetime(2025, 11, 2)


def _frame(*walls: datetime | None) -> pl.LazyFrame:
    return pl.DataFrame(
        {"row_id": list(range(len(walls))), "wall": list(walls)},
        schema={"row_id": pl.UInt32, "wall": pl.Datetime("us")},
    ).lazy()


def _utc(wall: datetime, *, fold: int = 0) -> datetime:
    """The UTC instant the standard library assigns to a Chicago wall clock."""
    return wall.replace(tzinfo=CT, fold=fold).astimezone(UTC)


def test_ordinary_winter_and_summer_bars_localise_to_the_standard_offsets() -> None:
    winter = datetime(2026, 1, 15, 9, 30)
    summer = datetime(2026, 7, 15, 9, 30)
    kept, rejects = localise_wall_clock(_frame(winter, summer), "wall")
    out = kept.collect()
    assert rejects.collect().height == 0
    assert out["ts_utc"].to_list() == [_utc(winter), _utc(summer)]
    # CST is UTC-6, CDT is UTC-5 — the offset is not a constant.
    assert out["ts_utc"][0] - winter.replace(tzinfo=UTC) == timedelta(hours=6)
    assert out["ts_utc"][1] - summer.replace(tzinfo=UTC) == timedelta(hours=5)
    assert out["is_ambiguous"].to_list() == [False, False]


def test_ts_local_keeps_the_wall_clock_traders_see() -> None:
    wall = datetime(2026, 1, 15, 16, 59)
    out = localise_wall_clock(_frame(wall), "wall")[0].collect()
    assert out["ts_local"].to_list() == [wall]
    assert out.schema["ts_local"] == pl.Datetime("us")
    assert out.schema["ts_utc"] == pl.Datetime("us", "UTC")
    # The source column is left untouched for provenance.
    assert out["wall"].to_list() == [wall]


# --------------------------------------------------------------------------- #
# spring forward
# --------------------------------------------------------------------------- #
def test_spring_forward_gap_is_rejected_not_shifted() -> None:
    before = SPRING_FORWARD.replace(hour=1, minute=30)
    inside = SPRING_FORWARD.replace(hour=2, minute=30)
    after = SPRING_FORWARD.replace(hour=3, minute=30)

    kept, rejects = localise_wall_clock(_frame(before, inside, after), "wall")
    kept_df, reject_df = kept.collect(), rejects.collect()

    assert reject_df.height == 1
    assert reject_df["row_id"].to_list() == [1]
    assert reject_df["reason"].to_list() == [RejectReason.NONEXISTENT_LOCAL_TIME.value]
    # The rejected row is never quietly moved to 03:30.
    assert kept_df["row_id"].to_list() == [0, 2]
    assert kept_df["ts_utc"].to_list() == [_utc(before), _utc(after)]
    assert kept_df["ts_utc"].null_count() == 0
    assert not any(kept_df["is_ambiguous"].to_list())


def test_rejects_carry_the_source_columns_but_not_derived_ones() -> None:
    rejects = localise_wall_clock(_frame(SPRING_FORWARD.replace(hour=2, minute=1)), "wall")[1]
    assert rejects.collect_schema().names() == ["row_id", "wall", "reason"]


def test_an_existing_reason_column_is_replaced_not_duplicated() -> None:
    lf = _frame(SPRING_FORWARD.replace(hour=2, minute=1)).with_columns(
        reason=pl.lit("carried over from an earlier stage")
    )
    rejects = localise_wall_clock(lf, "wall")[1].collect()
    assert rejects.columns == ["row_id", "wall", "reason"]
    assert rejects["reason"].to_list() == [RejectReason.NONEXISTENT_LOCAL_TIME.value]


def test_null_timestamps_are_rejected_so_ts_utc_is_never_null() -> None:
    kept, rejects = localise_wall_clock(_frame(datetime(2026, 1, 2, 3), None), "wall")
    assert rejects.collect()["reason"].to_list() == [RejectReason.UNPARSEABLE_TIMESTAMP.value]
    assert kept.collect()["ts_utc"].null_count() == 0


# --------------------------------------------------------------------------- #
# fall back
# --------------------------------------------------------------------------- #
def test_fall_back_hour_localises_and_is_flagged_ambiguous() -> None:
    repeated = FALL_BACK.replace(hour=1, minute=30)
    kept, rejects = localise_wall_clock(_frame(repeated, repeated), "wall")
    out = kept.collect()

    assert rejects.collect().height == 0
    assert out.height == 2, "both bars of the repeated hour must survive"
    assert out["is_ambiguous"].to_list() == [True, True]
    # 'earliest' is deterministic: the first (CDT) pass.
    assert out["ts_utc"].to_list() == [_utc(repeated, fold=0)] * 2


def test_the_two_readings_of_the_repeated_hour_are_exactly_60_minutes_apart() -> None:
    repeated = FALL_BACK.replace(hour=1, minute=30)
    earliest, latest = _utc(repeated, fold=0), _utc(repeated, fold=1)
    assert latest - earliest == timedelta(minutes=60)

    out = localise_wall_clock(_frame(repeated), "wall")[0].collect()
    # We resolve to the earliest reading, and say so via `is_ambiguous` — the flag
    # exists precisely because the alternative reading is a whole hour away.
    assert out["ts_utc"][0] == earliest
    assert out["is_ambiguous"][0] is True


def test_only_the_repeated_hour_is_flagged() -> None:
    walls = [
        FALL_BACK.replace(hour=0, minute=30),  # unambiguously CDT
        FALL_BACK.replace(hour=1, minute=30),  # repeated
        FALL_BACK.replace(hour=2, minute=30),  # unambiguously CST
    ]
    out = localise_wall_clock(_frame(*walls), "wall")[0].collect()
    assert out["is_ambiguous"].to_list() == [False, True, False]
    assert out["ts_utc"].to_list() == [_utc(w) for w in walls]


def test_consecutive_bars_across_fall_back_stay_ordered_in_utc() -> None:
    walls = [FALL_BACK.replace(hour=h, minute=m) for h, m in [(0, 59), (1, 30), (2, 0)]]
    out = localise_wall_clock(_frame(*walls), "wall")[0].collect()
    instants = out["ts_utc"].to_list()
    assert instants == sorted(instants)


# --------------------------------------------------------------------------- #
# other zones, precision, degenerate inputs
# --------------------------------------------------------------------------- #
def test_a_different_zone_is_honoured() -> None:
    wall = datetime(2026, 6, 1, 12, 0)
    out = localise_wall_clock(_frame(wall), "wall", tz="Europe/London")[0].collect()
    assert out["ts_utc"][0] == wall.replace(tzinfo=ZoneInfo("Europe/London")).astimezone(UTC)


def test_millisecond_precision_input_is_upcast_to_microseconds() -> None:
    lf = pl.DataFrame(
        {"wall": [datetime(2026, 1, 5, 8, 30)]},
        schema={"wall": pl.Datetime("ms")},
    ).lazy()
    out = localise_wall_clock(lf, "wall")[0].collect()
    assert out.schema["ts_local"] == pl.Datetime("us")
    assert out.schema["ts_utc"] == pl.Datetime("us", "UTC")


def test_empty_frame_keeps_its_schema() -> None:
    kept, rejects = localise_wall_clock(_frame(), "wall")
    kept_df = kept.collect()
    assert kept_df.height == 0
    assert kept_df.schema["ts_utc"] == pl.Datetime("us", "UTC")
    assert kept_df.schema["is_ambiguous"] == pl.Boolean
    assert rejects.collect().height == 0


def test_missing_column_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="column 'nope' not found"):
        localise_wall_clock(_frame(datetime(2026, 1, 1)), "nope")


def test_non_datetime_column_is_a_clear_error() -> None:
    lf = pl.DataFrame({"wall": ["2026-01-01 00:00:00"]}).lazy()
    with pytest.raises(ValueError, match="must be a Datetime"):
        localise_wall_clock(lf, "wall")


def test_already_aware_column_is_refused() -> None:
    """The HF trap: a column already in UTC must not be re-localised."""
    lf = pl.DataFrame({"wall": [datetime(2026, 1, 1, tzinfo=UTC)]}).lazy()
    with pytest.raises(ValueError, match="already time-zone aware"):
        localise_wall_clock(lf, "wall")
