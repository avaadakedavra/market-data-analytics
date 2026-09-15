"""`GET /quality/summary` and `GET /quality/findings` — what is wrong with the data.

The summary is the overview: how many findings, of what severity, from which check, on how
many instruments, together with the thresholds that decided each severity. Those thresholds
travel with the answer on purpose — a user looking at an INFO where they expected an ERROR
must be able to see the number that downgraded it without reading any source code.

The findings endpoint is the detail, filtered the way a person actually asks for it: this
instrument, at least this serious, from this check, over these dates. `severity` is a
*minimum*, so asking for WARNING returns warnings and errors: severity is a ranking, and a
filter that ignored the ranking would make the user ask twice for the thing they care about
most.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from mdq.api.deps import ROW_LIMIT_DEFAULT, ROW_LIMIT_MAX, FrequencyChoice, ServiceDep
from mdq.api.schemas import (
    FindingsResponse,
    QualitySummaryResponse,
    SeverityName,
    finding_from_row,
    page_rows,
)
from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.service import FindingFilter, check_title

__all__ = ["FindingQuery", "router"]

router = APIRouter(prefix="/quality", tags=["quality"])


class FindingQuery(BaseModel):
    """The filters `GET /quality/findings` accepts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: Annotated[
        list[str] | None,
        Field(description="Contract code, repeatable. Omit for every instrument."),
    ] = None
    severity: Annotated[
        SeverityName | None,
        Field(description="Minimum severity to include; WARNING returns warnings and errors."),
    ] = None
    check: Annotated[
        list[str] | None,
        Field(description="Check id, repeatable. See `checks_run` in /quality/summary."),
    ] = None
    start: Annotated[
        date | None, Field(description="Earliest trading session to include (inclusive).")
    ] = None
    end: Annotated[
        date | None,
        Field(
            description=(
                "Latest trading session to include (inclusive). Giving a date range "
                "excludes findings that name no session, such as a malformed record."
            )
        ),
    ] = None
    frequency: Annotated[Frequency | None, Field(description="Bar frequency to report on.")] = None
    limit: Annotated[int, Field(ge=1, le=ROW_LIMIT_MAX)] = ROW_LIMIT_DEFAULT
    offset: Annotated[int, Field(ge=0)] = 0

    def to_filter(self) -> FindingFilter:
        """The service-level filter this query describes."""
        return FindingFilter(
            frequency=self.frequency,
            contracts=tuple(self.contract) if self.contract is not None else None,
            min_severity=Severity.parse(self.severity) if self.severity is not None else None,
            checks=tuple(self.check) if self.check is not None else None,
            start=self.start,
            end=self.end,
        )


@router.get("/summary", response_model=QualitySummaryResponse, summary="Quality overview")
def quality_summary(
    service: ServiceDep, choice: Annotated[FrequencyChoice, Query()]
) -> QualitySummaryResponse:
    """Findings totalled by check and by instrument, with the thresholds in force."""
    return QualitySummaryResponse.of(service.quality_summary(choice.frequency))


@router.get("/findings", response_model=FindingsResponse, summary="Findings, filtered")
def findings(service: ServiceDep, query: Annotated[FindingQuery, Query()]) -> FindingsResponse:
    """Return the findings matching a filter, worst severity first."""
    frequency = query.frequency or service.default_frequency()
    frame = service.findings(query.to_filter())
    rows, next_offset = page_rows(frame, query.limit, query.offset)
    return FindingsResponse(
        frequency=frequency,
        findings=[finding_from_row(row, check_title(row["check_id"])) for row in rows],
        row_count=len(rows),
        limit=query.limit,
        offset=query.offset,
        next_offset=next_offset,
    )
