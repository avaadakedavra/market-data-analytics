"""`MarketDataService` — the one object the API and the dashboard both talk to.

Everything a caller can ask of this platform is a method here, and neither presentation
layer knows about ingestion, checks, cleansing or insights. That is what lets the
dashboard run in-process against the same code the HTTP API serves (PLAN §3.5), and it is
why an endpoint in `mdq.api` is a handful of lines: parse, delegate, paginate.

**Two questions this layer decides, both with measurements behind them.**

*What does startup cost?* Nothing but a directory listing. `load_directory` classifies
every file by reading its columns — 13 ms for the 116 MB corpus — and reads no rows at
all. The first request that needs daily bars pays 0.12 s; the first that needs minute bars
pays 1.1 s; nobody pays for a frequency they never ask about. Autoloading and checking
5.3M rows eagerly would make the first request wait several seconds for work most users
never want.

*What does `?view=` default to?* Raw. PLAN §4.4 proposed running analytics on the cleansed
view by default, and on daily data that would be free (0.07 s to check, 0.02 s to
cleanse). On the minute corpus it costs 4.9 s + 3.0 s, so a single-contract chart request
would drag the whole corpus through the quality engine before drawing anything. `?view=
clean` is one parameter away and is memoised after the first call; the default is the one
that keeps an interactive query interactive. `CleanseLog` travels with the clean view so
nothing is removed silently.

**What a dataset-wide question is asked of.** Uploads stay separate datasets so an upload
can be reported on by itself, but every dataset-wide endpoint reads `store.combined()`.
That matters for more than tidiness: `missing_session` can only tell a holiday from a hole
in the feed by comparing a contract against its siblings, so the frame the checks run over
has to span every contract loaded.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import BaseModel

from mdq.analytics import REGISTRY, Analytic, filter_bars, run_analytic
from mdq.domain.config import AppSettings
from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame, C
from mdq.ingest import IngestOptions, IngestResult, read_buffer
from mdq.ingest.readers import extension_of, supported_extensions
from mdq.insights import Insight, InsightEngine
from mdq.quality import CheckRegistry, QualityReport, UnknownCheckError
from mdq.service.store import Dataset, DatasetStore, Source, scan_file

__all__ = [
    "AUTOLOAD_ID",
    "MAX_REJECT_SAMPLE",
    "CheckSummary",
    "ContractInfo",
    "FindingFilter",
    "FrequencySummary",
    "LoadReport",
    "MarketDataService",
    "QualitySummary",
    "ServiceSummary",
    "SkippedFile",
    "UnknownContractError",
    "UploadReport",
    "UploadTooLargeError",
    "View",
    "check_title",
]

logger = logging.getLogger(__name__)

#: Dataset id given to the directory autoloaded at startup.
AUTOLOAD_ID = "autoload"

#: Rejects shown back to an uploader. Enough to see the shape of the problem; the whole
#: frame would be a denial-of-service on a file that is malformed from end to end.
MAX_REJECT_SAMPLE = 50


class UnknownContractError(LookupError):
    """Raised when a requested contract is not in the loaded data (404 at the API)."""


class UploadTooLargeError(ValueError):
    """Raised when an upload exceeds `AppSettings.max_upload_bytes` (413 at the API)."""


class View(StrEnum):
    """Which version of the bars a query runs against.

    `RAW` is everything that was ingested. `CLEAN` is the same frame with exact duplicates
    collapsed and the rows named by ERROR findings removed — `mdq.quality.cleanse`, whose
    log says exactly what went and why.
    """

    RAW = "raw"
    CLEAN = "clean"

    @property
    def is_clean(self) -> bool:
        """True for the cleansed view."""
        return self is View.CLEAN


# --------------------------------------------------------------------------- #
# what the service reports about itself
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkippedFile:
    """A file in a loaded directory that did not become bars, and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class LoadReport:
    """The outcome of loading a directory: what was taken, what was left, what broke."""

    dataset_id: str
    path: str
    loaded: tuple[str, ...] = ()
    skipped: tuple[SkippedFile, ...] = ()

    @property
    def file_count(self) -> int:
        """How many files became sources."""
        return len(self.loaded)


@dataclass(frozen=True)
class FrequencySummary:
    """What one frequency of the loaded data contains."""

    frequency: Frequency
    sources: int
    contracts: tuple[str, ...]
    exchanges: tuple[str, ...]
    row_count: int
    first_session: date | None
    last_session: date | None


@dataclass(frozen=True)
class ServiceSummary:
    """The dataset-level answer to "what have I got?"."""

    datasets: tuple[str, ...]
    frequencies: tuple[FrequencySummary, ...]
    contracts: tuple[str, ...]
    exchanges: tuple[str, ...]
    row_count: int


@dataclass(frozen=True)
class ContractInfo:
    """One instrument, and where it appears in the loaded data."""

    contract: str
    root: str | None
    exchange: str | None
    frequencies: tuple[Frequency, ...]
    first_session: date | None
    last_session: date | None
    bar_count: int


@dataclass(frozen=True)
class CheckSummary:
    """One check's contribution to a quality report, totalled over every contract."""

    check_id: str
    title: str
    findings: int
    bars_affected: int
    contracts: int
    findings_by_severity: Mapping[str, int]


@dataclass(frozen=True)
class QualitySummary:
    """Everything the Overview page renders, and the thresholds that produced it."""

    frequency: Frequency
    total_findings: int
    bars_affected: int
    findings_by_severity: Mapping[str, int]
    checks: tuple[CheckSummary, ...]
    by_contract: tuple[Mapping[str, Any], ...]
    checks_run: tuple[str, ...]
    thresholds: Mapping[str, Any]


@dataclass(frozen=True)
class UploadReport:
    """What one upload produced — the answer to "what happened to my file?".

    The scopes are deliberately different, and the API names them so in every field
    description. `stats`, `rejects`, `quality` and `findings` are about the uploaded file
    and nothing else: that is what the person who just uploaded it wants to see. `insights`
    are about **all** the loaded data with this file now part of it, because a pattern is
    by definition something that recurs — a rule needs a contract family, a run of
    sessions or a spread of reject reasons before it can say anything true, and a
    twenty-five-row CSV on its own can only ever produce silence.

    Attributes:
        dataset_id: Where the upload was stored; findings can be re-queried against it.
        frequency: The frequency the file was ingested at.
        stats: `IngestStats` for the file — rows in, rows out, rejects by reason.
        rejects: A bounded sample of the rows that could not be placed.
        quality: The checks run over this file alone.
        findings: This file's findings, in `FINDING_SCHEMA`.
        insights: Patterns across every loaded dataset, recomputed with this file in.
    """

    dataset_id: str
    frequency: Frequency
    stats: Mapping[str, Any]
    rejects: tuple[Mapping[str, Any], ...]
    quality: QualitySummary
    findings: pl.DataFrame
    insights: tuple[Insight, ...]


@dataclass(frozen=True)
class FindingFilter:
    """The `GET /quality/findings` query, as one object.

    Attributes:
        frequency: Which report to read; `None` means the service default.
        contracts: Restrict to these instruments. Unknown codes are a 404, not an empty
            answer, because a typo should not look like clean data.
        min_severity: Lowest severity to include, so `WARNING` returns warnings *and*
            errors. Findings are ranked, and a filter that ignored the ranking would make
            the user ask twice for the thing they actually care about.
        checks: Restrict to these check ids.
        start: Earliest `session_date`, inclusive.
        end: Latest `session_date`, inclusive. A date range excludes findings that name no
            session at all (a malformed record has no trading day to belong to).
    """

    frequency: Frequency | None = None
    contracts: tuple[str, ...] | None = None
    min_severity: Severity | None = None
    checks: tuple[str, ...] | None = None
    start: date | None = None
    end: date | None = None


# --------------------------------------------------------------------------- #
# the facade
# --------------------------------------------------------------------------- #


class MarketDataService:
    """Load market data, then answer questions about it.

    Attributes:
        settings: Deployment knobs (`MDQ_*`), including the autoload directory and the
            upload ceiling.
        store: The datasets in memory. Exposed so a test can inspect what was loaded.
    """

    def __init__(
        self,
        settings: AppSettings | None = None,
        store: DatasetStore | None = None,
        engine: InsightEngine | None = None,
    ) -> None:
        self.settings = settings or AppSettings()
        self.store = store or DatasetStore(self.settings.quality, engine=engine)

    # --- loading ----------------------------------------------------------- #

    def autoload(self) -> LoadReport | None:
        """Load `MDQ_DATA_DIR` if it exists; `None` if it does not.

        A missing directory is not an error. Starting the API before running
        `make fetch` should give an empty but perfectly functional service, not a stack
        trace — every endpoint answers 200 with nothing in it.
        """
        directory = self.settings.data_dir
        if not directory.exists():
            logger.info("MDQ_DATA_DIR %s does not exist; starting empty", directory)
            return None
        return self.load_directory(directory, dataset_id=AUTOLOAD_ID)

    def load_directory(self, path: str | Path, dataset_id: str | None = None) -> LoadReport:
        """Register every bar file under `path`, without reading a single row.

        Args:
            path: A directory (searched recursively) or a single file.
            dataset_id: Id to store the result under; defaults to the resolved path.

        Returns:
            A `LoadReport` naming the files that became sources and the ones that did
            not. Files are skipped rather than rejected when they are simply not market
            data — a vendor drop carries checksums and catalogues alongside its bars —
            and a file that fails to read is recorded rather than aborting the load, so
            one corrupt file never costs the user the other seventy-nine.

        Raises:
            FileNotFoundError: if `path` does not exist.
        """
        location = Path(path)
        if not location.exists():
            raise FileNotFoundError(f"no such data directory or file: {location}")

        sources: list[Source] = []
        skipped: list[SkippedFile] = []
        for candidate in _bar_file_candidates(location):
            try:
                source = scan_file(candidate)
            except Exception as exc:
                logger.warning("could not read %s: %s", candidate, exc)
                skipped.append(SkippedFile(str(candidate), f"{type(exc).__name__}: {exc}"))
                continue
            if source is None:
                skipped.append(SkippedFile(str(candidate), "no contract or timestamp column"))
                continue
            sources.append(source)

        identifier = dataset_id or str(location.resolve())
        self.store.put(identifier, sources)
        return LoadReport(
            dataset_id=identifier,
            path=str(location),
            loaded=tuple(source.name for source in sources),
            skipped=tuple(skipped),
        )

    def ingest_upload(self, name: str, data: bytes) -> UploadReport:
        """Ingest an uploaded file, keep it as its own dataset, and report on it.

        The report is deliberately the *whole* story — ingest statistics, a sample of the
        rejects, the quality summary, the findings and the insights — because an upload is
        the one moment a business user is looking straight at their own data and wants to
        know what is wrong with it.

        Args:
            name: The uploaded file's name. Its extension chooses the reader.
            data: The file's bytes.

        Raises:
            UploadTooLargeError: above `AppSettings.max_upload_bytes` (413).
            UnsupportedFormatError: if no reader claims the extension (400).
        """
        if len(data) > self.settings.max_upload_bytes:
            raise UploadTooLargeError(
                f"upload {name!r} is {len(data)} bytes; the limit is "
                f"{self.settings.max_upload_bytes} bytes"
            )
        # `read_buffer` picks the reader by extension and raises `UnsupportedFormatError`
        # — naming the formats that would have worked — before it parses a single byte.
        result = read_buffer(data, extension_of(name), IngestOptions(source=name))

        dataset = self.store.put(_upload_id(name, self.store.ids()), [Source.loaded(name, result)])
        frequency = result.bars.frequency
        return UploadReport(
            dataset_id=dataset.id,
            frequency=frequency,
            stats=result.stats.to_dict(),
            rejects=tuple(_reject_sample(result)),
            quality=_quality_summary(dataset, frequency),
            findings=dataset.report(frequency).findings,
            # Dataset-wide, and therefore recomputed: the upload changed the data these
            # patterns are drawn from, so a cached answer would now be a stale one.
            insights=tuple(self.insights(frequency)),
        )

    # --- what is loaded ---------------------------------------------------- #

    @property
    def data(self) -> Dataset:
        """The combined view of every loaded dataset — what queries run against."""
        return self.store.combined()

    def frequencies(self) -> tuple[Frequency, ...]:
        """The frequencies present in the loaded data."""
        return self.data.frequencies()

    def default_frequency(self) -> Frequency:
        """The frequency used when a request does not name one.

        Daily, whenever daily data is loaded. It is the frequency the vendor publishes
        diagnostics for, the one a desk acts on, and — at 30k rows against 5.3M — the one
        that answers in a tenth of a second. A minute-only dataset falls back to minute,
        so a request never silently asks about a frequency nobody loaded.
        """
        available = self.frequencies()
        # `frequencies()` happens to return DAILY first today, which would make the
        # membership test redundant. It is written out anyway: the preference for daily is
        # a decision, and it should not quietly become a different decision the day a
        # frequency is added ahead of DAILY in the enum.
        if not available or Frequency.DAILY in available:
            return Frequency.DAILY
        return available[0]

    def summary(self) -> ServiceSummary:
        """What has been loaded, per frequency and overall."""
        data = self.data
        per_frequency = tuple(
            FrequencySummary(
                frequency=frequency,
                sources=len(data.sources_for(frequency)),
                contracts=data.summary(frequency).contracts,
                exchanges=data.summary(frequency).exchanges,
                row_count=data.summary(frequency).row_count,
                first_session=data.summary(frequency).first_session,
                last_session=data.summary(frequency).last_session,
            )
            for frequency in data.frequencies()
        )
        return ServiceSummary(
            datasets=tuple(self.store.ids()),
            frequencies=per_frequency,
            contracts=_sorted_union(s.contracts for s in per_frequency),
            exchanges=_sorted_union(s.exchanges for s in per_frequency),
            row_count=sum(s.row_count for s in per_frequency),
        )

    def contracts(self, frequency: Frequency | None = None) -> list[ContractInfo]:
        """Every instrument in the loaded data, with its span and how many bars it has.

        Args:
            frequency: Restrict to one frequency; `None` covers every loaded frequency,
                merging a contract that appears in both into a single entry.
        """
        wanted = (frequency,) if frequency is not None else self.data.frequencies()
        merged: dict[str, ContractInfo] = {}
        for freq in wanted:
            for info in _contract_rows(self.data.bars(freq), freq):
                existing = merged.get(info.contract)
                merged[info.contract] = _merge_contracts(existing, info)
        return [merged[key] for key in sorted(merged)]

    def contract_codes(self, frequency: Frequency) -> tuple[str, ...]:
        """The contract codes present at one frequency."""
        return self.data.summary(frequency).contracts

    # --- bars and analytics ------------------------------------------------ #

    def bars(
        self,
        frequency: Frequency | None = None,
        contracts: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        view: View = View.RAW,
    ) -> BarFrame:
        """The bars matching a selection, still lazy so the caller can page cheaply.

        Raises:
            UnknownContractError: if a requested contract is not in the data (404).
            InvalidRangeError: if `start` is after `end` (422).
        """
        freq = frequency or self.default_frequency()
        self._require_contracts(freq, contracts)
        return filter_bars(self.data.view(freq, clean=view.is_clean), contracts, start, end)

    def run_analytic(
        self,
        name: str,
        params: BaseModel | Mapping[str, Any] | None = None,
        frequency: Frequency | None = None,
        contracts: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        view: View = View.RAW,
    ) -> pl.DataFrame:
        """Run one registered analytic over a selection of bars.

        Raises:
            UnknownAnalyticError: no analytic registered under `name` (404).
            UnknownContractError: unknown contract (404).
            FrequencyNotSupportedError: the analytic does not serve that frequency (422).
            InvalidRangeError: `start` is after `end` (422).
            pydantic.ValidationError: bad parameters (422).
        """
        analytic = REGISTRY.get(name)
        freq = frequency or self.frequency_for(analytic)
        return run_analytic(name, self.bars(freq, contracts, start, end, view), params)

    def frequency_for(self, analytic: Analytic) -> Frequency:
        """The frequency an analytic runs at when the request does not name one.

        An analytic that serves exactly one frequency answers for itself — asking for
        `rolling_vwap` without saying "minute" can only have meant minute bars. Anything
        else falls back to the service default.
        """
        if len(analytic.frequencies) == 1:
            return next(iter(analytic.frequencies))
        return self.default_frequency()

    # --- quality and insights ---------------------------------------------- #

    def quality_summary(self, frequency: Frequency | None = None) -> QualitySummary:
        """Findings totalled by check and by contract, with the thresholds in force."""
        return _quality_summary(self.data, frequency or self.default_frequency())

    def findings(self, criteria: FindingFilter | None = None) -> pl.DataFrame:
        """The findings matching `criteria`, worst severity first.

        Raises:
            UnknownContractError: if a requested contract is not in the data (404).
            UnknownCheckError: if a requested check id was never registered (404).
        """
        wanted = criteria or FindingFilter()
        freq = wanted.frequency or self.default_frequency()
        self._require_contracts(freq, wanted.contracts)
        for check_id in wanted.checks or ():
            CheckRegistry.get(check_id)

        report = self.data.report(freq).filter(
            check_id=wanted.checks,
            contract=wanted.contracts,
            min_severity=wanted.min_severity,
        )
        return _sorted_findings(_within_dates(report.findings, wanted.start, wanted.end))

    def insights(self, frequency: Frequency | None = None) -> list[Insight]:
        """The explained patterns in the data, strongest evidence first."""
        return self.data.insights(frequency or self.default_frequency())

    # --- internals --------------------------------------------------------- #

    def _require_contracts(self, frequency: Frequency, contracts: Iterable[str] | None) -> None:
        """Reject a contract the data does not hold, rather than answering with nothing.

        `filter_bars` deliberately returns an empty frame for an unknown contract, because
        it only sees the frame it was handed. Here the whole contract list is known, so a
        typo can be told from a genuinely empty selection — and must be.
        """
        if contracts is None:
            return
        known = set(self.contract_codes(frequency))
        unknown = [code for code in dict.fromkeys(contracts) if code not in known]
        if unknown:
            raise UnknownContractError(
                f"unknown contract(s) {unknown} at {frequency.value} frequency; "
                f"loaded contracts are {sorted(known)}"
            )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _bar_file_candidates(location: Path) -> list[Path]:
    """Files under `location` whose extension some reader claims, in a stable order."""
    if location.is_file():
        return [location]
    extensions = supported_extensions()
    return sorted(
        path for path in location.rglob("*") if path.is_file() and path.suffix.lower() in extensions
    )


def _upload_id(name: str, taken: Iterable[str]) -> str:
    """A dataset id for an upload, suffixed if that name is already loaded."""
    base = f"upload:{name}"
    existing = set(taken)
    if base not in existing:
        return base
    index = 2
    while f"{base}#{index}" in existing:
        index += 1
    return f"{base}#{index}"


def _reject_sample(result: IngestResult) -> list[Mapping[str, Any]]:
    """A bounded sample of an ingest's rejects, oldest row first."""
    if result.rejects.height == 0:
        return []
    ordered = result.rejects.sort(C.ROW_ID, nulls_last=True)
    return list(ordered.head(MAX_REJECT_SAMPLE).to_dicts())


def _sorted_union(groups: Iterable[Sequence[str]]) -> tuple[str, ...]:
    """The sorted distinct union of several already-sorted sequences."""
    return tuple(sorted({item for group in groups for item in group}))


def _within_dates(findings: pl.DataFrame, start: date | None, end: date | None) -> pl.DataFrame:
    """Restrict findings to a `session_date` range, dropping ones that name no session."""
    if start is None and end is None:
        return findings
    # Explicit rather than left to Polars, whose comparison against a null yields null and
    # therefore drops the row anyway: the exclusion is a documented promise to the caller,
    # not a side effect of how a predicate happens to treat missing values.
    frame = findings.filter(pl.col(C.SESSION_DATE).is_not_null())
    if start is not None:
        frame = frame.filter(pl.col(C.SESSION_DATE) >= start)
    if end is not None:
        frame = frame.filter(pl.col(C.SESSION_DATE) <= end)
    return frame


_SEVERITY_RANK: Mapping[str, int] = {s.label: int(s) for s in Severity}


def _severity_rank() -> pl.Expr:
    return pl.col(C.SEVERITY).replace_strict(_SEVERITY_RANK, default=0, return_dtype=pl.Int32)


def _sorted_findings(findings: pl.DataFrame) -> pl.DataFrame:
    """Worst severity first, then by contract and session — reading order for a human."""
    if findings.height == 0:
        return findings
    rank = "_severity_rank"
    return (
        findings.with_columns(_severity_rank().alias(rank))
        .sort(
            [rank, C.CONTRACT, C.SESSION_DATE, C.CHECK_ID],
            descending=[True, False, False, False],
            nulls_last=True,
        )
        .drop(rank)
    )


def _quality_summary(dataset: Dataset, frequency: Frequency) -> QualitySummary:
    """Total one report by check and by contract."""
    report = dataset.report(frequency)
    by_contract = report.summary()
    return QualitySummary(
        frequency=frequency,
        total_findings=len(report),
        bars_affected=_bars_affected(report),
        findings_by_severity=report.counts_by_severity(),
        checks=tuple(_check_summaries(by_contract)),
        by_contract=tuple(by_contract.to_dicts()),
        checks_run=report.checks_run,
        thresholds=report.thresholds,
    )


def _bars_affected(report: QualityReport) -> int:
    """How many bars the whole report implicates, counting a bar once per finding."""
    if report.is_empty():
        return 0
    return int(report.findings.get_column(C.COUNT).sum() or 0)


def _check_summaries(by_contract: pl.DataFrame) -> list[CheckSummary]:
    """Collapse the check x severity x contract grid to one row per check."""
    if by_contract.height == 0:
        return []
    totals = (
        by_contract.group_by(C.CHECK_ID)
        .agg(
            pl.col("findings").sum().alias("findings"),
            pl.col("rows").sum().alias("rows"),
            pl.col(C.CONTRACT).n_unique().alias("contracts"),
        )
        .sort("rows", C.CHECK_ID, descending=[True, False])
    )
    severities = {
        (row[C.CHECK_ID], row[C.SEVERITY]): int(row["findings"])
        for row in by_contract.group_by(C.CHECK_ID, C.SEVERITY)
        .agg(pl.col("findings").sum().alias("findings"))
        .to_dicts()
    }
    return [
        CheckSummary(
            check_id=str(row[C.CHECK_ID]),
            title=check_title(str(row[C.CHECK_ID])),
            findings=int(row["findings"]),
            bars_affected=int(row["rows"]),
            contracts=int(row["contracts"]),
            findings_by_severity={
                severity.label: severities[(row[C.CHECK_ID], severity.label)]
                for severity in sorted(Severity, reverse=True)
                if (row[C.CHECK_ID], severity.label) in severities
            },
        )
        for row in totals.to_dicts()
    ]


def check_title(check_id: str) -> str:
    """The check's own title, or the id when the finding did not come from a check.

    `check_failed` findings are produced by the runner rather than by a check, so their
    id is not in the registry; falling back to the id keeps the API answering instead of
    raising on the one finding that already means something went wrong.
    """
    try:
        return CheckRegistry.get(check_id).title
    except UnknownCheckError:
        return check_id


def _contract_rows(bars: BarFrame, frequency: Frequency) -> list[ContractInfo]:
    """One `ContractInfo` per contract in `bars`."""
    grouped = (
        bars.lf.group_by(C.CONTRACT)
        .agg(
            pl.col(C.ROOT).drop_nulls().first().alias(C.ROOT),
            pl.col(C.EXCHANGE).drop_nulls().first().alias(C.EXCHANGE),
            pl.col(C.SESSION_DATE).min().alias("first_session"),
            pl.col(C.SESSION_DATE).max().alias("last_session"),
            pl.len().alias("bar_count"),
        )
        .sort(C.CONTRACT)
        .collect()
    )
    return [
        ContractInfo(
            contract=str(row[C.CONTRACT]),
            root=row[C.ROOT],
            exchange=row[C.EXCHANGE],
            frequencies=(frequency,),
            first_session=row["first_session"],
            last_session=row["last_session"],
            bar_count=int(row["bar_count"]),
        )
        for row in grouped.to_dicts()
    ]


def _merge_contracts(existing: ContractInfo | None, addition: ContractInfo) -> ContractInfo:
    """Fold one contract's daily and minute entries into a single answer."""
    if existing is None:
        return addition
    return ContractInfo(
        contract=existing.contract,
        root=existing.root or addition.root,
        exchange=existing.exchange or addition.exchange,
        frequencies=tuple(
            f for f in Frequency if f in {*existing.frequencies, *addition.frequencies}
        ),
        first_session=min(
            (d for d in (existing.first_session, addition.first_session) if d is not None),
            default=None,
        ),
        last_session=max(
            (d for d in (existing.last_session, addition.last_session) if d is not None),
            default=None,
        ),
        bar_count=existing.bar_count + addition.bar_count,
    )
