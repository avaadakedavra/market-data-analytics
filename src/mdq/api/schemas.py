"""Response models — the vocabulary the API speaks, and the paging every list shares.

These models are the documentation: FastAPI turns them into the OpenAPI schema, and the
OpenAPI schema is what a business user reads before they read any prose. So the names here
are the ones a trader or a data steward would use, not the ones the internals use:

* a finding's `count` column becomes **`bars_affected`**, because "count of what?" is the
  first question anyone asks of it;
* a check's `rows` total becomes `bars_affected` for the same reason;
* nothing is called a "frame", a "row id" or a "lazy" anything.

Every list-returning endpoint answers with the same four paging fields — `row_count`,
`limit`, `offset`, `next_offset` — and `next_offset` is `null` exactly when there is
nothing further to fetch, so a client loops until it is null and never has to compare
totals. Paging is applied **before** the rows are turned into dicts (`page_rows`), which
is what keeps `?limit=100` on a 5.3M-row frame a cheap request rather than a 5.3M-dict
serialisation followed by a slice.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.insights import Insight
from mdq.service import (
    CheckSummary,
    ContractInfo,
    QualitySummary,
    ServiceSummary,
    UploadReport,
    View,
)

__all__ = [
    "AnalyticInfo",
    "AnalyticResult",
    "AnalyticsResponse",
    "BarsResponse",
    "CheckTotal",
    "ContractsResponse",
    "DatasetsSummaryResponse",
    "ErrorResponse",
    "FindingOut",
    "FindingsResponse",
    "FrequencyBreakdown",
    "HealthResponse",
    "IngestResponse",
    "IngestStatsOut",
    "InsightOut",
    "InsightsResponse",
    "Page",
    "QualitySummaryResponse",
    "SeverityName",
    "SuggestedRuleOut",
    "error_responses",
    "page_rows",
]

#: Severity as a business user writes it in a query string.
SeverityName = Literal["INFO", "WARNING", "ERROR"]


class _Model(BaseModel):
    """Base for every response model: no extra fields, no surprises."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# paging
# --------------------------------------------------------------------------- #


class Page(_Model):
    """The paging fields every list-returning endpoint carries."""

    row_count: Annotated[int, Field(description="Rows in this response.")]
    limit: Annotated[int, Field(description="Maximum rows this response could hold.")]
    offset: Annotated[int, Field(description="Rows skipped before this page.")]
    next_offset: Annotated[
        int | None,
        Field(description="Offset of the next page, or null when this is the last one."),
    ] = None


def page_rows(
    frame: pl.DataFrame | pl.LazyFrame,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int | None]:
    """One page of `frame` as dicts, plus the offset of the next page (or `None`).

    One row beyond the page is fetched to decide whether a next page exists. That is the
    whole trick: it answers "is there more?" without counting a frame that may hold five
    million rows, and it keeps the slice inside Polars, where it is nearly free, instead of
    materialising everything into Python first.
    """
    sliced = frame.slice(offset, limit + 1)
    materialised = sliced.collect() if isinstance(sliced, pl.LazyFrame) else sliced
    has_more = materialised.height > limit
    rows = materialised.head(limit).to_dicts()
    return rows, (offset + limit if has_more else None)


# --------------------------------------------------------------------------- #
# health and inventory
# --------------------------------------------------------------------------- #


class HealthResponse(_Model):
    """Liveness, plus what the service has been pointed at. Cheap by construction."""

    status: Annotated[Literal["ok"], Field(description="Always 'ok' if the service answers.")]
    data_dir: Annotated[str, Field(description="Directory autoloaded at startup (MDQ_DATA_DIR).")]
    datasets: Annotated[list[str], Field(description="Ids of everything loaded so far.")]
    files: Annotated[int, Field(description="Data files registered across all datasets.")]
    frequencies: Annotated[
        list[Frequency], Field(description="Bar frequencies present in the loaded files.")
    ]
    in_memory: Annotated[
        list[Frequency],
        Field(
            description=(
                "Frequencies whose rows have actually been read. Files are registered at "
                "startup but only read when a request needs them."
            )
        ),
    ]
    analytics: Annotated[list[str], Field(description="Analytics this build can run.")]
    checks: Annotated[list[str], Field(description="Quality checks this build can run.")]


class FrequencyBreakdown(_Model):
    """What one bar frequency of the loaded data holds."""

    frequency: Frequency
    files: Annotated[int, Field(description="Source files carrying this frequency.")]
    contracts: list[str]
    exchanges: list[str]
    bars: Annotated[int, Field(description="Bars loaded at this frequency.")]
    first_session: date | None
    last_session: date | None


class DatasetsSummaryResponse(_Model):
    """The answer to "what have I got?"."""

    datasets: list[str]
    contracts: list[str]
    exchanges: list[str]
    bars: Annotated[int, Field(description="Bars loaded across every frequency.")]
    by_frequency: list[FrequencyBreakdown]

    @classmethod
    def of(cls, summary: ServiceSummary) -> DatasetsSummaryResponse:
        """Build from the service's own summary."""
        return cls(
            datasets=list(summary.datasets),
            contracts=list(summary.contracts),
            exchanges=list(summary.exchanges),
            bars=summary.row_count,
            by_frequency=[
                FrequencyBreakdown(
                    frequency=item.frequency,
                    files=item.sources,
                    contracts=list(item.contracts),
                    exchanges=list(item.exchanges),
                    bars=item.row_count,
                    first_session=item.first_session,
                    last_session=item.last_session,
                )
                for item in summary.frequencies
            ],
        )


class ContractOut(_Model):
    """One instrument in the loaded data."""

    contract: str
    root: str | None
    exchange: str | None
    frequencies: list[Frequency]
    first_session: date | None
    last_session: date | None
    bars: int

    @classmethod
    def of(cls, info: ContractInfo) -> ContractOut:
        """Build from the service's `ContractInfo`."""
        return cls(
            contract=info.contract,
            root=info.root,
            exchange=info.exchange,
            frequencies=list(info.frequencies),
            first_session=info.first_session,
            last_session=info.last_session,
            bars=info.bar_count,
        )


class ContractsResponse(_Model):
    """Every instrument the service can answer questions about."""

    contracts: list[ContractOut]
    contract_count: int


# --------------------------------------------------------------------------- #
# bars and analytics
# --------------------------------------------------------------------------- #


class BarsResponse(Page):
    """Bars, exactly as they sit on the canonical timeline."""

    frequency: Frequency
    view: Annotated[
        View,
        Field(
            description="'raw' is everything ingested; 'clean' has the cleansing policy applied."
        ),
    ]
    rows: Annotated[list[dict[str, Any]], Field(description="One object per bar.")]


class AnalyticInfo(_Model):
    """One analytic in the catalogue, with its query parameters."""

    name: str
    title: Annotated[str, Field(description="One-line description of what it computes.")]
    frequencies: Annotated[
        list[Frequency], Field(description="Bar frequencies this analytic is meaningful for.")
    ]
    path: Annotated[str, Field(description="Where to call it.")]
    parameters: Annotated[
        dict[str, Any],
        Field(description="JSON Schema for the analytic's own query parameters."),
    ]


class AnalyticsResponse(_Model):
    """Everything this build can compute. Generated from the analytic registry."""

    analytics: list[AnalyticInfo]


class AnalyticResult(Page):
    """One analytic's output over a selection of bars."""

    analytic: str
    frequency: Frequency
    view: View
    parameters: Annotated[
        dict[str, Any], Field(description="The parameters actually used, defaults filled in.")
    ]
    rows: list[dict[str, Any]]


# --------------------------------------------------------------------------- #
# quality
# --------------------------------------------------------------------------- #


class CheckTotal(_Model):
    """One check's contribution to the report, totalled over every contract."""

    check_id: str
    title: str
    findings: Annotated[int, Field(description="Findings this check produced.")]
    bars_affected: Annotated[
        int,
        Field(
            description=(
                "Bars those findings implicate. Larger than `findings` because one "
                "finding can cover a whole session."
            )
        ),
    ]
    contracts: Annotated[int, Field(description="Instruments the check fired on.")]
    findings_by_severity: dict[str, int]

    @classmethod
    def of(cls, summary: CheckSummary) -> CheckTotal:
        """Build from the service's `CheckSummary`."""
        return cls(
            check_id=summary.check_id,
            title=summary.title,
            findings=summary.findings,
            bars_affected=summary.bars_affected,
            contracts=summary.contracts,
            findings_by_severity=dict(summary.findings_by_severity),
        )


class QualitySummaryResponse(_Model):
    """The whole quality picture for one frequency."""

    frequency: Frequency
    total_findings: int
    bars_affected: int
    findings_by_severity: Annotated[
        dict[str, int], Field(description="Findings per severity; every severity is present.")
    ]
    checks: Annotated[list[CheckTotal], Field(description="Worst-affected check first.")]
    by_contract: Annotated[
        list[dict[str, Any]],
        Field(description="The check x severity x instrument grid behind the totals."),
    ]
    checks_run: Annotated[
        list[str],
        Field(description="Every check that executed, including ones that found nothing."),
    ]
    thresholds: Annotated[
        dict[str, Any],
        Field(
            description="The numbers that decided each severity, so a downgrade can be explained."
        ),
    ]

    @classmethod
    def of(cls, summary: QualitySummary) -> QualitySummaryResponse:
        """Build from the service's `QualitySummary`."""
        return cls(
            frequency=summary.frequency,
            total_findings=summary.total_findings,
            bars_affected=summary.bars_affected,
            findings_by_severity=dict(summary.findings_by_severity),
            checks=[CheckTotal.of(item) for item in summary.checks],
            by_contract=[dict(row) for row in summary.by_contract],
            checks_run=list(summary.checks_run),
            thresholds=dict(summary.thresholds),
        )


class FindingOut(_Model):
    """One quality observation, about one instrument, over one span of time."""

    check_id: str
    title: str
    severity: SeverityName
    contract: str | None
    frequency: Frequency
    session_date: date | None
    start_utc: datetime | None
    end_utc: datetime | None
    bars_affected: Annotated[int, Field(description="Bars this single finding covers.")]
    message: Annotated[str, Field(description="The finding in one sentence.")]
    evidence: Annotated[
        dict[str, Any],
        Field(description="The values behind the message, including a sample of row ids."),
    ]
    suggested_rule_id: Annotated[
        str | None, Field(description="The rule in /insights that would govern this finding.")
    ]


class FindingsResponse(Page):
    """Findings matching a filter, worst severity first."""

    frequency: Frequency
    findings: list[FindingOut]


def _severity_name(value: str) -> SeverityName:
    """The severity as the API spells it, validated against the domain enum."""
    name: SeverityName = Severity.parse(value).label  # type: ignore[assignment]
    return name


def finding_from_row(row: dict[str, Any], title: str) -> FindingOut:
    """Turn one `FINDING_SCHEMA` row into its API shape, decoding the evidence JSON."""
    evidence = row.get(C.EVIDENCE)
    return FindingOut(
        check_id=row[C.CHECK_ID],
        title=title,
        severity=_severity_name(row[C.SEVERITY]),
        contract=row.get(C.CONTRACT),
        frequency=Frequency.parse(row[C.FREQUENCY]),
        session_date=row.get(C.SESSION_DATE),
        start_utc=row.get(C.START_UTC),
        end_utc=row.get(C.END_UTC),
        bars_affected=int(row.get(C.COUNT) or 0),
        message=row[C.MESSAGE],
        evidence=json.loads(evidence) if evidence else {},
        suggested_rule_id=row.get(C.SUGGESTED_RULE_ID),
    )


# --------------------------------------------------------------------------- #
# insights
# --------------------------------------------------------------------------- #


class SuggestedRuleOut(_Model):
    """A concrete rule to adopt, in the form a user can paste into a config file."""

    rule_id: str
    kind: Annotated[
        str, Field(description="'cleansing' changes the data; 'validation' refuses it.")
    ]
    params: dict[str, Any]
    rationale: Annotated[str, Field(description="Why this rule, with the numbers behind it.")]
    confidence: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="Share of the relevant findings this pattern accounts for.",
        ),
    ]


class InsightOut(_Model):
    """One explained pattern in the data, and the rule it argues for."""

    id: str
    title: str
    pattern: Annotated[str, Field(description="The explanation, in prose, quantified.")]
    evidence: dict[str, Any]
    affected_contracts: list[str]
    finding_count: int
    rule: SuggestedRuleOut

    @classmethod
    def of(cls, insight: Insight) -> InsightOut:
        """Build from the insights layer's own JSON-safe dict."""
        return cls.model_validate(insight.to_dict())


class InsightsResponse(_Model):
    """Every pattern found, strongest evidence first."""

    frequency: Frequency
    insight_count: int
    insights: list[InsightOut]


# --------------------------------------------------------------------------- #
# ingestion
# --------------------------------------------------------------------------- #


class IngestStatsOut(_Model):
    """A factual account of one upload. No judgements, just counts."""

    source: str
    profile: Annotated[str, Field(description="The column layout the file was read as.")]
    frequency: Frequency
    rows_in: int
    rows_out: Annotated[int, Field(description="Rows that became bars.")]
    rows_rejected: Annotated[int, Field(description="Rows that could not be placed at all.")]
    rejects_by_reason: dict[str, int]
    was_sorted: Annotated[bool, Field(description="Whether the file arrived in time order.")]
    contracts: list[str]
    span: Annotated[list[datetime] | None, Field(description="First and last instant in the file.")]


class IngestResponse(_Model):
    """What happened to an uploaded file."""

    dataset_id: Annotated[
        str, Field(description="Where the upload was stored; use it to re-query its findings.")
    ]
    frequency: Frequency
    ingest: Annotated[IngestStatsOut, Field(description="Counts for your file.")]
    rejects: Annotated[
        list[dict[str, Any]],
        Field(description="A sample of the rows in your file that could not be used."),
    ]
    quality: Annotated[QualitySummaryResponse, Field(description="Checks run over your file.")]
    findings: Annotated[list[FindingOut], Field(description="What is wrong in your file.")]
    insights: Annotated[
        list[InsightOut],
        Field(
            description=(
                "Patterns across all loaded data, recomputed now that your file is part "
                "of it. A single small file rarely contains a pattern worth naming on its "
                "own, so these are drawn from everything the service holds."
            )
        ),
    ]

    @classmethod
    def of(cls, report: UploadReport, findings: list[FindingOut]) -> IngestResponse:
        """Build from the service's `UploadReport`."""
        return cls(
            dataset_id=report.dataset_id,
            frequency=report.frequency,
            ingest=IngestStatsOut.model_validate(dict(report.stats)),
            rejects=[dict(row) for row in report.rejects],
            quality=QualitySummaryResponse.of(report.quality),
            findings=findings,
            insights=[InsightOut.of(insight) for insight in report.insights],
        )


class ErrorResponse(_Model):
    """The shape of an error this API raises deliberately.

    FastAPI's own query-parameter validation answers 422 with a richer, per-parameter
    list instead; that shape is documented by FastAPI and is deliberately left alone,
    because it says *which* parameter was wrong and this one could not.
    """

    detail: Annotated[str, Field(description="What went wrong, in one sentence.")]


def error_responses() -> dict[int | str, dict[str, Any]]:
    """The deliberate failures, declared on every route so OpenAPI documents them."""
    return {
        400: {"model": ErrorResponse, "description": "Unreadable file format"},
        404: {"model": ErrorResponse, "description": "No such contract, analytic or check"},
        413: {"model": ErrorResponse, "description": "Upload above the configured size limit"},
    }
