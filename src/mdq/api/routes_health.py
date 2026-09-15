"""`GET /health` — is the service up, and what is it holding?

Deliberately the cheapest endpoint in the API: it reports what was *registered*, never
what was read, so a health probe can run every second against a 116 MB corpus without
pulling a single row into memory. `in_memory` is the one field that says which frequencies
have actually been materialised, which is how an operator sees the laziness working rather
than having to take it on trust.
"""

from __future__ import annotations

from fastapi import APIRouter

from mdq.analytics import REGISTRY
from mdq.api.deps import ServiceDep
from mdq.api.schemas import HealthResponse
from mdq.quality import CheckRegistry

__all__ = ["router"]

router = APIRouter(tags=["service"])


@router.get("/health", response_model=HealthResponse, summary="Service status")
def health(service: ServiceDep) -> HealthResponse:
    """Report liveness and what has been loaded."""
    data = service.data
    frequencies = data.frequencies()
    return HealthResponse(
        status="ok",
        data_dir=str(service.settings.data_dir),
        datasets=service.store.ids(),
        files=sum(len(dataset.sources) for dataset in service.store),
        frequencies=list(frequencies),
        in_memory=[f for f in frequencies if data.is_loaded(f)],
        analytics=REGISTRY.names(),
        checks=list(CheckRegistry.ids()),
    )
