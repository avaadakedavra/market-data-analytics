"""`GET /datasets/summary`, `GET /contracts`, `POST /ingest` — what is loaded, and adding to it.

The two read endpoints are the ones a dashboard calls before it can draw anything: they
name the instruments and the date range the user is allowed to ask about. Both materialise
the frequencies they describe, which is the first point at which any rows are read at all.

`POST /ingest` is the only endpoint that changes what the service holds. Its response is
scoped on purpose: the counts, rejects and findings are about the uploaded file, because
that is what the person who just uploaded it is looking at; the insights are about every
loaded dataset with the new file in it, because a pattern needs a corpus to appear in.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, UploadFile, status

from mdq.api.deps import ServiceDep
from mdq.api.schemas import (
    ContractOut,
    ContractsResponse,
    DatasetsSummaryResponse,
    IngestResponse,
    finding_from_row,
)
from mdq.domain.frequency import Frequency
from mdq.service import check_title

__all__ = ["router"]

router = APIRouter(tags=["data"])


@router.get(
    "/datasets/summary",
    response_model=DatasetsSummaryResponse,
    summary="What data is loaded",
)
def datasets_summary(service: ServiceDep) -> DatasetsSummaryResponse:
    """Contracts, exchanges, bar counts and date span, per frequency and overall."""
    return DatasetsSummaryResponse.of(service.summary())


@router.get("/contracts", response_model=ContractsResponse, summary="Instruments available")
def contracts(service: ServiceDep, frequency: Frequency | None = None) -> ContractsResponse:
    """Every instrument in the loaded data, with its trading span and bar count.

    Omitting `frequency` merges an instrument that appears at both frequencies into a
    single entry, which is what a contract picker wants: one row per instrument, and the
    widest span it is known over.
    """
    found = service.contracts(frequency)
    return ContractsResponse(
        contracts=[ContractOut.of(info) for info in found],
        contract_count=len(found),
    )


@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a CSV or parquet file",
)
async def ingest(
    service: ServiceDep,
    file: Annotated[UploadFile, File(description="A CSV or parquet file of bars.")],
) -> IngestResponse:
    """Ingest an uploaded file and report everything known about it.

    The file's extension chooses the reader: an extension no reader claims is a 400, and a
    file above the configured size limit is a 413. Nothing is silently repaired — a row
    that could not be placed comes back in `rejects` with the reason and the original
    values, and a row that was placed but is wrong comes back in `findings`.
    """
    report = service.ingest_upload(file.filename or "upload", await file.read())
    findings = [
        finding_from_row(row, check_title(row["check_id"])) for row in report.findings.to_dicts()
    ]
    return IngestResponse.of(report, findings)
