"""Ingestion against real vendor data and the vendor's own published diagnostics.

The sample is pre-normalised — zero duplicates, zero nulls, zero non-positive prices —
so what ingestion has to prove on real data is *fidelity*, not defect detection: every
row survives, every value is unchanged, and every instant is the one the vendor meant.

That last one is the whole ballgame. `timestamp_ms` and `timestamp_chicago_wall` are
Chicago wall clock, **not** UTC. Reading either as UTC shifts every bar by five or six
hours, still produces a plausible-looking frame, and quietly corrupts every session
aggregate downstream. These tests pin it down with an independent `zoneinfo` oracle.

The vendor diagnostics committed alongside the slices are the golden source for the
quality engine (WP4): `invalid_ohlc` must reproduce 43 and `stale_bar` 14,152
corpus-wide. This module asserts the *contract* — that the numbers are there and are
what PLAN §0.1 says — so those tests have something to stand on.
"""

from __future__ import annotations

from datetime import UTC, date, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, C
from mdq.ingest import HF_DAILY, HF_MINUTE, IngestOptions, detect_profile, ingest_file, normalise
from mdq.time.localise import CHICAGO
from support.fixtures import (
    CLG26_DAILY,
    ESH26_DAILY,
    ESH26_MINUTE,
    vendor_diagnostics,
)

pytestmark = pytest.mark.golden

CT = ZoneInfo(CHICAGO)

#: PLAN §0.1 — independently reproduced across all 40 daily files, 0 mismatches.
VENDOR_TOTALS = {
    "invalid_ohlc_row_count": 43,
    "no_range_bar_count": 14_152,
    "duplicate_timestamp_count": 0,
    "null_price_row_count": 0,
}


# --------------------------------------------------------------------------- #
# the three real slices
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("path", "profile", "frequency"),
    [
        (ESH26_MINUTE, HF_MINUTE, Frequency.MINUTE),
        (ESH26_DAILY, HF_DAILY, Frequency.DAILY),
        (CLG26_DAILY, HF_DAILY, Frequency.DAILY),
    ],
)
def test_every_real_file_normalises_onto_the_canonical_schema_without_losing_a_row(
    path: object, profile: object, frequency: Frequency
) -> None:
    result = ingest_file(path)  # type: ignore[arg-type]
    frame = result.bars.collect()
    expected = pl.read_parquet(path).height  # type: ignore[arg-type]
    assert result.stats.profile == profile.name  # type: ignore[attr-defined]
    assert result.bars.frequency is frequency
    assert frame.schema == BAR_SCHEMA
    assert (result.stats.rows_in, result.stats.rows_out) == (expected, expected)
    assert result.rejects.height == 0


def test_the_vendor_layouts_are_detected_from_their_columns_alone() -> None:
    assert detect_profile(pl.scan_parquet(ESH26_MINUTE).collect_schema().names()) is HF_MINUTE
    assert detect_profile(pl.scan_parquet(ESH26_DAILY).collect_schema().names()) is HF_DAILY


@pytest.mark.parametrize(("path", "contract"), [(ESH26_DAILY, "ESH26"), (CLG26_DAILY, "CLG26")])
def test_daily_row_counts_reproduce_the_vendor_manifest(path: object, contract: str) -> None:
    published = vendor_diagnostics("daily").filter(pl.col("contract_symbol") == contract)
    assert published.height == 1
    result = ingest_file(path)  # type: ignore[arg-type]
    assert result.stats.rows_out == published.get_column("row_count").item()
    assert result.stats.contracts == (contract,)


def test_no_value_is_altered_on_the_way_through() -> None:
    source = (
        pl.read_parquet(ESH26_MINUTE)
        .with_row_index(C.ROW_ID)
        .with_columns(pl.col(C.ROW_ID).cast(pl.UInt32))
    )
    bars = ingest_file(ESH26_MINUTE).bars.collect()
    joined = bars.join(source, on=C.ROW_ID, suffix="_src")
    for column in (*C.PRICES, C.VOLUME):
        assert joined.get_column(column).equals(
            joined.get_column(f"{column}_src"), check_names=False
        )


# --------------------------------------------------------------------------- #
# the timestamp trap
# --------------------------------------------------------------------------- #
def test_the_local_clock_is_the_vendors_wall_clock_exactly() -> None:
    source = (
        pl.read_parquet(ESH26_MINUTE)
        .with_row_index(C.ROW_ID)
        .with_columns(pl.col(C.ROW_ID).cast(pl.UInt32))
    )
    bars = ingest_file(ESH26_MINUTE).bars.collect()
    joined = bars.join(source, on=C.ROW_ID)
    assert joined.get_column(C.TS_LOCAL).equals(
        joined.get_column("timestamp_chicago_wall").cast(pl.Datetime("us")), check_names=False
    )


def test_the_canonical_instant_is_the_localised_wall_clock_not_the_naive_one() -> None:
    frame = ingest_file(ESH26_MINUTE).bars.collect()
    sample = frame.select(C.TS_LOCAL, C.TS_UTC).sample(n=200, seed=11)
    for wall, instant in sample.iter_rows():
        # zoneinfo, not polars: an independent oracle rather than a restatement.
        assert instant == wall.replace(tzinfo=CT).astimezone(UTC)
    # And it is emphatically *not* the naive reading, which is the trap.
    first = frame.row(0, named=True)
    assert first[C.TS_UTC] != first[C.TS_LOCAL].replace(tzinfo=UTC)


def test_the_epoch_fallback_decodes_to_the_same_instants_as_the_wall_clock_column() -> None:
    # `timestamp_ms` is a Chicago wall clock in milliseconds. Declaring it as such —
    # `epoch_ms_chicago_wall` — must reproduce the primary column exactly.
    raw = pl.scan_parquet(ESH26_MINUTE)
    primary = normalise(raw, HF_MINUTE, IngestOptions(source=str(ESH26_MINUTE)))
    fallback = normalise(
        raw.drop("timestamp_chicago_wall"), HF_MINUTE, IngestOptions(source=str(ESH26_MINUTE))
    )
    assert fallback.bars.collect().equals(primary.bars.collect())


def test_reading_the_epoch_as_utc_would_move_every_bar_by_a_whole_session() -> None:
    # The failure this codebase exists to prevent, made visible. Note that the error is
    # not even a constant: the slice straddles the 2026-03-08 transition, so a naive
    # "just subtract six hours" repair would be wrong for half of it.
    raw = pl.scan_parquet(ESH26_MINUTE).drop("timestamp_chicago_wall")
    correct = normalise(raw, HF_MINUTE).bars.collect().get_column(C.TS_UTC)
    wrong = (
        normalise(raw, HF_MINUTE, IngestOptions(timestamp_kind="epoch_ms_utc"))
        .bars.collect()
        .get_column(C.TS_UTC)
    )
    assert sorted((correct - wrong).unique().to_list()) == [timedelta(hours=5), timedelta(hours=6)]


# --------------------------------------------------------------------------- #
# sessions on real data
# --------------------------------------------------------------------------- #
def test_bars_at_or_after_the_roll_belong_to_the_next_session() -> None:
    frame = ingest_file(ESH26_MINUTE).bars.collect()
    rolled = frame.filter(pl.col(C.TS_LOCAL).dt.hour() >= 17)
    assert rolled.height > 0
    assert (
        rolled.select((pl.col(C.SESSION_DATE) - pl.col(C.TS_LOCAL).dt.date()).dt.total_days() == 1)
        .to_series()
        .all()
    )


def test_a_sunday_evening_bar_settles_on_the_monday_session() -> None:
    frame = ingest_file(ESH26_MINUTE).bars.collect()
    sunday = frame.filter(
        (pl.col(C.TS_LOCAL).dt.date() == date(2026, 3, 8)) & (pl.col(C.TS_LOCAL).dt.hour() >= 17)
    )
    assert sunday.height > 0
    assert sunday.get_column(C.SESSION_DATE).unique().to_list() == [date(2026, 3, 9)]


def test_the_spring_forward_gap_is_absent_from_the_data_rather_than_rejected() -> None:
    # Both 2025/2026 transitions fall inside the Fri 16:00 -> Sun 17:00 CME closure, so
    # the vendor has no bars in the gap and ingestion has nothing to reject. The slice
    # spans 2026-03-08 precisely so this stays true if the fixture is ever regenerated.
    result = ingest_file(ESH26_MINUTE)
    assert result.rejects.height == 0
    local = result.bars.collect().get_column(C.TS_LOCAL)
    assert local.dt.date().max() >= date(2026, 3, 8)
    in_gap = (local.dt.date() == date(2026, 3, 8)) & (local.dt.hour() == 2)
    assert in_gap.sum() == 0


def test_the_daily_instant_is_a_midnight_utc_label_on_its_own_session_date() -> None:
    source = pl.read_parquet(ESH26_DAILY)
    frame = ingest_file(ESH26_DAILY).bars.collect()
    assert frame.get_column(C.SESSION_DATE).equals(source.get_column("date"), check_names=False)
    assert frame.get_column(C.TS_UTC).equals(
        source.get_column("date").cast(pl.Datetime("us")).dt.replace_time_zone("UTC"),
        check_names=False,
    )


# --------------------------------------------------------------------------- #
# the vendor's diagnostics, committed for WP4
# --------------------------------------------------------------------------- #
def test_the_vendor_diagnostics_cover_every_file_in_the_sample() -> None:
    published = vendor_diagnostics()
    assert published.height == 80
    assert published.get_column("frequency").value_counts().sort("frequency").rows() == [
        ("daily", 40),
        ("minute", 40),
    ]


@pytest.mark.parametrize(("column", "total"), sorted(VENDOR_TOTALS.items()))
def test_the_corpus_wide_counts_are_the_ones_the_quality_engine_must_reproduce(
    column: str, total: int
) -> None:
    daily = vendor_diagnostics("daily")
    assert daily.get_column(column).sum() == total


def test_the_committed_slices_carry_the_defects_they_were_chosen_for() -> None:
    published = vendor_diagnostics("daily")
    by_contract = dict(published.select("contract_symbol", "invalid_ohlc_row_count").rows())
    # CLG26 is in the fixture set because it holds real OHLC violations; ESH26 daily is
    # the clean counterpart. Both are needed for the WP4 golden tests to mean anything.
    assert by_contract["CLG26"] == 6
    assert by_contract["ESH26"] == 0
