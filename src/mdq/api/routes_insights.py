"""`GET /insights` — why the data looks the way it does, and what to do about it.

The findings endpoint says *what* is wrong. This one says *why*, and hands back a concrete
rule to adopt. Forty-three incoherent bars in a table look like forty-three problems; the
insight that thirty-nine of them are one settlement convention, and that they land on
eleven dates across a whole product family, is the difference between a data-entry ticket
and a call to the vendor.

Each insight carries its `evidence` and a `confidence` that always means the same thing —
the share of the relevant findings the pattern accounts for — so two insights' confidences
can be compared and the comparison means something. An empty list is a real answer: it says
nothing recurring was found, not that the analysis failed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from mdq.api.deps import FrequencyChoice, ServiceDep
from mdq.api.schemas import InsightOut, InsightsResponse

__all__ = ["router"]

router = APIRouter(tags=["insights"])


@router.get("/insights", response_model=InsightsResponse, summary="Patterns and suggested rules")
def insights(service: ServiceDep, choice: Annotated[FrequencyChoice, Query()]) -> InsightsResponse:
    """Return every pattern found in the loaded data, strongest evidence first."""
    frequency = choice.frequency or service.default_frequency()
    found = service.insights(frequency)
    return InsightsResponse(
        frequency=frequency,
        insight_count=len(found),
        insights=[InsightOut.of(insight) for insight in found],
    )
