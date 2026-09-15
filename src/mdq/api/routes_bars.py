"""`GET /bars` — the canonical timeline, paged.

One row per bar, in `BAR_SCHEMA`: the contract, the UTC instant, the Chicago wall clock a
trader reads, the trading session the bar belongs to, and OHLCV. Nothing is rounded,
re-based or dropped on the way out.

Filtering to nothing is a 200 with an empty `rows`, never an error — "that instrument did
not trade in that window" is a perfectly ordinary answer and the dashboard renders it as
an empty state. Naming an instrument that was never loaded *is* an error (404), because
that is a typo, and a typo that looks like clean data is the worst answer of the three.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from mdq.api.deps import BarSelection, ServiceDep
from mdq.api.schemas import BarsResponse, page_rows

__all__ = ["router"]

router = APIRouter(tags=["data"])


@router.get("/bars", response_model=BarsResponse, summary="Bars for a selection")
def bars(service: ServiceDep, selection: Annotated[BarSelection, Query()]) -> BarsResponse:
    """Return the bars matching a contract and session-date selection."""
    frequency = selection.frequency or service.default_frequency()
    frame = service.bars(
        frequency=frequency,
        contracts=selection.contract,
        start=selection.start,
        end=selection.end,
        view=selection.view,
    )
    rows, next_offset = page_rows(frame.lf, selection.limit, selection.offset)
    return BarsResponse(
        frequency=frequency,
        view=selection.view,
        rows=rows,
        row_count=len(rows),
        limit=selection.limit,
        offset=selection.offset,
        next_offset=next_offset,
    )
