"""What has been loaded, and everything derived from it — held in memory, computed once.

This module exists to answer one question well: *how much work does the platform have to
do before it can answer a request?* The corpus is 5.3M minute rows across 40 contracts
(116 MB), so the honest answer has to be "as little as possible, and never twice".

Three decisions, all measured on the full `data/raw` corpus (5.3M minute rows, 30k daily
rows, 116 MB). The *property* the numbers depend on — that loading reads no rows — is what
the tests assert, in `tests/unit/test_service.py`; the timings themselves are recorded in
the README rather than turned into a flaky wall-clock assertion:

* **A `Source` classifies itself without reading any rows.** The vendor's layouts declare
  their frequency (`SourceProfile.frequency_hint`), so scanning a file's *columns* is
  enough to say "this is minute data" — 13 ms for all 80 files. A layout that declares
  nothing (a hand-made CSV) is read, because there is no other way to know.
* **Ingestion is deferred to first use, per frequency.** Loading the whole directory
  costs nothing; asking a daily question costs 0.12 s and 30k rows; asking a minute
  question costs 1.1 s and 5.3M rows. A user who only ever looks at daily bars never pays
  for the minute corpus.
* **Everything derived is memoised and invalidated together.** The quality report over
  the minute corpus takes 4.9 s and cleansing it another 3.0 s; the daily equivalents are
  0.07 s and 0.02 s. Those are one-time costs per frequency, not per request, which is
  what keeps an interactive dashboard interactive.

**Row ids.** `row_id` is a position within a *source file*, so concatenating forty files
would make it ambiguous. Each source's rows are therefore shifted by the running total of
input rows before the frames are concatenated, which keeps `row_id` unique across a
dataset and keeps bars and their rejects in one numbering space (a reject's id must never
collide with a surviving bar's). The shift is per *view*: ids from `store.combined()` and
ids from one upload's own `Dataset` are each internally consistent, and findings are
always read against the view that produced them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from mdq.domain.config import QualityConfig
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame, C
from mdq.ingest import IngestOptions, IngestResult, detect_profile, normalise
from mdq.ingest.readers import MALFORMED_LINE_COLUMN, READERS
from mdq.ingest.report import empty_reject_df
from mdq.insights import DatasetSummary, Insight, InsightEngine, RuleBasedInsightEngine
from mdq.quality import (
    DEFAULT_POLICY,
    ActivityProfile,
    CheckContext,
    CleanseLog,
    CleansePolicy,
    QualityReport,
    cleanse,
    run_checks,
)

__all__ = [
    "COMBINED_ID",
    "Dataset",
    "DatasetStore",
    "Source",
    "UnknownDatasetError",
    "scan_file",
]

logger = logging.getLogger(__name__)

#: Id of the view that spans every loaded dataset — what the API answers questions about.
COMBINED_ID = "all"


class UnknownDatasetError(KeyError):
    """Raised when a dataset id is requested that was never loaded."""


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #


@dataclass
class Source:
    """One ingestible input — a file on disk, or an upload held in memory.

    Attributes:
        name: Provenance, as it appears in `IngestStats.source` and in every reject.
        declared_frequency: The frequency the source's *layout* declares, known without
            reading a row. `None` means it can only be learned by ingesting.
    """

    name: str
    _read: Callable[[], IngestResult] = field(repr=False)
    declared_frequency: Frequency | None = None
    _result: IngestResult | None = field(default=None, init=False, repr=False)

    # --- construction ------------------------------------------------------ #

    @classmethod
    def loaded(cls, name: str, result: IngestResult) -> Source:
        """A source that has already been read — the upload path."""
        source = cls(name, lambda: result, result.bars.frequency)
        source._result = result
        return source

    # --- reading ----------------------------------------------------------- #

    @property
    def is_loaded(self) -> bool:
        """True once the rows have actually been read."""
        return self._result is not None

    @property
    def frequency(self) -> Frequency:
        """The source's frequency, reading the file only if its layout does not say."""
        return self.declared_frequency or self.result().bars.frequency

    def result(self) -> IngestResult:
        """Ingest the source, once. Subsequent calls return the same result."""
        if self._result is None:
            self._result = self._read()
        return self._result


def scan_file(path: str | Path) -> Source | None:
    """A deferred `Source` for `path`, or `None` when the file is not bar data.

    Reads the file's *columns* and nothing else. That is enough to pick a
    `SourceProfile`, and therefore enough both to reject the catalogue and checksum files
    that sit alongside the bars in a vendor drop — they resolve no contract and no
    timestamp — and to learn the frequency of the ones that are bars.

    Raises:
        UnsupportedFormatError: if no reader claims the file's extension.
    """
    location = Path(path)
    raw = READERS.for_path(location).read(location)
    columns = [name for name in raw.collect_schema().names() if name != MALFORMED_LINE_COLUMN]
    profile = detect_profile(columns)
    if profile.missing(columns):
        return None
    options = IngestOptions(source=str(location))
    return Source(str(location), lambda: normalise(raw, profile, options), profile.frequency_hint)


# --------------------------------------------------------------------------- #
# datasets
# --------------------------------------------------------------------------- #


@dataclass
class Dataset:
    """One load unit — a directory, or a single upload — and everything derived from it.

    A dataset is homogeneous in nothing but provenance: it may hold both frequencies, and
    every derived artefact is therefore keyed by frequency and computed independently.
    Each is produced on first request and kept until `invalidate()`, which every mutation
    calls.

    Attributes:
        id: Stable identifier, unique within a `DatasetStore`.
        sources: The inputs, in load order.
        config: Quality thresholds used for every check run over this dataset.
        policy: What `cleanse` is allowed to remove from the clean view.
        engine: The insight engine; swap it to swap the whole insights layer.
    """

    id: str
    sources: tuple[Source, ...] = ()
    config: QualityConfig = field(default_factory=QualityConfig)
    policy: CleansePolicy = DEFAULT_POLICY
    engine: InsightEngine = field(default_factory=RuleBasedInsightEngine)

    _bars: dict[Frequency, BarFrame] = field(default_factory=dict, init=False, repr=False)
    _rejects: dict[Frequency, pl.DataFrame] = field(default_factory=dict, init=False, repr=False)
    _context: dict[Frequency, CheckContext] = field(default_factory=dict, init=False, repr=False)
    _report: dict[Frequency, QualityReport] = field(default_factory=dict, init=False, repr=False)
    _clean: dict[Frequency, tuple[BarFrame, CleanseLog]] = field(
        default_factory=dict, init=False, repr=False
    )
    _insights: dict[Frequency, list[Insight]] = field(default_factory=dict, init=False, repr=False)
    _summary: dict[Frequency, DatasetSummary] = field(default_factory=dict, init=False, repr=False)

    # --- mutation ---------------------------------------------------------- #

    def extend(self, sources: Sequence[Source]) -> Dataset:
        """Add sources and drop every cached artefact. Returns self, for chaining."""
        if sources:
            self.sources = (*self.sources, *sources)
            self.invalidate()
        return self

    def invalidate(self) -> None:
        """Forget everything derived. The sources themselves keep their ingested rows."""
        for cache in (
            self._bars,
            self._rejects,
            self._context,
            self._report,
            self._clean,
            self._insights,
            self._summary,
        ):
            cache.clear()

    # --- shape ------------------------------------------------------------- #

    def frequencies(self) -> tuple[Frequency, ...]:
        """The frequencies this dataset holds, in `Frequency` declaration order.

        Sources whose layout declares a frequency answer for free; one that does not is
        ingested here, because there is no cheaper way to find out what it is.
        """
        present = {source.frequency for source in self.sources}
        return tuple(frequency for frequency in Frequency if frequency in present)

    def sources_for(self, frequency: Frequency) -> tuple[Source, ...]:
        """The sources carrying `frequency`, in load order."""
        return tuple(source for source in self.sources if source.frequency is frequency)

    def is_loaded(self, frequency: Frequency) -> bool:
        """True when every source of `frequency` has actually been read."""
        sources = self.sources_for(frequency)
        return bool(sources) and all(source.is_loaded for source in sources)

    # --- the data ---------------------------------------------------------- #

    def ingests(self, frequency: Frequency) -> tuple[IngestResult, ...]:
        """Ingest every source of `frequency` (once) and return the results in order."""
        return tuple(source.result() for source in self.sources_for(frequency))

    def bars(self, frequency: Frequency) -> BarFrame:
        """Every bar of `frequency`, as one frame with dataset-unique `row_id`s."""
        cached = self._bars.get(frequency)
        if cached is None:
            cached = self._combine(frequency)
            self._bars[frequency] = cached
        return cached

    def rejects(self, frequency: Frequency) -> pl.DataFrame:
        """Every row of `frequency` ingestion could not place, with the same `row_id`s."""
        cached = self._rejects.get(frequency)
        if cached is None:
            frames = [
                result.rejects.with_columns(_shift_row_id(offset))
                for result, offset in _with_offsets(self.ingests(frequency))
            ]
            cached = pl.concat(frames, how="vertical") if frames else empty_reject_df()
            self._rejects[frequency] = cached
        return cached

    def _combine(self, frequency: Frequency) -> BarFrame:
        """Concatenate the frequency's frames, shifting each source's `row_id`s clear."""
        results = self.ingests(frequency)
        if not results:
            return BarFrame.empty(frequency, self.id)
        frames = [
            result.bars.lf.with_columns(_shift_row_id(offset))
            for result, offset in _with_offsets(results)
        ]
        return BarFrame(pl.concat(frames, how="vertical"), frequency, self.id)

    # --- the analysis ------------------------------------------------------ #

    def context(self, frequency: Frequency) -> CheckContext:
        """The check context: thresholds, the activity profile, and the rejects.

        `sibling_contracts` is left to the check, which derives it from the frame it is
        given — and that frame already spans every contract loaded, which is exactly the
        corroboration `missing_session` needs to tell a holiday from a hole.
        """
        cached = self._context.get(frequency)
        if cached is None:
            cached = CheckContext.build(
                self.bars(frequency),
                self.config,
                rejects=self.rejects(frequency),
            )
            self._context[frequency] = cached
        return cached

    def activity(self, frequency: Frequency) -> ActivityProfile:
        """The activity regime for every `(contract, session_date)` of `frequency`."""
        return self.context(frequency).profile

    def report(self, frequency: Frequency) -> QualityReport:
        """Every applicable check, run over `frequency`. Computed once."""
        cached = self._report.get(frequency)
        if cached is None:
            cached = run_checks(self.bars(frequency), self.context(frequency))
            self._report[frequency] = cached
        return cached

    def cleansed(self, frequency: Frequency) -> tuple[BarFrame, CleanseLog]:
        """The bars with the report's ERROR rows and exact duplicates removed."""
        cached = self._clean.get(frequency)
        if cached is None:
            cached = cleanse(self.bars(frequency), self.report(frequency), self.policy)
            self._clean[frequency] = cached
        return cached

    def view(self, frequency: Frequency, *, clean: bool) -> BarFrame:
        """The raw bars, or the cleansed ones — the choice behind `?view=`."""
        return self.cleansed(frequency)[0] if clean else self.bars(frequency)

    def summary(self, frequency: Frequency) -> DatasetSummary:
        """Which contracts and exchanges `frequency` holds, over what span."""
        cached = self._summary.get(frequency)
        if cached is None:
            cached = DatasetSummary.from_bars(self.bars(frequency))
            self._summary[frequency] = cached
        return cached

    def insights(self, frequency: Frequency) -> list[Insight]:
        """The explained patterns in `frequency`'s report, strongest evidence first."""
        cached = self._insights.get(frequency)
        if cached is None:
            cached = self.engine.derive(
                self.report(frequency),
                self.activity(frequency),
                self.summary(frequency),
            )
            self._insights[frequency] = cached
        return cached


def _shift_row_id(offset: int) -> pl.Expr:
    """Move a source's `row_id`s clear of every source loaded before it."""
    return (pl.col(C.ROW_ID) + offset).cast(pl.UInt32).alias(C.ROW_ID)


def _with_offsets(results: Sequence[IngestResult]) -> Iterator[tuple[IngestResult, int]]:
    """Pair each result with the shift that makes its row ids unique in the dataset.

    The stride is `rows_in`, not `rows_out`: bars and rejects are numbered against the
    same input rows, so shifting by the number of rows that *survived* would let one
    file's reject id collide with the next file's bar id — and `cleanse` would then drop
    a perfectly good bar.
    """
    offset = 0
    for result in results:
        yield result, offset
        offset += result.stats.rows_in


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #


class DatasetStore:
    """Every loaded dataset, plus the combined view the API answers questions about.

    Datasets are kept separate so an upload can be reported on by itself — "what did *my*
    file look like?" is the question a business user asks after uploading — while
    `combined()` is what every dataset-wide endpoint reads, because a check like
    `missing_session` is only as good as the contracts it can compare against.
    """

    def __init__(
        self,
        config: QualityConfig | None = None,
        policy: CleansePolicy = DEFAULT_POLICY,
        engine: InsightEngine | None = None,
    ) -> None:
        self._datasets: dict[str, Dataset] = {}
        self._combined: Dataset | None = None
        self.config = config or QualityConfig()
        self.policy = policy
        self.engine = engine or RuleBasedInsightEngine()

    # --- contents ---------------------------------------------------------- #

    def __len__(self) -> int:
        return len(self._datasets)

    def __contains__(self, dataset_id: str) -> bool:
        return dataset_id in self._datasets

    def __iter__(self) -> Iterator[Dataset]:
        return iter(self._datasets.values())

    def ids(self) -> list[str]:
        """Dataset ids, in load order."""
        return list(self._datasets)

    def get(self, dataset_id: str) -> Dataset:
        """One dataset by id.

        Raises:
            UnknownDatasetError: if nothing was loaded under `dataset_id`.
        """
        try:
            return self._datasets[dataset_id]
        except KeyError:
            raise UnknownDatasetError(
                f"unknown dataset {dataset_id!r}; loaded: {list(self._datasets)}"
            ) from None

    # --- mutation ---------------------------------------------------------- #

    def put(self, dataset_id: str, sources: Sequence[Source]) -> Dataset:
        """Create or replace the dataset under `dataset_id` and return it.

        Replacing rather than merging is what makes re-loading a directory idempotent:
        the second load sees the files as they are now, not as they were plus as they are.
        """
        dataset = Dataset(dataset_id, tuple(sources), self.config, self.policy, self.engine)
        self._datasets[dataset_id] = dataset
        self._combined = None
        return dataset

    def clear(self) -> None:
        """Forget everything. The next `combined()` is an empty dataset."""
        self._datasets.clear()
        self._combined = None

    # --- the view everything else reads ------------------------------------ #

    def combined(self) -> Dataset:
        """One dataset spanning every loaded source. Memoised until the store changes.

        With a single dataset loaded this *is* that dataset, so the common case — an
        autoloaded directory and nothing else — never computes a report twice.
        """
        if self._combined is None:
            if len(self._datasets) == 1:
                self._combined = next(iter(self._datasets.values()))
            else:
                sources = tuple(s for dataset in self._datasets.values() for s in dataset.sources)
                self._combined = Dataset(
                    COMBINED_ID, sources, self.config, self.policy, self.engine
                )
        return self._combined
