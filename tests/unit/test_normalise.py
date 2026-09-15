"""Normalisation: the canonical schema, and a reason for everything that is dropped.

Two rules are asserted over and over here because everything else depends on them:

1. A row is rejected only when it cannot be **located**. A bad *price* never removes a
   row — it becomes a visible null that the quality engine flags in context.
2. Nothing is ever silently repaired. `"1,234.50"` does not become `1234.5`, `12.5`
   volume does not become `12`, and a duplicate is not quietly dropped.

The malformed-CSV test asserts the rejects **row by row and reason by reason**: a count
would pass even if the pipeline blamed the wrong line, which is the failure that would
actually mislead a user.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from mdq.domain.findings import RejectReason
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, C
from mdq.ingest import (
    GENERIC,
    IngestOptions,
    IngestResult,
    MixedFrequencyError,
    TooManyMalformedLinesError,
    UnsupportedFormatError,
    ingest_file,
    normalise,
    read_bars,
    read_buffer,
)
from mdq.ingest.report import RAW_VALUES, REJECT_SCHEMA, SOURCE
from mdq.time.localise import CHICAGO
from support.fixtures import GENERIC_CLEAN, GENERIC_MALFORMED, GENERIC_UTC_ISO

pytestmark = pytest.mark.unit

CT = ZoneInfo(CHICAGO)
HEADER = "contract,timestamp,open,high,low,close,volume"


def _write(tmp_path: Path, *rows: str, header: str = HEADER, name: str = "bars.csv") -> Path:
    path = tmp_path / name
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def _ingest(tmp_path: Path, *rows: str, **kwargs: object) -> IngestResult:
    header = str(kwargs.pop("header", HEADER))
    options = kwargs.pop("options", None)
    assert not kwargs, kwargs
    path = _write(tmp_path, *rows, header=header)
    return ingest_file(path, options)  # type: ignore[arg-type]


def _rejects(result: IngestResult) -> list[tuple[int | None, str]]:
    return [(row[C.ROW_ID], row[C.REASON]) for row in result.rejects.iter_rows(named=True)]


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
def test_a_clean_csv_becomes_the_canonical_schema_with_nothing_rejected() -> None:
    result = ingest_file(GENERIC_CLEAN)
    frame = result.bars.collect()
    assert frame.schema == BAR_SCHEMA
    assert result.rejects.schema == REJECT_SCHEMA
    assert (result.stats.rows_in, result.stats.rows_out) == (6, 6)
    assert result.is_clean
    assert result.bars.frequency is Frequency.MINUTE


def test_output_is_sorted_by_contract_then_instant_then_row_id() -> None:
    frame = ingest_file(GENERIC_CLEAN).bars.collect()
    assert frame.get_column(C.CONTRACT).to_list() == ["CLG26"] * 3 + ["ESH26"] * 3
    assert frame.get_column(C.ROW_ID).to_list() == [1, 3, 5, 0, 2, 4]


def test_a_file_whose_contracts_are_interleaved_is_reported_as_unsorted() -> None:
    # Not an error — but worth knowing, because an unordered vendor file is usually the
    # first sign of a concatenation bug upstream.
    assert ingest_file(GENERIC_CLEAN).stats.was_sorted is False


def test_the_wall_clock_is_localised_rather_than_treated_as_utc() -> None:
    frame = ingest_file(GENERIC_CLEAN).bars.collect()
    first = frame.row(0, named=True)
    assert first[C.TS_LOCAL] == datetime(2026, 3, 3, 9, 0)
    # 09:00 CT in March, before the transition, is CST = UTC-6.
    assert first[C.TS_UTC] == datetime(2026, 3, 3, 9, 0, tzinfo=CT).astimezone(UTC)


def test_root_is_derived_from_the_contract_when_the_source_has_none(tmp_path: Path) -> None:
    result = _ingest(
        tmp_path,
        "SR3H26,2026-03-03 09:00:00,1,1,1,1,1",
        "ESH26,2026-03-03 09:00:00,1,1,1,1,1",
        header="contract,timestamp,open,high,low,close,volume",
    )
    # Stripping the expiry keeps a numeric root intact; "leading letters" would give SR.
    assert result.bars.collect().get_column(C.ROOT).to_list() == ["ESH26"[:2], "SR3"]


def test_an_iso_utc_source_is_declared_not_guessed() -> None:
    declared = ingest_file(GENERIC_UTC_ISO, IngestOptions(timestamp_kind="iso_utc"))
    frame = declared.bars.collect()
    assert declared.is_clean
    assert frame.row(0, named=True)[C.TS_UTC] == datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    assert frame.row(0, named=True)[C.TS_LOCAL] == datetime(2026, 3, 3, 9, 0)


def test_an_offset_bearing_string_yields_the_same_instant_however_it_is_declared() -> None:
    declared = ingest_file(GENERIC_UTC_ISO, IngestOptions(timestamp_kind="iso_utc"))
    inferred = ingest_file(GENERIC_UTC_ISO)
    assert inferred.bars.collect().equals(declared.bars.collect())


def test_an_epoch_column_declared_as_a_wall_clock_is_not_read_as_utc(tmp_path: Path) -> None:
    # The HuggingFace trap, in miniature: the same integer means different instants
    # depending on what the source says it is, so the source has to say.
    epoch_ms = int(datetime(2026, 3, 3, 9, 0, tzinfo=UTC).timestamp() * 1000)
    rows = (f"ESH26,{epoch_ms},1,1,1,1,1",)
    wall = _ingest(tmp_path, *rows, options=IngestOptions(timestamp_kind="epoch_ms_chicago_wall"))
    utc = _ingest(tmp_path, *rows, options=IngestOptions(timestamp_kind="epoch_ms_utc"))
    assert wall.bars.collect().row(0, named=True)[C.TS_LOCAL] == datetime(2026, 3, 3, 9, 0)
    assert utc.bars.collect().row(0, named=True)[C.TS_LOCAL] == datetime(2026, 3, 3, 3, 0)


# --------------------------------------------------------------------------- #
# the malformed fixture, row by row
# --------------------------------------------------------------------------- #
def test_the_malformed_csv_rejects_exactly_the_rows_that_cannot_be_located() -> None:
    result = ingest_file(GENERIC_MALFORMED)
    assert _rejects(result) == [
        (9, RejectReason.UNPARSEABLE_TIMESTAMP.value),
        (13, RejectReason.NULL_CONTRACT.value),
        (21, RejectReason.MALFORMED_LINE.value),
    ]
    assert result.stats.rejects_by_reason == {
        RejectReason.MALFORMED_LINE.value: 1,
        RejectReason.NULL_CONTRACT.value: 1,
        RejectReason.UNPARSEABLE_TIMESTAMP.value: 1,
    }


def test_the_malformed_csv_keeps_every_other_row_including_the_bad_values() -> None:
    frame = ingest_file(GENERIC_MALFORMED).bars.collect()
    assert frame.get_column(C.ROW_ID).to_list() == [i for i in range(25) if i not in {9, 13, 21}]


def test_text_in_a_price_nulls_that_one_value_and_keeps_the_row() -> None:
    row = _row_by_id(ingest_file(GENERIC_MALFORMED).bars.collect(), 5)
    assert row[C.OPEN] is None
    # The rest of the bar survives: the defect is one cell, not one row.
    assert (row[C.HIGH], row[C.LOW], row[C.CLOSE]) == (6102.5, 6100.5, 6101.75)


def test_a_thousands_separator_is_refused_rather_than_reinterpreted() -> None:
    row = _row_by_id(ingest_file(GENERIC_MALFORMED).bars.collect(), 17)
    # The source wrote "6,123.50". Reading that as 6123.5 would invent a price, and
    # reading it as 6 would be worse; the honest answer is a null the user can see.
    assert row[C.CLOSE] is None
    assert row[C.OPEN] == 6104.25


def test_a_reject_names_its_contract_whenever_the_contract_is_knowable() -> None:
    by_row = {
        row[C.ROW_ID]: row for row in ingest_file(GENERIC_MALFORMED).rejects.iter_rows(named=True)
    }
    # The bad timestamp belongs to a known instrument; the other two rejects are
    # precisely the cases where the instrument is what we could not read.
    assert by_row[9][C.CONTRACT] == "ESH26"
    assert by_row[13][C.CONTRACT] is None
    assert by_row[21][C.CONTRACT] is None


def test_a_reject_carries_the_source_text_that_caused_it() -> None:
    result = ingest_file(GENERIC_MALFORMED)
    by_row = {row[C.ROW_ID]: row for row in result.rejects.iter_rows(named=True)}
    assert json.loads(by_row[9][RAW_VALUES])["timestamp"] == "2026-02-30 09:09:00"
    assert json.loads(by_row[13][RAW_VALUES])["contract"] is None
    assert by_row[21][SOURCE] == str(GENERIC_MALFORMED)


def test_a_malformed_line_is_attributed_to_its_position_in_the_file() -> None:
    # The ragged line sends the whole file down the line-by-line path; row ids must
    # still be the positions in the original file, or findings point at the wrong row.
    frame = ingest_file(GENERIC_MALFORMED).bars.collect()
    row = _row_by_id(frame, 22)
    assert row[C.TS_LOCAL] == datetime(2026, 3, 3, 9, 22)


def _row_by_id(frame: pl.DataFrame, row_id: int) -> dict[str, object]:
    return frame.filter(pl.col(C.ROW_ID) == row_id).row(0, named=True)


# --------------------------------------------------------------------------- #
# values that are kept and flagged, never fixed
# --------------------------------------------------------------------------- #
def test_a_whole_number_volume_written_as_a_float_casts_to_an_integer(tmp_path: Path) -> None:
    result = _ingest(tmp_path, "ESH26,2026-03-03 09:00:00,1,1,1,1,12.0")
    assert result.bars.collect().get_column(C.VOLUME).to_list() == [12]


def test_a_fractional_volume_becomes_null_rather_than_being_truncated(tmp_path: Path) -> None:
    result = _ingest(tmp_path, "ESH26,2026-03-03 09:00:00,1,1,1,1,12.5")
    assert result.bars.collect().get_column(C.VOLUME).to_list() == [None]
    assert result.is_clean  # kept, not rejected


def test_a_volume_with_a_thousands_separator_becomes_null(tmp_path: Path) -> None:
    result = _ingest(tmp_path, 'ESH26,2026-03-03 09:00:00,1,1,1,1,"1,000"')
    assert result.bars.collect().get_column(C.VOLUME).to_list() == [None]


def test_null_prices_are_kept_and_left_for_the_quality_engine(tmp_path: Path) -> None:
    result = _ingest(tmp_path, "ESH26,2026-03-03 09:00:00,,,,,")
    frame = result.bars.collect()
    assert frame.height == 1
    assert frame.row(0, named=True)[C.CLOSE] is None
    assert result.is_clean


def test_duplicate_timestamps_are_kept_for_the_quality_engine_to_classify(
    tmp_path: Path,
) -> None:
    result = _ingest(
        tmp_path,
        "ESH26,2026-03-03 09:00:00,1,1,1,1,10",
        "ESH26,2026-03-03 09:00:00,1,1,1,1,10",
        "ESH26,2026-03-03 09:00:00,2,2,2,2,99",
    )
    assert result.bars.collect().height == 3
    assert result.is_clean


def test_a_negative_price_is_kept_because_some_products_really_trade_there(
    tmp_path: Path,
) -> None:
    result = _ingest(tmp_path, "CLG26,2026-03-03 09:00:00,-37.6,-37.6,-40.3,-37.6,1")
    assert result.bars.collect().row(0, named=True)[C.OPEN] == -37.6
    assert result.is_clean


# --------------------------------------------------------------------------- #
# time: sessions and DST
# --------------------------------------------------------------------------- #
def test_the_session_rolls_at_seventeen_hundred_local(tmp_path: Path) -> None:
    result = _ingest(
        tmp_path,
        "ESH26,2026-03-03 16:59:00,1,1,1,1,1",
        "ESH26,2026-03-03 17:00:00,1,1,1,1,1",
        header="contract,timestamp,open,high,low,close,volume",
        options=IngestOptions(default_exchange="CME"),
    )
    assert result.bars.collect().get_column(C.SESSION_DATE).to_list() == [
        date(2026, 3, 3),
        date(2026, 3, 4),
    ]


def test_a_venue_with_no_session_profile_uses_the_calendar_date(tmp_path: Path) -> None:
    result = _ingest(
        tmp_path,
        "ESH26,2026-03-03 17:00:00,1,1,1,1,1",
        options=IngestOptions(default_exchange="ICEUS"),
    )
    assert result.bars.collect().get_column(C.SESSION_DATE).to_list() == [date(2026, 3, 3)]


def test_a_wall_clock_that_never_happened_is_rejected_not_shifted(tmp_path: Path) -> None:
    result = _ingest(
        tmp_path,
        "ESH26,2026-03-08 01:59:00,1,1,1,1,1",
        "ESH26,2026-03-08 02:30:00,1,1,1,1,1",
        "ESH26,2026-03-08 03:00:00,1,1,1,1,1",
    )
    assert _rejects(result) == [(1, RejectReason.NONEXISTENT_LOCAL_TIME.value)]
    assert result.bars.collect().height == 2


def test_both_passes_of_the_repeated_hour_are_kept(tmp_path: Path) -> None:
    # Under ambiguous="earliest" they necessarily share an instant; that collision is
    # itself the finding, and it is the quality engine's to report, not ingestion's.
    result = _ingest(
        tmp_path,
        "ESH26,2025-11-02 01:30:00,1,1,1,1,1",
        "ESH26,2025-11-02 01:30:00,2,2,2,2,2",
    )
    frame = result.bars.collect()
    assert result.is_clean
    assert frame.get_column(C.TS_UTC).n_unique() == 1


# --------------------------------------------------------------------------- #
# frequency
# --------------------------------------------------------------------------- #
def test_daily_bars_are_recognised_and_labelled_at_midnight_utc(tmp_path: Path) -> None:
    result = _ingest(
        tmp_path,
        "ESH26,2026-03-03,1,2,0.5,1.5,10",
        "ESH26,2026-03-04,1,2,0.5,1.5,10",
        header="contract,date,open,high,low,close,volume",
    )
    frame = result.bars.collect()
    assert result.bars.frequency is Frequency.DAILY
    assert frame.get_column(C.TS_UTC).to_list() == [
        datetime(2026, 3, 3, tzinfo=UTC),
        datetime(2026, 3, 4, tzinfo=UTC),
    ]
    assert frame.get_column(C.SESSION_DATE).to_list() == [date(2026, 3, 3), date(2026, 3, 4)]


def test_a_pre_1970_daily_label_is_ordinary_data(tmp_path: Path) -> None:
    # A negative epoch is not a defect; refusing it would silently lose old history.
    result = _ingest(
        tmp_path,
        "ESH26,-86400000,1,2,0.5,1.5,10",
        "ESH26,0,1,2,0.5,1.5,10",
        options=IngestOptions(timestamp_kind="epoch_ms_chicago_wall"),
    )
    frame = result.bars.collect()
    assert result.is_clean
    assert frame.get_column(C.SESSION_DATE).to_list() == [date(1969, 12, 31), date(1970, 1, 1)]


def test_a_file_mixing_daily_and_intraday_bars_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MixedFrequencyError, match=r"declare IngestOptions\.frequency"):
        _ingest(
            tmp_path,
            "ESH26,2026-03-03 00:00:00,1,1,1,1,1",
            "ESH26,2026-03-04 09:30:00,1,1,1,1,1",
            "ESH26,2026-03-04 09:31:00,1,1,1,1,1",
        )


def test_declaring_the_frequency_overrides_inference(tmp_path: Path) -> None:
    result = _ingest(
        tmp_path,
        "ESH26,2026-03-03 00:00:00,1,1,1,1,1",
        "ESH26,2026-03-04 09:30:00,1,1,1,1,1",
        "ESH26,2026-03-04 09:31:00,1,1,1,1,1",
        options=IngestOptions(frequency=Frequency.MINUTE),
    )
    assert result.bars.frequency is Frequency.MINUTE
    assert result.bars.collect().height == 3


def test_a_duplicated_daily_row_does_not_make_a_file_look_mixed(tmp_path: Path) -> None:
    result = _ingest(
        tmp_path,
        "ESH26,2026-03-03 00:00:00,1,1,1,1,1",
        "ESH26,2026-03-03 00:00:00,1,1,1,1,1",
        "ESH26,2026-03-04 00:00:00,1,1,1,1,1",
    )
    assert result.bars.frequency is Frequency.DAILY


# --------------------------------------------------------------------------- #
# files that cannot be read at all
# --------------------------------------------------------------------------- #
def test_an_unknown_extension_is_refused_by_name(tmp_path: Path) -> None:
    path = tmp_path / "bars.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError, match="json"):
        read_bars(path)


def test_an_empty_file_yields_an_empty_frame_with_the_right_schema(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    result = ingest_file(path)
    assert result.bars.collect().schema == BAR_SCHEMA
    assert result.bars.is_empty()
    assert result.rejects.height == 0
    assert result.stats.rows_in == 0
    assert result.stats.reject_ratio == 0.0


def test_a_header_with_no_rows_yields_an_empty_frame(tmp_path: Path) -> None:
    result = ingest_file(_write(tmp_path))
    assert result.bars.is_empty()
    assert result.rejects.height == 0
    assert result.stats.contracts == ()
    assert result.stats.span is None


def test_a_file_that_cannot_locate_its_bars_reports_one_file_level_reject(
    tmp_path: Path,
) -> None:
    result = ingest_file(_write(tmp_path, "1,2", header="alpha,beta"))
    assert _rejects(result) == [(None, RejectReason.MISSING_REQUIRED_COLUMN.value)]
    detail = json.loads(result.rejects.row(0, named=True)[RAW_VALUES])
    assert "contract" in detail["required"] and "timestamp" in detail["required"]
    assert detail["columns"] == "alpha, beta"
    assert result.bars.is_empty()
    assert result.stats.rows_in == 1


def test_a_file_that_is_mostly_unparseable_is_refused_rather_than_half_read(
    tmp_path: Path,
) -> None:
    rows = [f"ESH26,2026-03-03 09:0{i}:00,1,1,1,1,1,extra" for i in range(4)]
    rows.append("ESH26,2026-03-03 09:05:00,1,1,1,1,1")
    with pytest.raises(TooManyMalformedLinesError, match=r"80\.0%"):
        _ingest(tmp_path, *rows)


def test_the_malformed_bound_is_a_policy_the_caller_can_relax(tmp_path: Path) -> None:
    rows = [f"ESH26,2026-03-03 09:0{i}:00,1,1,1,1,1,extra" for i in range(4)]
    rows.append("ESH26,2026-03-03 09:05:00,1,1,1,1,1")
    result = _ingest(tmp_path, *rows, options=IngestOptions(max_reject_ratio=0.9))
    assert result.stats.rejects_by_reason == {RejectReason.MALFORMED_LINE.value: 4}
    assert result.bars.collect().height == 1


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def test_read_bars_returns_only_the_bars() -> None:
    bars = read_bars(GENERIC_CLEAN)
    assert bars.contracts() == ["CLG26", "ESH26"]
    assert bars.source == str(GENERIC_CLEAN)


def test_an_upload_is_ingested_from_memory_by_declared_extension() -> None:
    result = read_buffer(GENERIC_CLEAN.read_bytes(), ".csv", IngestOptions(source="upload.csv"))
    assert result.bars.collect().height == 6
    assert result.stats.source == "upload.csv"


def test_a_caller_may_pin_the_profile_instead_of_detecting_it() -> None:
    raw = pl.LazyFrame({"contract": ["ESH26"], "timestamp": ["2026-03-03 09:00:00"]})
    result = normalise(raw, GENERIC, IngestOptions(source="<inline>"))
    assert result.stats.profile == "generic"
    assert result.bars.collect().row(0, named=True)[C.CLOSE] is None


def test_the_statistics_serialise_for_the_report() -> None:
    stats = ingest_file(GENERIC_MALFORMED).stats.to_dict()
    assert stats["rows_in"] == 25
    assert stats["rows_out"] == 22
    assert stats["rows_rejected"] == 3
    assert stats["contracts"] == ["ESH26"]
    assert stats["frequency"] == "minute"
    assert stats["profile"] == "generic"


def test_a_source_column_called_row_id_does_not_displace_the_canonical_one(
    tmp_path: Path,
) -> None:
    result = _ingest(
        tmp_path,
        "ESH26,2026-03-03 09:00:00,7",
        header="contract,timestamp,row_id",
    )
    assert result.bars.collect().get_column(C.ROW_ID).to_list() == [0]


# --------------------------------------------------------------------------- #
# a declared kind meets a source dtype it did not expect
# --------------------------------------------------------------------------- #
def _inline(timestamps: pl.Series, **options: object) -> IngestResult:
    raw = pl.LazyFrame({"contract": ["ESH26"] * timestamps.len(), "timestamp": timestamps})
    return normalise(raw, GENERIC, IngestOptions(source="<inline>", **options))  # type: ignore[arg-type]


def test_a_date_typed_column_declared_as_a_wall_clock_reads_as_midnight() -> None:
    result = _inline(pl.Series([date(2026, 3, 3)], dtype=pl.Date))
    row = result.bars.collect().row(0, named=True)
    assert result.bars.frequency is Frequency.DAILY
    assert row[C.TS_LOCAL] == datetime(2026, 3, 3)


def test_a_column_that_already_carries_a_zone_keeps_its_instant() -> None:
    # Declared as a wall clock, but the source has told us the instant outright; the
    # offset is honoured rather than thrown away.
    stamps = pl.Series([datetime(2026, 3, 3, 15, 0, tzinfo=UTC)]).dt.cast_time_unit("us")
    row = _inline(stamps).bars.collect().row(0, named=True)
    assert row[C.TS_LOCAL] == datetime(2026, 3, 3, 9, 0)
    assert row[C.TS_UTC] == datetime(2026, 3, 3, 15, 0, tzinfo=UTC)


def test_an_epoch_declaration_over_an_already_decoded_column_is_a_no_op() -> None:
    stamps = pl.Series([datetime(2026, 3, 3, 9, 0)], dtype=pl.Datetime("ms"))
    row = _inline(stamps, timestamp_kind="epoch_ms_chicago_wall").bars.collect().row(0, named=True)
    assert row[C.TS_LOCAL] == datetime(2026, 3, 3, 9, 0)


@pytest.mark.parametrize(
    "stamps",
    [
        pl.Series([datetime(2026, 3, 3, 15, 0)], dtype=pl.Datetime("us")),
        pl.Series([datetime(2026, 3, 3, 15, 0, tzinfo=UTC)]).dt.convert_time_zone("Europe/London"),
    ],
)
def test_a_utc_declaration_over_a_typed_column_yields_the_same_instant(stamps: pl.Series) -> None:
    row = _inline(stamps, timestamp_kind="iso_utc").bars.collect().row(0, named=True)
    assert row[C.TS_UTC] == datetime(2026, 3, 3, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "stamps",
    [
        pl.Series(["2026-03-03"], dtype=pl.String),
        pl.Series([datetime(2026, 3, 3, 16, 30)], dtype=pl.Datetime("us")),
    ],
)
def test_a_date_declaration_collapses_whatever_it_is_given_to_a_day(stamps: pl.Series) -> None:
    result = _inline(stamps, timestamp_kind="date")
    row = result.bars.collect().row(0, named=True)
    assert row[C.SESSION_DATE] == date(2026, 3, 3)
    assert row[C.TS_UTC] == datetime(2026, 3, 3, tzinfo=UTC)


def test_an_instant_that_cannot_be_parsed_is_rejected_whatever_the_declared_kind() -> None:
    result = _inline(
        pl.Series(["not-a-date", "2026-03-03T15:00:00Z"], dtype=pl.String),
        timestamp_kind="iso_utc",
    )
    assert _rejects(result) == [(0, RejectReason.UNPARSEABLE_TIMESTAMP.value)]
    assert result.bars.collect().height == 1


def test_a_session_table_with_no_rolling_venue_falls_back_to_calendar_dates(
    tmp_path: Path,
) -> None:
    from mdq.time.sessions import CALENDAR_SESSION

    result = _ingest(
        tmp_path,
        "ESH26,2026-03-03 17:00:00,1,1,1,1,1",
        options=IngestOptions(default_exchange="CME", sessions={"CME": CALENDAR_SESSION}),
    )
    assert result.bars.collect().get_column(C.SESSION_DATE).to_list() == [date(2026, 3, 3)]


def test_a_timestamp_column_of_pure_nonsense_reports_every_row_instead_of_crashing(
    tmp_path: Path,
) -> None:
    result = _ingest(
        tmp_path,
        "ESH26,nonsense,1,1,1,1,1",
        "ESH26,also-nonsense,1,1,1,1,1",
        options=IngestOptions(max_reject_ratio=1.0),
    )
    assert _rejects(result) == [
        (0, RejectReason.UNPARSEABLE_TIMESTAMP.value),
        (1, RejectReason.UNPARSEABLE_TIMESTAMP.value),
    ]
