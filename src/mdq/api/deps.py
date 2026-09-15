"""What every route shares: the service, and the query models that select bars.

`BarSelection` is deliberately one model rather than a handful of loose parameters. It is
reused verbatim by `GET /bars` *and*, through `mdq.api.routes_analytics`, merged into the
query model of every generated analytic route — so "which bars?" means the same thing, and
validates the same way, everywhere in the API.

The row-limit bounds come from the *declared* defaults of `AppSettings`, not from the
environment. A query parameter's accepted range is part of the published contract: it
appears in the OpenAPI document, and a document that changed shape depending on how the
server was started would be worse than useless to whoever is reading it.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Final

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from mdq.domain.config import AppSettings
from mdq.domain.frequency import Frequency
from mdq.service import MarketDataService, View

__all__ = [
    "ROW_LIMIT_DEFAULT",
    "ROW_LIMIT_MAX",
    "BarSelection",
    "FrequencyChoice",
    "ServiceDep",
    "get_service",
]

#: Paging bounds, published in the OpenAPI schema (PLAN §3.4: default 5,000, max 50,000).
ROW_LIMIT_DEFAULT: Final[int] = int(AppSettings.model_fields["row_limit_default"].default)
ROW_LIMIT_MAX: Final[int] = int(AppSettings.model_fields["row_limit_max"].default)

_FREQUENCY_HELP: Final = (
    "Bar frequency. Defaults to daily whenever daily data is loaded: it is the frequency "
    "the vendor publishes diagnostics for, and the one that answers fastest."
)
_CONTRACT_HELP: Final = (
    "Contract code, repeatable (contract=ESH26&contract=CLG26). Omit for every contract. "
    "A code that is not loaded is an error, not an empty answer."
)


def get_service(request: Request) -> MarketDataService:
    """The process-wide service, put on the app by `create_app`."""
    service: MarketDataService = request.app.state.service
    return service


#: The dependency every route annotates its first parameter with.
ServiceDep = Annotated[MarketDataService, Depends(get_service)]


class FrequencyChoice(BaseModel):
    """A query that names nothing but a frequency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frequency: Annotated[Frequency | None, Field(description=_FREQUENCY_HELP)] = None


class BarSelection(BaseModel):
    """Which bars a request is about, and how much of the answer to return."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: Annotated[list[str] | None, Field(description=_CONTRACT_HELP)] = None
    start: Annotated[
        date | None,
        Field(description="Earliest trading session to include (inclusive, YYYY-MM-DD)."),
    ] = None
    end: Annotated[
        date | None,
        Field(description="Latest trading session to include (inclusive, YYYY-MM-DD)."),
    ] = None
    frequency: Annotated[Frequency | None, Field(description=_FREQUENCY_HELP)] = None
    view: Annotated[
        View,
        Field(
            description=(
                "'raw' returns everything that was ingested. 'clean' applies the cleansing "
                "policy — exact duplicates collapsed, rows named by an ERROR finding "
                "removed — which requires running the quality checks first."
            )
        ),
    ] = View.RAW
    limit: Annotated[int, Field(ge=1, le=ROW_LIMIT_MAX, description="Maximum rows to return.")] = (
        ROW_LIMIT_DEFAULT
    )
    offset: Annotated[int, Field(ge=0, description="Rows to skip before this page.")] = 0
