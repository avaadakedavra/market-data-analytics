"""The `Backend` protocol and its two implementations — where every dashboard decision lives.

Streamlit pages are the worst place in this codebase to put logic. `AppTest` can only
assert on rendered widgets, a page cannot be called from a unit test without a script
runner, and a rerun re-executes the whole file top to bottom. So the pages in
`mdq.dashboard.views` are deliberately thin: they pick values out of widgets, hand them to
a `Backend`, and draw what comes back. Everything that could be *wrong* — which severity a
findings table defaults to, how a regime profile becomes a heatmap, how a JSON instant
becomes a Polars timestamp — is here, and is tested directly.

**Two implementations, one vocabulary.** `HttpBackend` talks to the FastAPI app over
httpx; `EmbeddedBackend` calls `MarketDataService` in the same process. They are the same
object to a page, and they must never drift, so both answer in the API's *own* pydantic
response models (`DatasetsSummaryResponse`, `FindingsResponse`, `IngestResponse`, …).
`EmbeddedBackend` builds them with the same `of()` classmethods the routes use and
`HttpBackend` validates the JSON into them, which means a field renamed in `mdq.api.schemas`
breaks both backends at once rather than one of them silently. `tests/integration`
runs the same assertions over both.

**JSON cannot carry a dtype, so `conform` puts them back.** Over HTTP a timestamp arrives
as `"2026-03-02T23:00:00Z"` and a session as `"2026-03-02"`; in process they are a
`datetime` and a `date`. Every row-returning method funnels through `conform(rows, schema)`
so a chart receives the same typed frame either way — and so a test can compare the two
frames for equality rather than hoping.

**The severity default is a product decision, not a UI default.** Measured on the full
corpus: 5 ERROR, 39 WARNING and 14,974 INFO daily findings, and 14,899 minute
`stale_bar` findings covering 2,559,602 bars, of which *none* are above INFO. A findings
table that opens on "everything" hands a risk manager two and a half million correct
settlement prints and
buries the five real errors. `DEFAULT_MIN_SEVERITY` is therefore `WARNING`, INFO is opt-in,
and `SeverityTriage` exists so the page can always say how many findings are being held
back and why. The regime model already did this triage; the dashboard's job is to show it,
not to undo it.

**What is expensive, and what the pages do about it.** Measured: `GET /bars` daily p95
11 ms, minute p95 32 ms, `rolling_vwap` p95 30 ms; `GET /quality/summary` daily 72 ms cold
and 3 ms warm, but **minute 5.2 s cold**; `GET /datasets/summary` 1.6 s cold. Peak RSS
reaches 1.3 GB once minute bars materialise. Every landing view therefore asks for daily,
the minute quality pass is never triggered without a spinner that says what is happening,
and `mdq.dashboard.cache` memoises the answers per session.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

import httpx
import polars as pl
from pydantic import BaseModel, ValidationError

from mdq.analytics import (
    FrequencyNotSupportedError,
    InvalidRangeError,
    UnknownAnalyticError,
)
from mdq.analytics.daily_bars import BAR_COUNT, DAILY_BARS_SCHEMA
from mdq.analytics.vwap import BARS_IN_WINDOW, VWAP, VWAP_SCHEMA, WINDOW_VOLUME
from mdq.api.deps import ROW_LIMIT_MAX
from mdq.api.routes_quality import FindingQuery
from mdq.api.schemas import (
    ContractOut,
    ContractsResponse,
    DatasetsSummaryResponse,
    FindingOut,
    FindingsResponse,
    IngestResponse,
    InsightOut,
    InsightsResponse,
    QualitySummaryResponse,
    finding_from_row,
    page_rows,
)
from mdq.domain.config import AppSettings, QualityConfig
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, BarFrame, C
from mdq.ingest import UnsupportedFormatError
from mdq.quality import UnknownCheckError, activity_profile
from mdq.quality.context import ACTIVITY_SCHEMA, A, Regime
from mdq.service import (
    MarketDataService,
    UnknownContractError,
    UnknownDatasetError,
    UploadTooLargeError,
    View,
    check_title,
)

__all__ = [
    "ACTIVITY_BAR_CAP",
    "CHECK_SCHEMA",
    "DEFAULT_API_URL",
    "DEFAULT_MIN_SEVERITY",
    "FINDINGS_PAGE",
    "GAP_CHECKS",
    "GAP_SCHEMA",
    "REGIME_LABELS",
    "SEVERITY_ORDER",
    "Backend",
    "BackendError",
    "EmbeddedBackend",
    "FindingQuery",
    "HeatmapData",
    "HttpBackend",
    "SeverityTriage",
    "build_backend",
    "candles",
    "check_totals",
    "conform",
    "findings_table",
    "gap_segments",
    "heatmap_data",
    "reconciliation",
    "session_coverage",
    "session_vwap",
    "severity_triage",
    "to_yaml",
]

#: Where `HttpBackend` looks for the API when nothing says otherwise.
DEFAULT_API_URL: Final = "http://127.0.0.1:8000"

#: The findings view opens here. See the module docstring: on the real corpus "everything"
#: means 2.5M expected settlement prints on top of 5 genuine errors.
DEFAULT_MIN_SEVERITY: Final = "WARNING"

#: Findings fetched for one table. Large enough that a filter rarely truncates, small
#: enough that a mis-set filter cannot pull a million rows into a browser.
FINDINGS_PAGE: Final = 2_000

#: Above this many bars, `HttpBackend` refuses to rebuild an activity profile over the
#: wire. The daily corpus is 30k rows; the minute corpus is 5.3M and would be a several-
#: hundred-megabyte JSON download to recompute something the server already knows.
ACTIVITY_BAR_CAP: Final = 250_000

#: Worst first — the order severities are listed, coloured and sorted in.
SEVERITY_ORDER: Final[tuple[str, ...]] = ("ERROR", "WARNING", "INFO")

#: `DORMANT`, `THIN` and `ACTIVE` are our words. These are the reader's.
REGIME_LABELS: Final[Mapping[str, str]] = {
    Regime.ACTIVE.value: "Normal trading",
    Regime.THIN.value: "Light trading",
    Regime.DORMANT.value: "Nothing traded",
}

#: The checks that describe a *hole* in the data rather than a bad value in it. These are
#: what the gap timeline draws; everything else has no duration to draw.
GAP_CHECKS: Final[tuple[str, ...]] = ("intrabar_gap", "missing_session")

#: One row per check for the Overview bar chart.
CHECK_SCHEMA: Final[pl.Schema] = pl.Schema(
    [
        ("check", pl.String),
        ("severity", pl.String),
        ("findings", pl.Int64),
        ("bars_affected", pl.Int64),
    ]
)

#: One row per hole in the data, for the gap timeline strip. `from_utc`/`to_utc` are always
#: populated so the strip can be drawn without a branch; `whole_session` says whether they
#: were *observed* (a gap between two real bars) or *implied* (a session with no bars at
#: all, which has no instants of its own to report).
GAP_SCHEMA: Final[pl.Schema] = pl.Schema(
    [
        (C.CONTRACT, pl.String),
        (C.SESSION_DATE, pl.Date),
        ("from_utc", pl.Datetime("us", "UTC")),
        ("to_utc", pl.Datetime("us", "UTC")),
        ("minutes_missing", pl.Int64),
        ("whole_session", pl.Boolean),
        ("severity", pl.String),
        ("what", pl.String),
    ]
)

#: How `HttpBackend` restores the dtypes an analytic's JSON has lost. An analytic this
#: table does not know is returned with whatever dtypes Polars infers, which is still a
#: usable frame — the entry is an improvement, not a requirement.
_ANALYTIC_SCHEMAS: Final[Mapping[str, pl.Schema]] = {
    "daily_bars": DAILY_BARS_SCHEMA,
    "rolling_vwap": VWAP_SCHEMA,
}

#: Every deliberate failure the layers below raise, mapped to one dashboard error so a
#: page has exactly one thing to catch.
_DOMAIN_ERRORS: Final[tuple[type[Exception], ...]] = (
    FrequencyNotSupportedError,
    InvalidRangeError,
    UnknownAnalyticError,
    UnknownCheckError,
    UnknownContractError,
    UnknownDatasetError,
    UnsupportedFormatError,
    UploadTooLargeError,
    ValidationError,
)


class BackendError(RuntimeError):
    """Something the dashboard asked for could not be answered.

    Pages catch this and render the message as an empty state. It carries the API's own
    `detail` string when the answer came over HTTP and the exception's own message when it
    came from the service, so the sentence a user reads is the same either way.
    """


@contextmanager
def _as_backend_error(what: str) -> Iterator[None]:
    """Turn any deliberate failure below into a `BackendError` naming what was asked."""
    try:
        yield
    except _DOMAIN_ERRORS as exc:
        message = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
        raise BackendError(f"{what}: {message}") from exc


# --------------------------------------------------------------------------- #
# the protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class Backend(Protocol):
    """Everything a dashboard page is allowed to ask for.

    The return types are the API's published response models on purpose: they are already
    written in a business user's vocabulary (`bars_affected`, not `count`), they are
    already validated, and using them means the in-process backend cannot answer in a
    shape the HTTP one would not.
    """

    #: One line naming where the data is coming from, shown in the sidebar.
    label: str

    def summary(self) -> DatasetsSummaryResponse:
        """What is loaded: contracts, exchanges, bar counts and span per frequency."""
        ...

    def contracts(self, frequency: Frequency | None = None) -> list[ContractOut]:
        """Every instrument that can be charted, with its span and bar count."""
        ...

    def bars(
        self,
        frequency: Frequency,
        contracts: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        view: View = View.RAW,
        limit: int = ROW_LIMIT_MAX,
    ) -> pl.DataFrame:
        """Bars for a selection, conforming to `BAR_SCHEMA`."""
        ...

    def analytic(
        self,
        name: str,
        frequency: Frequency,
        contracts: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        params: Mapping[str, Any] | None = None,
        limit: int = ROW_LIMIT_MAX,
    ) -> pl.DataFrame:
        """One analytic's output over a selection of bars."""
        ...

    def quality_summary(self, frequency: Frequency | None = None) -> QualitySummaryResponse:
        """Findings totalled by check and instrument, with the thresholds in force."""
        ...

    def findings(self, query: FindingQuery) -> FindingsResponse:
        """One page of findings matching a filter, worst severity first."""
        ...

    def insights(self, frequency: Frequency | None = None) -> InsightsResponse:
        """The explained patterns in the data, strongest evidence first."""
        ...

    def activity(self, frequency: Frequency = Frequency.DAILY) -> pl.DataFrame:
        """How busy every `(contract, session)` was, in `ACTIVITY_SCHEMA`."""
        ...

    def upload(self, name: str, data: bytes) -> IngestResponse:
        """Ingest an uploaded file and report everything known about it."""
        ...


# --------------------------------------------------------------------------- #
# in-process
# --------------------------------------------------------------------------- #


class EmbeddedBackend:
    """Answers from a `MarketDataService` in this process — no API, no network.

    This is what `make ui` runs and what `AppTest` drives: one process, no port to
    coordinate, and a stack trace that points at the real cause instead of at a 500. It
    deliberately mirrors the route handlers line for line — same models, same paging, same
    `check_title` lookup — because any shortcut taken here would be a difference between
    what a test exercises and what a user sees.
    """

    def __init__(self, service: MarketDataService, label: str | None = None) -> None:
        self._service = service
        self.label = label or "in-process (no API)"

    @classmethod
    def autoloaded(cls, data_dir: str | Path | None = None) -> EmbeddedBackend:
        """A backend over `MDQ_DATA_DIR` (or `data_dir`), already loaded.

        Loading reads file *columns* only — 13 ms for the 116 MB corpus — so this is
        cheap even though it looks like it should not be.
        """
        settings = AppSettings(data_dir=Path(data_dir)) if data_dir else AppSettings()
        service = MarketDataService(settings)
        service.autoload()
        return cls(service, label=f"in-process, reading {settings.data_dir}")

    # --- inventory --------------------------------------------------------- #

    def summary(self) -> DatasetsSummaryResponse:
        """What is loaded, per frequency and overall."""
        with _as_backend_error("could not summarise the loaded data"):
            return DatasetsSummaryResponse.of(self._service.summary())

    def contracts(self, frequency: Frequency | None = None) -> list[ContractOut]:
        """Every instrument in the loaded data."""
        with _as_backend_error("could not list the instruments"):
            return [ContractOut.of(info) for info in self._service.contracts(frequency)]

    # --- bars and analytics ------------------------------------------------ #

    def bars(
        self,
        frequency: Frequency,
        contracts: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        view: View = View.RAW,
        limit: int = ROW_LIMIT_MAX,
    ) -> pl.DataFrame:
        """Bars for a selection, capped at `limit` rows."""
        with _as_backend_error("could not read the bars"):
            frame = self._service.bars(frequency, contracts, start, end, view)
            return frame.lf.head(limit).collect()

    def analytic(
        self,
        name: str,
        frequency: Frequency,
        contracts: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        params: Mapping[str, Any] | None = None,
        limit: int = ROW_LIMIT_MAX,
    ) -> pl.DataFrame:
        """Run one analytic over a selection of bars."""
        with _as_backend_error(f"could not run {name}"):
            frame = self._service.run_analytic(
                name, params, frequency=frequency, contracts=contracts, start=start, end=end
            )
            return frame.head(limit)

    # --- quality and insights ---------------------------------------------- #

    def quality_summary(self, frequency: Frequency | None = None) -> QualitySummaryResponse:
        """The whole quality picture for one frequency."""
        with _as_backend_error("could not run the quality checks"):
            return QualitySummaryResponse.of(self._service.quality_summary(frequency))

    def findings(self, query: FindingQuery) -> FindingsResponse:
        """One page of findings matching `query`."""
        with _as_backend_error("could not read the findings"):
            frequency = query.frequency or self._service.default_frequency()
            frame = self._service.findings(query.to_filter())
            rows, next_offset = page_rows(frame, query.limit, query.offset)
            return FindingsResponse(
                frequency=frequency,
                findings=[finding_from_row(r, check_title(r[C.CHECK_ID])) for r in rows],
                row_count=len(rows),
                limit=query.limit,
                offset=query.offset,
                next_offset=next_offset,
            )

    def insights(self, frequency: Frequency | None = None) -> InsightsResponse:
        """Every pattern found, strongest evidence first."""
        with _as_backend_error("could not derive the insights"):
            frequency = frequency or self._service.default_frequency()
            found = self._service.insights(frequency)
            return InsightsResponse(
                frequency=frequency,
                insight_count=len(found),
                insights=[InsightOut.of(insight) for insight in found],
            )

    def activity(self, frequency: Frequency = Frequency.DAILY) -> pl.DataFrame:
        """The regime classification the checks used, straight from the store."""
        with _as_backend_error("could not read the activity profile"):
            return self._service.data.activity(frequency).collect()

    # --- ingestion --------------------------------------------------------- #

    def upload(self, name: str, data: bytes) -> IngestResponse:
        """Ingest an uploaded file and report on it."""
        with _as_backend_error(f"could not ingest {name}"):
            report = self._service.ingest_upload(name, data)
            findings = [
                finding_from_row(row, check_title(row[C.CHECK_ID]))
                for row in report.findings.to_dicts()
            ]
            return IngestResponse.of(report, findings)


# --------------------------------------------------------------------------- #
# over HTTP
# --------------------------------------------------------------------------- #


class HttpBackend:
    """Answers from the FastAPI app over httpx — the default, and the honest one.

    It is the default because it is what a real deployment looks like: the dashboard is a
    client of the same documented API anyone else would use, so anything the dashboard can
    show, a script or a spreadsheet can fetch too. Its cost is that JSON has no dtypes and
    no arbitrary precision, which is what `conform` and `_ANALYTIC_SCHEMAS` exist to
    repair, and that a frame arrives a page at a time.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_API_URL,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # A long timeout, deliberately: the first minute-frequency request pays a 5.2 s
        # quality pass and a 60-second ceiling is the difference between a slow page and a
        # page that fails for a user who chose minute bars on purpose.
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)
        self.label = f"API at {self.base_url}"

    # --- transport --------------------------------------------------------- #

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One GET, with every failure turned into a sentence a user can act on."""
        try:
            response = self._client.get(path, params=dict(params or {}))
        except httpx.HTTPError as exc:
            raise BackendError(
                f"could not reach the API at {self.base_url} ({exc}). "
                f"Start it with `make api`, or run the dashboard with `make ui`."
            ) from exc
        return _decode(response, path)

    def _pages(
        self, path: str, params: Mapping[str, Any], key: str, limit: int
    ) -> list[dict[str, Any]]:
        """Follow `next_offset` until `limit` rows are in hand or the API runs out.

        Paging is the API's contract (`next_offset` is null exactly when there is no more),
        so this loop is the whole of it: no total is ever counted, and a frame far larger
        than one page still arrives.
        """
        collected: list[dict[str, Any]] = []
        offset = int(params.get("offset", 0) or 0)
        while len(collected) < limit:
            page = min(ROW_LIMIT_MAX, limit - len(collected))
            body = self._get(path, {**params, "limit": page, "offset": offset})
            collected.extend(body[key])
            next_offset = body.get("next_offset")
            if next_offset is None:
                break
            offset = int(next_offset)
        return collected

    # --- inventory --------------------------------------------------------- #

    def summary(self) -> DatasetsSummaryResponse:
        """What is loaded, per frequency and overall."""
        return DatasetsSummaryResponse.model_validate(self._get("/datasets/summary"))

    def contracts(self, frequency: Frequency | None = None) -> list[ContractOut]:
        """Every instrument in the loaded data."""
        body = self._get("/contracts", _selection(frequency=frequency))
        return list(ContractsResponse.model_validate(body).contracts)

    # --- bars and analytics ------------------------------------------------ #

    def bars(
        self,
        frequency: Frequency,
        contracts: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        view: View = View.RAW,
        limit: int = ROW_LIMIT_MAX,
    ) -> pl.DataFrame:
        """Bars for a selection, paged until `limit` rows are in hand."""
        params = _selection(frequency, contracts, start, end, view)
        return conform(self._pages("/bars", params, "rows", limit), BAR_SCHEMA)

    def analytic(
        self,
        name: str,
        frequency: Frequency,
        contracts: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        params: Mapping[str, Any] | None = None,
        limit: int = ROW_LIMIT_MAX,
    ) -> pl.DataFrame:
        """Run one analytic over a selection of bars."""
        query = {**_selection(frequency, contracts, start, end), **_scalar(params or {})}
        rows = self._pages(f"/analytics/{name}", query, "rows", limit)
        schema = _ANALYTIC_SCHEMAS.get(name)
        return conform(rows, schema) if schema is not None else pl.DataFrame(rows)

    # --- quality and insights ---------------------------------------------- #

    def quality_summary(self, frequency: Frequency | None = None) -> QualitySummaryResponse:
        """The whole quality picture for one frequency."""
        body = self._get("/quality/summary", _selection(frequency=frequency))
        return QualitySummaryResponse.model_validate(body)

    def findings(self, query: FindingQuery) -> FindingsResponse:
        """One page of findings matching `query`."""
        body = self._get("/quality/findings", _scalar(query.model_dump(exclude_none=True)))
        return FindingsResponse.model_validate(body)

    def insights(self, frequency: Frequency | None = None) -> InsightsResponse:
        """Every pattern found, strongest evidence first."""
        body = self._get("/insights", _selection(frequency=frequency))
        return InsightsResponse.model_validate(body)

    def activity(self, frequency: Frequency = Frequency.DAILY) -> pl.DataFrame:
        """Rebuild the regime classification locally, from the bars and the thresholds.

        The API publishes no activity endpoint, so this fetches the bars and re-runs
        `mdq.quality.context.activity_profile` over them — using the very thresholds the
        server reports in `/quality/summary`, not a local default, so the classification a
        user sees in the heatmap is bit-for-bit the one the checks used to decide
        severities. Above `ACTIVITY_BAR_CAP` bars it refuses instead: the minute corpus is
        5.3M rows, and downloading it to recompute something the server already knows would
        be a minutes-long page load.

        Raises:
            BackendError: when the frequency holds more bars than the cap allows.
        """
        bars = self._bar_count(frequency)
        if bars > ACTIVITY_BAR_CAP:
            raise BackendError(
                f"the activity map is built from {frequency.value} bars, and this dataset "
                f"holds {bars:,} of them — more than the {ACTIVITY_BAR_CAP:,} this view "
                f"fetches over HTTP. Daily bars always fit."
            )
        frame = self.bars(frequency, limit=ACTIVITY_BAR_CAP)
        if frame.height == 0:
            return pl.DataFrame(schema=ACTIVITY_SCHEMA)
        config = self._config(frequency)
        profile = activity_profile(BarFrame(frame.lazy(), frequency, self.label), config)
        return profile.collect()

    def _bar_count(self, frequency: Frequency) -> int:
        """How many bars the API holds at `frequency`; 0 when it holds none."""
        return sum(item.bars for item in self.summary().by_frequency if item.frequency is frequency)

    def _config(self, frequency: Frequency) -> QualityConfig:
        """The thresholds the server actually used, read back out of its own report."""
        thresholds = self.quality_summary(frequency).thresholds
        declared = thresholds.get("config")
        return QualityConfig.model_validate(declared) if declared else QualityConfig()

    # --- ingestion --------------------------------------------------------- #

    def upload(self, name: str, data: bytes) -> IngestResponse:
        """Ingest an uploaded file and report on it."""
        try:
            # The client's own timeout applies; passing one per request would override the
            # long ceiling set in __init__, which an upload needs more than any other call.
            response = self._client.post("/ingest", files={"file": (name, data)})
        except httpx.HTTPError as exc:
            raise BackendError(f"could not upload {name} to {self.base_url} ({exc})") from exc
        return IngestResponse.model_validate(_decode(response, "/ingest"))


def _decode(response: httpx.Response, path: str) -> dict[str, Any]:
    """The JSON body, or a `BackendError` carrying the API's own `detail`."""
    if response.is_success:
        body: dict[str, Any] = response.json()
        return body
    raise BackendError(f"{path} answered {response.status_code}: {_detail(response)}")


def _detail(response: httpx.Response) -> str:
    """The API's `detail` string, falling back to the raw body when it has none.

    FastAPI answers a query-parameter failure with a *list* of per-parameter problems
    rather than a sentence, so anything that is not already a string is re-encoded as JSON
    instead of being dropped — a user staring at an unexplained empty page is worse served
    than one staring at a slightly technical one.
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or response.reason_phrase
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        return detail if isinstance(detail, str) else json.dumps(detail)
    return json.dumps(payload)


def _selection(
    frequency: Frequency | None = None,
    contracts: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    view: View | None = None,
) -> dict[str, Any]:
    """A `BarSelection` as query parameters, omitting everything left unsaid."""
    params: dict[str, Any] = {}
    if frequency is not None:
        params["frequency"] = frequency.value
    if contracts:
        params["contract"] = list(contracts)
    if start is not None:
        params["start"] = start.isoformat()
    if end is not None:
        params["end"] = end.isoformat()
    if view is not None:
        params["view"] = view.value
    return params


def _scalar(values: Mapping[str, Any]) -> dict[str, Any]:
    """Query parameters with enums and dates flattened to the strings httpx can send."""
    flattened: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, Frequency | View):
            flattened[key] = value.value
        elif isinstance(value, date):
            flattened[key] = value.isoformat()
        elif isinstance(value, BaseModel):
            flattened[key] = value.model_dump(mode="json")
        else:
            flattened[key] = value
    return flattened


# --------------------------------------------------------------------------- #
# choosing a backend
# --------------------------------------------------------------------------- #


def build_backend(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Backend:
    """The backend a `streamlit run` invocation asked for.

    `--embedded` (or `MDQ_UI_EMBEDDED=1`) runs the service in this process, which is what
    `make ui` does so the dashboard is demonstrable with one command and no second
    terminal. Otherwise the dashboard is an HTTP client of the API at `--api-url`, or
    `MDQ_API_URL`, or `DEFAULT_API_URL` — the arrangement a real deployment uses, and the
    one `make api` + `make ui-http` runs.
    """
    arguments = list(argv if argv is not None else [])
    environment = dict(env or {})
    if "--embedded" in arguments or _truthy(environment.get("MDQ_UI_EMBEDDED")):
        return EmbeddedBackend.autoloaded(environment.get("MDQ_DATA_DIR"))
    return HttpBackend(_api_url(arguments, environment))


def _api_url(arguments: Sequence[str], env: Mapping[str, str]) -> str:
    """`--api-url URL`, `--api-url=URL`, `MDQ_API_URL`, then the default."""
    for index, argument in enumerate(arguments):
        if argument.startswith("--api-url="):
            return argument.split("=", 1)[1]
        if argument == "--api-url" and index + 1 < len(arguments):
            return arguments[index + 1]
    return env.get("MDQ_API_URL") or DEFAULT_API_URL


def _truthy(value: str | None) -> bool:
    """Environment-variable truth: `1`, `true`, `yes`, `on`, in any case."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# turning answers into the frames a chart wants
# --------------------------------------------------------------------------- #


def conform(rows: Sequence[Mapping[str, Any]], schema: pl.Schema) -> pl.DataFrame:
    """Rows from either backend as one typed frame in `schema`.

    JSON has no timestamp type, so `/bars` hands back `"2026-03-02T23:00:00Z"` where the
    in-process service hands back a `datetime`. Every string column whose target is a date
    or an instant is parsed rather than cast — a cast would raise, and silently dropping
    the column would produce an empty chart with no explanation. Missing columns become
    typed nulls, extra columns are dropped, and empty input yields an empty frame with the
    full schema so a chart can still be drawn (as an empty state) without a branch.
    """
    if not rows:
        return pl.DataFrame(schema=schema)
    frame = pl.DataFrame(list(rows), infer_schema_length=None)
    present = frame.collect_schema()
    projected = frame.select(
        [_conform_expr(name, dtype, present.get(name)) for name, dtype in schema.items()]
    )
    return projected.cast(dict(schema))  # type: ignore[arg-type]


def _conform_expr(name: str, dtype: pl.DataType, actual: pl.DataType | None) -> pl.Expr:
    """The expression that turns one arriving column into its schema dtype."""
    if actual is None:
        return pl.lit(None, dtype).alias(name)
    if actual == pl.String and dtype != pl.String:
        if isinstance(dtype, pl.Datetime):
            return (
                pl.col(name)
                .str.to_datetime(time_unit=dtype.time_unit, time_zone=dtype.time_zone)
                .alias(name)
            )
        if dtype == pl.Date:
            return pl.col(name).str.to_date().alias(name)
    return pl.col(name)


@dataclass(frozen=True)
class SeverityTriage:
    """The severity split, said the way the Overview says it.

    `needs_attention` is errors plus warnings — the findings a person should act on today.
    `expected` is the INFO tier: settlement prints on days nothing traded, exchange
    holidays, dormant contracts. It is the overwhelming majority (14,974 of 15,018 daily
    findings on the real corpus) and it is *correct data*, which is why it is counted and
    named rather than listed.
    """

    errors: int
    warnings: int
    expected: int

    @property
    def needs_attention(self) -> int:
        """Findings above INFO — what the findings table opens on."""
        return self.errors + self.warnings

    @property
    def total(self) -> int:
        """Every finding, at every severity."""
        return self.needs_attention + self.expected

    @property
    def hidden_note(self) -> str:
        """One sentence telling the reader what they are not being shown, and why."""
        if self.expected == 0:
            return "Every finding is shown."
        return (
            f"{self.expected:,} further finding(s) are hidden because they are expected: "
            "settlement prints on days the contract did not trade, exchange holidays and "
            "dormant contracts. Tick “include expected” to see them."
        )


def severity_triage(counts: Mapping[str, int]) -> SeverityTriage:
    """Split a `findings_by_severity` map into what to act on and what to expect."""
    return SeverityTriage(
        errors=int(counts.get("ERROR", 0)),
        warnings=int(counts.get("WARNING", 0)),
        expected=int(counts.get("INFO", 0)),
    )


def check_totals(summary: QualitySummaryResponse) -> pl.DataFrame:
    """One row per check and severity, busiest check first — the Overview bar chart.

    Sorted by the check's *total* bars affected rather than by severity, because the chart
    answers "what is this dataset mostly made of?" and the colour already answers "how bad
    is it?".
    """
    rows = [
        {
            "check": item.title,
            "severity": severity,
            "findings": int(count),
            "bars_affected": item.bars_affected,
        }
        for item in summary.checks
        for severity, count in item.findings_by_severity.items()
    ]
    if not rows:
        return pl.DataFrame(schema=CHECK_SCHEMA)
    return pl.DataFrame(rows, schema=CHECK_SCHEMA).sort(
        ["bars_affected", "check"], descending=[True, False]
    )


@dataclass(frozen=True)
class HeatmapData:
    """A contract x month grid of how much each instrument actually traded.

    Attributes:
        contracts: Row labels, one instrument each.
        months: Column labels, `YYYY-MM`, in calendar order.
        active_share: `active_share[row][col]` in `0..1`, or `None` where the contract had
            no sessions that month. Nulls are deliberate — a blank cell means "not listed
            yet / already expired", which is different from "listed and silent".
        hover: The same grid as sentences, for the tooltip.
    """

    contracts: list[str]
    months: list[str]
    active_share: list[list[float | None]]
    hover: list[list[str]]

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to draw."""
        return not self.contracts or not self.months


def heatmap_data(activity: pl.DataFrame) -> HeatmapData:
    """Collapse an activity profile into the Overview's contract x month heatmap.

    A month is the right bucket because it is the unit a listing cycle is described in: a
    deferred contract is dormant for a year and then wakes up, and that shape — a dark
    block turning bright a few months before expiry — is the single clearest picture of
    why 47% of this corpus is flat bars and why flagging all of it would be useless.
    """
    if activity.height == 0:
        return HeatmapData([], [], [], [])
    counted = (
        activity.with_columns(pl.col(C.SESSION_DATE).dt.strftime("%Y-%m").alias("month"))
        .group_by(C.CONTRACT, "month", A.REGIME)
        .agg(pl.len().alias("sessions"))
        .pivot(on=A.REGIME, index=[C.CONTRACT, "month"], values="sessions")
    )
    for regime in Regime:
        if regime.value not in counted.columns:
            counted = counted.with_columns(pl.lit(None, pl.UInt32).alias(regime.value))
    filled = counted.with_columns(
        *(pl.col(regime.value).fill_null(0).cast(pl.Int64).alias(regime.value) for regime in Regime)
    ).with_columns(
        (
            pl.col(Regime.ACTIVE.value) + pl.col(Regime.THIN.value) + pl.col(Regime.DORMANT.value)
        ).alias("sessions")
    )
    contracts = sorted(filled.get_column(C.CONTRACT).unique().to_list())
    months = sorted(filled.get_column("month").unique().to_list())
    cells = {(row[C.CONTRACT], row["month"]): row for row in filled.to_dicts()}
    shares: list[list[float | None]] = []
    hovers: list[list[str]] = []
    for contract in contracts:
        share_row: list[float | None] = []
        hover_row: list[str] = []
        for month in months:
            cell = cells.get((contract, month))
            if cell is None or cell["sessions"] == 0:
                share_row.append(None)
                hover_row.append(f"{contract} · {month}<br>not listed / no sessions")
                continue
            share_row.append(cell[Regime.ACTIVE.value] / cell["sessions"])
            hover_row.append(
                f"{contract} · {month}<br>{cell['sessions']} session(s): "
                f"{cell[Regime.ACTIVE.value]} normal, {cell[Regime.THIN.value]} light, "
                f"{cell[Regime.DORMANT.value]} with nothing traded"
            )
        shares.append(share_row)
        hovers.append(hover_row)
    return HeatmapData(contracts, months, shares, hovers)


def findings_table(findings: Sequence[FindingOut]) -> pl.DataFrame:
    """The findings page's table: the columns a person reads, in reading order.

    Takes the findings themselves rather than the response wrapping them, because the same
    table is drawn twice from two different shapes — the findings endpoint's page, and the
    list an upload report carries about one file.

    `evidence` is JSON-encoded back into a string here rather than shown as a column of
    dicts — the table stays readable, and the evidence expander decodes it again for the
    one finding the user picked.
    """
    rows = [
        {
            "Severity": finding.severity,
            "Instrument": finding.contract,
            "Session": finding.session_date,
            "What was found": finding.title,
            "Bars affected": finding.bars_affected,
            "Detail": finding.message,
            "check_id": finding.check_id,
            "evidence": json.dumps(finding.evidence, sort_keys=True),
        }
        for finding in findings
    ]
    schema = pl.Schema(
        [
            ("Severity", pl.String),
            ("Instrument", pl.String),
            ("Session", pl.Date),
            ("What was found", pl.String),
            ("Bars affected", pl.Int64),
            ("Detail", pl.String),
            ("check_id", pl.String),
            ("evidence", pl.String),
        ]
    )
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def gap_segments(findings: Sequence[FindingOut]) -> pl.DataFrame:
    """The findings that describe a *hole*, as drawable segments in `GAP_SCHEMA`.

    Two checks qualify and they carry their span differently, which is the whole reason
    this is a function rather than a `select`. `intrabar_gap` knows the two instants it
    sits between, so it becomes a segment of exactly that width and reports the minutes it
    is missing. `missing_session` has no instants at all — nothing was recorded, so there
    is nothing to timestamp — so it becomes the whole session it names, `whole_session` is
    set, and `minutes_missing` stays null rather than being invented. Anything else is
    dropped: a bad price has no duration and drawing it as one would be a lie.
    """
    rows: list[dict[str, Any]] = []
    for finding in findings:
        if finding.check_id not in GAP_CHECKS:
            continue
        session = finding.session_date
        observed = finding.start_utc is not None and finding.end_utc is not None
        if observed:
            from_utc, to_utc = finding.start_utc, finding.end_utc
            missing = finding.evidence.get("minutes_missing")
        elif session is not None:
            from_utc, to_utc = _utc_midnight(session), _utc_midnight(session, days=1)
            missing = None
        else:
            continue
        rows.append(
            {
                C.CONTRACT: finding.contract,
                C.SESSION_DATE: session,
                "from_utc": from_utc,
                "to_utc": to_utc,
                "minutes_missing": int(missing) if missing is not None else None,
                "whole_session": not observed,
                "severity": finding.severity,
                "what": finding.message,
            }
        )
    if not rows:
        return pl.DataFrame(schema=GAP_SCHEMA)
    frame = pl.DataFrame(rows, schema=GAP_SCHEMA)
    return frame.sort([C.CONTRACT, "from_utc"], nulls_last=True)


def _utc_midnight(session: date, days: int = 0) -> datetime:
    """A session date as an instant, so a session-wide hole can be drawn on a time axis."""
    return datetime.combine(session + timedelta(days=days), time(), tzinfo=UTC)


def candles(backend: Backend, contract: str, start: date | None, end: date | None) -> pl.DataFrame:
    """Vendor daily bars for one instrument, ready to draw as a candlestick."""
    return backend.bars(Frequency.DAILY, [contract], start, end).sort(C.SESSION_DATE)


def session_coverage(
    backend: Backend, contract: str, start: date | None = None, end: date | None = None
) -> pl.DataFrame:
    """One row per minute session for `contract`: its OHLC and how many bars it holds.

    This is the session picker's source *and* the honesty check behind it. `bar_count`
    against a CME session's 1,380 possible minutes is what tells a user that 2026-03-02 is
    a 960-bar truncated session before they read a VWAP off it. Asking `daily_bars` to
    aggregate the minute frame server-side keeps this to one small row per session instead
    of pulling a fortnight of minute bars to count them.
    """
    frame = backend.analytic("daily_bars", Frequency.MINUTE, [contract], start, end)
    return frame.sort(C.SESSION_DATE) if frame.height else frame


def session_vwap(
    backend: Backend, contract: str, session: date, window: str = "15m"
) -> pl.DataFrame:
    """Close and rolling VWAP for one session, bar by bar.

    The two come from different calls — the bars carry the close, the analytic carries the
    average — and are joined on the instant, which is the only key that is unambiguous:
    on a DST fall-back hour two bars share a wall clock but the join is on `ts_utc`, and a
    bar that produced no VWAP (a window with no volume) keeps its close and a null average
    rather than disappearing from the chart.
    """
    bars = backend.bars(Frequency.MINUTE, [contract], session, session)
    if bars.height == 0:
        return pl.DataFrame(
            schema=pl.Schema(
                [
                    (C.TS_UTC, pl.Datetime("us", "UTC")),
                    (C.TS_LOCAL, pl.Datetime("us")),
                    (C.CLOSE, pl.Float64),
                    (C.VOLUME, pl.Int64),
                    (VWAP, pl.Float64),
                    (WINDOW_VOLUME, pl.Int64),
                    (BARS_IN_WINDOW, pl.UInt32),
                ]
            )
        )
    averages = backend.analytic(
        "rolling_vwap", Frequency.MINUTE, [contract], session, session, {"window": window}
    )
    return (
        bars.select(C.TS_UTC, C.TS_LOCAL, C.CLOSE, C.VOLUME)
        .join(
            averages.select(C.TS_UTC, VWAP, WINDOW_VOLUME, BARS_IN_WINDOW),
            on=C.TS_UTC,
            how="left",
        )
        .unique(subset=[C.TS_UTC], keep="first")
        .sort(C.TS_UTC)
    )


def reconciliation(
    vendor: Mapping[str, Any] | None, derived: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Vendor daily bar vs the same session rebuilt from minute bars, field by field.

    Published and verified on the real corpus: open, high and low agree exactly on liquid
    sessions, and **close agrees on none of them** — the vendor's close is a settlement
    price, not the last trade. This function therefore reports a difference; it never
    reports a defect, and the `note` column says which is which so a reader does not open
    a ticket about a convention.
    """
    if not vendor or not derived:
        return []
    fields = (
        (C.OPEN, "Open", "Session open. Matches on liquid sessions."),
        (C.HIGH, "High", "Session high. The minute file can miss a print in a thin session."),
        (C.LOW, "Low", "Session low. The minute file can miss a print in a thin session."),
        (
            C.CLOSE,
            "Close",
            "Expected to differ: the daily close is a settlement price, not the last trade.",
        ),
    )
    rows: list[dict[str, Any]] = []
    for column, label, note in fields:
        published, rebuilt = vendor.get(column), derived.get(column)
        difference = (
            float(rebuilt) - float(published)
            if published is not None and rebuilt is not None
            else None
        )
        rows.append(
            {
                "Field": label,
                "Vendor daily": published,
                "Rebuilt from minutes": rebuilt,
                "Difference": difference,
                "Note": note,
            }
        )
    rows.append(
        {
            "Field": "Bars in session",
            "Vendor daily": 1,
            "Rebuilt from minutes": derived.get(BAR_COUNT),
            "Difference": None,
            "Note": "A full CME session is 1,380 minutes; fewer means sparse coverage.",
        }
    )
    return rows


# --------------------------------------------------------------------------- #
# the copyable rule block
# --------------------------------------------------------------------------- #


def to_yaml(value: Any, indent: int = 0) -> str:
    """A JSON-safe value as YAML, so a suggested rule can be pasted into a config file.

    Hand-rolled rather than a dependency, and it can afford to be: the only thing it ever
    serialises is a `SuggestedRule.to_dict()`, which `mdq.insights.models` has already
    normalised to strings, numbers, booleans, lists and dicts. Strings are quoted whenever
    they could be read as something else (a number, a date, `yes`, an empty string), which
    is the one YAML trap that would silently change a rule's meaning.
    """
    pad = "  " * indent
    if isinstance(value, Mapping):
        if not value:
            return f"{pad}{{}}"
        lines = []
        for key, item in value.items():
            if isinstance(item, Mapping | list | tuple) and item:
                lines.append(f"{pad}{key}:")
                lines.append(to_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list | tuple):
        if not value:
            return f"{pad}[]"
        lines = []
        for item in value:
            if isinstance(item, Mapping | list | tuple) and item:
                lines.append(f"{pad}-")
                lines.append(to_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_yaml_scalar(value)}"


#: Strings YAML would read as booleans if they were left unquoted.
_YAML_RESERVED: Final = frozenset(
    {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}
)


def _yaml_scalar(value: Any) -> str:
    """One scalar, quoted whenever leaving it bare would change what it means."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, Mapping):
        return "{}"
    if isinstance(value, list | tuple):
        return "[]"
    text = str(value)
    if text == "" or text.lower() in _YAML_RESERVED or _looks_numeric(text) or _needs_quotes(text):
        return json.dumps(text)
    return text


def _looks_numeric(text: str) -> bool:
    """True when a bare string would come back out of YAML as a number."""
    try:
        float(text)
    except ValueError:
        return False
    return True


def _needs_quotes(text: str) -> bool:
    """True when a string carries a character YAML gives its own meaning."""
    return (
        text.strip() != text
        or any(char in text for char in ":#{}[]&*!|>'\"%@`,")
        or text[0] in "-?"
    )
