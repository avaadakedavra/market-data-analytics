"""The store — deferred reading, row-id shifting, and caches that actually invalidate.

Three things here are load-bearing and would each fail silently if they broke:

* a file is classified without being read, so startup stays cheap;
* concatenated sources get disjoint `row_id` ranges, so `cleanse` cannot drop one file's
  bar because another file's reject happened to share its number;
* every derived artefact is computed once and thrown away on mutation, so a second upload
  never reports the first one's findings.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.ingest import UnsupportedFormatError, ingest_file
from mdq.service.store import (
    COMBINED_ID,
    Dataset,
    DatasetStore,
    Source,
    UnknownDatasetError,
    scan_file,
)
from support.fixtures import (
    CLG26_DAILY,
    ESH26_DAILY,
    ESH26_MINUTE,
    GENERIC_CLEAN,
    VENDOR_DIAGNOSTICS,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #


def test_scanning_a_vendor_file_learns_its_frequency_without_reading_it() -> None:
    """The whole startup budget rests on this: the layout declares the frequency."""
    source = scan_file(ESH26_MINUTE)
    assert source is not None
    assert source.declared_frequency is Frequency.MINUTE
    assert source.frequency is Frequency.MINUTE
    assert not source.is_loaded, "asking for the frequency must not have read the rows"


def test_scanning_a_layout_that_declares_nothing_reads_the_file_to_find_out() -> None:
    """A hand-made CSV has no declared frequency, so there is no cheaper way to know."""
    source = scan_file(GENERIC_CLEAN)
    assert source is not None
    assert source.declared_frequency is None
    assert source.frequency is Frequency.MINUTE
    assert source.is_loaded


def test_a_file_that_is_not_bar_data_is_not_a_source() -> None:
    """A vendor drop carries catalogues and checksums next to the bars."""
    assert scan_file(VENDOR_DIAGNOSTICS) is None


def test_an_unreadable_extension_is_reported_by_the_scanner() -> None:
    with pytest.raises(UnsupportedFormatError):
        scan_file(Path("prices.xlsx"))


def test_a_source_reads_its_file_exactly_once() -> None:
    calls: list[int] = []

    def read() -> object:
        calls.append(1)
        return ingest_file(ESH26_DAILY)

    source = Source("x", read)  # type: ignore[arg-type]
    assert source.result() is source.result()
    assert calls == [1]


def test_an_uploaded_source_is_already_loaded() -> None:
    result = ingest_file(ESH26_DAILY)
    source = Source.loaded("upload.parquet", result)
    assert source.is_loaded
    assert source.frequency is Frequency.DAILY
    assert source.result() is result


# --------------------------------------------------------------------------- #
# datasets
# --------------------------------------------------------------------------- #


def _dataset(*paths: Path) -> Dataset:
    sources = [s for s in (scan_file(p) for p in paths) if s is not None]
    return Dataset("test", tuple(sources))


def test_row_ids_of_concatenated_sources_never_overlap() -> None:
    """Two vendor files each number their rows from zero; a dataset must not."""
    dataset = _dataset(CLG26_DAILY, ESH26_DAILY)
    frame = dataset.bars(Frequency.DAILY).collect()
    assert frame.get_column(C.ROW_ID).n_unique() == frame.height
    per_contract = frame.group_by(C.CONTRACT).agg(
        pl.col(C.ROW_ID).min().alias("lo"), pl.col(C.ROW_ID).max().alias("hi")
    )
    spans = sorted((row["lo"], row["hi"]) for row in per_contract.to_dicts())
    assert spans[0][1] < spans[1][0], "the two files' id ranges must be disjoint"


_HEADER = "contract,exchange,timestamp,open,high,low,close,volume\n"


def _csv(path: Path, rows: int, *, contract: str, blank_contract_tail: int = 0) -> Path:
    """A tiny generic CSV; the last `blank_contract_tail` rows have no contract."""
    lines = [_HEADER]
    for minute in range(rows):
        code = "" if minute >= rows - blank_contract_tail else contract
        lines.append(f"{code},CME,2026-03-03 09:{minute:02d}:00,100.0,101.0,99.0,100.5,10\n")
    path.write_text("".join(lines))
    return path


def test_the_shift_keeps_rejects_in_the_same_numbering_space_as_the_bars(tmp_path: Path) -> None:
    """The stride is the number of *input* rows, not the number that survived.

    Rejects are numbered against the input rows, so a file whose last lines are rejects
    leaves ids above its surviving-row count. Striding by the survivors would hand those
    same ids to the next file's bars — and `cleanse`, which removes the rows an ERROR
    finding names, would then delete perfectly good data from a different file.
    """
    first = _csv(tmp_path / "a.csv", rows=6, contract="ESH26", blank_contract_tail=2)
    second = _csv(tmp_path / "b.csv", rows=4, contract="ESM26")
    dataset = _dataset(first, second)

    bars = dataset.bars(Frequency.MINUTE).collect()
    rejects = dataset.rejects(Frequency.MINUTE)
    reject_ids = {i for i in rejects.get_column(C.ROW_ID).to_list() if i is not None}
    assert reject_ids == {4, 5}, "the two blank-contract rows are the fifth and sixth lines"
    assert not (set(bars.get_column(C.ROW_ID).to_list()) & reject_ids)

    # The consequence, asserted rather than assumed: the second file keeps all its bars.
    clean, log = dataset.cleansed(Frequency.MINUTE)
    assert log.rows_dropped == 0
    assert clean.collect().height == bars.height == 8


def test_an_empty_frequency_still_has_the_canonical_schema() -> None:
    dataset = _dataset(ESH26_MINUTE)
    empty = dataset.bars(Frequency.DAILY)
    assert empty.is_empty()
    assert dataset.rejects(Frequency.DAILY).height == 0
    assert dataset.frequencies() == (Frequency.MINUTE,)


def test_derived_artefacts_are_computed_once() -> None:
    dataset = _dataset(CLG26_DAILY)
    assert dataset.report(Frequency.DAILY) is dataset.report(Frequency.DAILY)
    assert dataset.context(Frequency.DAILY) is dataset.context(Frequency.DAILY)
    assert dataset.cleansed(Frequency.DAILY) is dataset.cleansed(Frequency.DAILY)
    assert dataset.summary(Frequency.DAILY) is dataset.summary(Frequency.DAILY)
    assert dataset.insights(Frequency.DAILY) is dataset.insights(Frequency.DAILY)
    assert dataset.bars(Frequency.DAILY) is dataset.bars(Frequency.DAILY)
    assert dataset.activity(Frequency.DAILY) is dataset.context(Frequency.DAILY).profile


def test_adding_a_source_throws_the_cached_answers_away() -> None:
    dataset = _dataset(ESH26_DAILY)
    before = dataset.report(Frequency.DAILY)
    assert dataset.summary(Frequency.DAILY).contracts == ("ESH26",)

    extra = scan_file(CLG26_DAILY)
    assert extra is not None
    assert dataset.extend([extra]) is dataset

    after = dataset.report(Frequency.DAILY)
    assert after is not before
    assert dataset.summary(Frequency.DAILY).contracts == ("CLG26", "ESH26")


def test_extending_with_nothing_keeps_the_caches() -> None:
    dataset = _dataset(ESH26_DAILY)
    before = dataset.report(Frequency.DAILY)
    dataset.extend([])
    assert dataset.report(Frequency.DAILY) is before


def test_the_clean_view_removes_what_the_report_condemned_and_the_raw_view_does_not() -> None:
    dataset = _dataset(ESH26_MINUTE)
    raw = dataset.view(Frequency.MINUTE, clean=False)
    clean = dataset.view(Frequency.MINUTE, clean=True)
    assert raw is dataset.bars(Frequency.MINUTE)
    assert clean is dataset.cleansed(Frequency.MINUTE)[0]


def test_is_loaded_reports_whether_the_rows_have_been_read() -> None:
    dataset = _dataset(ESH26_MINUTE)
    assert not dataset.is_loaded(Frequency.MINUTE)
    assert not dataset.is_loaded(Frequency.DAILY), "a frequency with no sources is not loaded"
    dataset.bars(Frequency.MINUTE)
    assert dataset.is_loaded(Frequency.MINUTE)


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #


def test_a_single_dataset_is_its_own_combined_view() -> None:
    """The common case — one autoloaded directory — must not check the data twice."""
    store = DatasetStore()
    source = scan_file(ESH26_DAILY)
    assert source is not None
    dataset = store.put("only", [source])
    assert store.combined() is dataset
    assert store.combined() is store.combined()


def test_several_datasets_combine_into_one_view_that_shares_their_ingests() -> None:
    store = DatasetStore()
    daily = scan_file(ESH26_DAILY)
    other = scan_file(CLG26_DAILY)
    assert daily is not None and other is not None
    store.put("a", [daily])
    store.put("b", [other])

    combined = store.combined()
    assert combined.id == COMBINED_ID
    assert combined.summary(Frequency.DAILY).contracts == ("CLG26", "ESH26")
    # The member datasets' own ingests are reused, not repeated.
    assert store.get("a").ingests(Frequency.DAILY)[0] is daily.result()


def test_putting_a_dataset_invalidates_the_combined_view() -> None:
    store = DatasetStore()
    first = scan_file(ESH26_DAILY)
    second = scan_file(CLG26_DAILY)
    assert first is not None and second is not None
    store.put("a", [first])
    before = store.combined()
    store.put("b", [second])
    assert store.combined() is not before


def test_putting_the_same_id_twice_replaces_rather_than_merges() -> None:
    """Re-loading a directory must see the files as they are, not as they were plus."""
    store = DatasetStore()
    first = scan_file(ESH26_DAILY)
    second = scan_file(CLG26_DAILY)
    assert first is not None and second is not None
    store.put("dir", [first])
    store.put("dir", [second])
    assert len(store) == 1
    assert store.get("dir").summary(Frequency.DAILY).contracts == ("CLG26",)


def test_the_store_reports_what_it_holds() -> None:
    store = DatasetStore()
    source = scan_file(ESH26_DAILY)
    assert source is not None
    store.put("a", [source])
    assert store.ids() == ["a"]
    assert "a" in store
    assert "b" not in store
    assert [d.id for d in store] == ["a"]
    store.clear()
    assert len(store) == 0
    assert store.combined().frequencies() == ()


def test_asking_for_a_dataset_that_was_never_loaded_says_what_was() -> None:
    store = DatasetStore()
    with pytest.raises(UnknownDatasetError, match="loaded: \\[\\]"):
        store.get("nope")
