"""Readers and the reader registry.

The registry's job is that adding a format touches no existing code, so the tests
register a throw-away reader and assert it becomes reachable through the public API.
The CSV tests concentrate on the two ways a line can be the wrong shape, because one of
them (a *short* line) is silently padded by polars and would otherwise turn a data
defect into an invisible null.
"""

from __future__ import annotations

import io
from pathlib import Path

import polars as pl
import pytest

from mdq.ingest.errors import UnsupportedFormatError
from mdq.ingest.readers import (
    MALFORMED_LINE_COLUMN,
    READERS,
    ReaderRegistry,
    extension_of,
    load_readers,
    read_raw,
    register_reader,
    supported_extensions,
)
from mdq.ingest.readers.csv import CsvReader
from mdq.ingest.readers.parquet import ParquetReader
from support.fixtures import ESH26_DAILY, GENERIC_CLEAN, GENERIC_MALFORMED

pytestmark = pytest.mark.unit

HEADER = "contract,timestamp,close\n"


def _csv(tmp_path: Path, body: str, name: str = "sample.csv") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_the_shipped_readers_are_discovered_without_being_imported_by_name() -> None:
    # `supported_extensions` triggers the pkgutil sweep; nothing imported the modules.
    assert {".csv", ".parquet"} <= supported_extensions()


def test_an_unknown_extension_names_the_formats_that_are_supported() -> None:
    with pytest.raises(UnsupportedFormatError, match=r"\.csv"):
        READERS.get(".json")


def test_a_file_with_no_extension_is_unsupported_rather_than_guessed(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFormatError):
        read_raw(tmp_path / "prices")


def test_registering_a_new_format_needs_no_change_to_existing_code() -> None:
    registry = ReaderRegistry()

    class JsonReader:
        def read(self, source: object) -> pl.LazyFrame:  # pragma: no cover - not called
            raise NotImplementedError

    reader = JsonReader()
    registry.register(reader, [".JSON"])  # case is normalised
    assert registry.get(".json") is reader
    assert registry.extensions() >= {".json"}


def test_two_readers_may_not_claim_the_same_extension() -> None:
    registry = ReaderRegistry()

    class First:
        def read(self, source: object) -> pl.LazyFrame:  # pragma: no cover - not called
            raise NotImplementedError

    class Second:
        def read(self, source: object) -> pl.LazyFrame:  # pragma: no cover - not called
            raise NotImplementedError

    registry.register(First(), [".x"])
    registry.register(First(), [".x"])  # the same reader re-registering is harmless
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Second(), [".x"])


@pytest.mark.parametrize("bad", ["csv", ".", ""])
def test_an_extension_must_look_like_an_extension(bad: str) -> None:
    with pytest.raises(ValueError, match="must look like"):
        ReaderRegistry().register(CsvReader(), [bad])


def test_the_decorator_publishes_the_extensions_it_claims() -> None:
    assert CsvReader.extensions == frozenset({".csv", ".txt"})
    assert ParquetReader.extensions == frozenset({".parquet", ".pq"})


def test_loading_readers_twice_does_not_register_them_twice() -> None:
    load_readers()
    before = supported_extensions()
    load_readers()
    assert supported_extensions() == before


@pytest.mark.parametrize(
    ("path", "expected"),
    [("a/b.CSV", ".csv"), ("b.parquet", ".parquet"), ("noext", "")],
)
def test_extension_of_lower_cases_and_tolerates_absence(path: str, expected: str) -> None:
    assert extension_of(path) == expected


# --------------------------------------------------------------------------- #
# parquet
# --------------------------------------------------------------------------- #
def test_parquet_is_read_lazily_and_keeps_the_vendor_columns() -> None:
    lf = read_raw(ESH26_DAILY)
    assert isinstance(lf, pl.LazyFrame)
    assert "contract_symbol" in lf.collect_schema().names()


def test_parquet_can_be_read_from_an_upload_buffer() -> None:
    payload = ESH26_DAILY.read_bytes()
    assert ParquetReader().read(payload).collect().height == 651


# --------------------------------------------------------------------------- #
# csv
# --------------------------------------------------------------------------- #
def test_every_csv_value_arrives_as_a_string_so_coercion_can_be_attributed() -> None:
    schema = read_raw(GENERIC_CLEAN).collect_schema()
    assert set(schema.dtypes()) == {pl.String}


def test_a_zero_byte_file_is_empty_not_broken(tmp_path: Path) -> None:
    lf = CsvReader().read(_csv(tmp_path, ""))
    assert lf.collect_schema().names() == []


def test_a_header_with_no_rows_keeps_its_columns(tmp_path: Path) -> None:
    frame = CsvReader().read(_csv(tmp_path, HEADER)).collect()
    assert frame.height == 0
    assert frame.columns == ["contract", "timestamp", "close"]


def test_a_line_with_too_many_fields_is_marked_rather_than_truncated(tmp_path: Path) -> None:
    body = HEADER + "ESH26,2026-03-03 09:00:00,6100.0\nESH26,2026-03-03 09:01:00,6101.0,extra\n"
    frame = CsvReader().read(_csv(tmp_path, body)).collect()
    assert frame.get_column(MALFORMED_LINE_COLUMN).to_list() == [
        None,
        "ESH26,2026-03-03 09:01:00,6101.0,extra",
    ]


def test_a_line_with_too_few_fields_is_marked_rather_than_padded_with_nulls(
    tmp_path: Path,
) -> None:
    # polars pads a short line silently; without the fallback this defect would arrive
    # as an unexplained null close instead of a malformed line.
    body = HEADER + "ESH26,2026-03-03 09:00:00,6100.0\nESH26,2026-03-03 09:01:00\n"
    frame = CsvReader().read(_csv(tmp_path, body)).collect()
    assert frame.get_column(MALFORMED_LINE_COLUMN).to_list() == [
        None,
        "ESH26,2026-03-03 09:01:00",
    ]


def test_quoted_fields_survive_the_line_by_line_fallback() -> None:
    frame = CsvReader().read(GENERIC_MALFORMED).collect()
    # Row 17 carries a quoted thousands separator; the comma inside it is not a field
    # separator, and the fallback must not split on it.
    assert frame.get_column("close")[17] == "6,123.50"


def test_blank_lines_are_skipped_so_row_ids_still_point_at_data_rows(tmp_path: Path) -> None:
    body = HEADER + "ESH26,2026-03-03 09:00:00,6100.0\n\nESH26,2026-03-03 09:01:00,6101.0,x\n"
    frame = CsvReader().read(_csv(tmp_path, body)).collect()
    assert frame.height == 2
    assert frame.get_column(MALFORMED_LINE_COLUMN)[1] is not None


def test_a_file_of_only_whitespace_has_no_data_rows(tmp_path: Path) -> None:
    assert CsvReader().read(_csv(tmp_path, "\n \n")).collect().height == 0


def test_repeated_header_names_are_disambiguated_not_dropped(tmp_path: Path) -> None:
    body = "a,a,b\n1,2\n"
    frame = CsvReader().read(_csv(tmp_path, body)).collect()
    assert frame.columns == ["a", "a_duplicated_0", "b", MALFORMED_LINE_COLUMN]


def test_a_csv_may_arrive_as_an_upload_buffer_rather_than_a_path() -> None:
    buffer = io.BytesIO(GENERIC_CLEAN.read_bytes())
    assert CsvReader().read(buffer).collect().height == 6


def test_a_reader_may_be_pointed_at_a_string_path() -> None:
    assert CsvReader().read(str(GENERIC_CLEAN)).collect().height == 6


def test_register_reader_rejects_a_second_claim_on_a_live_extension() -> None:
    with pytest.raises(ValueError, match="already registered"):

        @register_reader(".csv")
        class Impostor:
            def read(self, source: object) -> pl.LazyFrame:  # pragma: no cover
                raise NotImplementedError
