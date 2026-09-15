"""The FastAPI application: assemble the routers, map the errors, autoload at startup.

Three things happen here and nothing else.

**Startup autoloads `MDQ_DATA_DIR`** — by registering the files, not by reading them.
Classifying the 116 MB sample costs 13 ms, so the server is answering requests almost
immediately and the cost of a frequency is paid by the first request that wants it. A
missing directory is not an error: the API comes up empty and every endpoint still answers.

**Errors are mapped once, centrally.** Each domain exception already means exactly one
thing, so the mapping is a table rather than a try/except in every route:

* **400** — the request is fine but the file is not a format we read:
  `UnsupportedFormatError`.
* **404** — a named thing that does not exist: `UnknownContractError`,
  `UnknownAnalyticError`, `UnknownCheckError`, `UnknownDatasetError`.
* **413** — the upload is above the configured ceiling: `UploadTooLargeError`.
* **422** — understood, but impossible to satisfy: `InvalidRangeError`,
  `FrequencyNotSupportedError`, pydantic's `ValidationError`.

Every one answers with the same `{"detail": "..."}` shape FastAPI already uses, so a client
has one error format to handle.

**Analytic routes are generated, not written.** `build_analytic_routes` reads
`mdq.analytics.REGISTRY` — see `mdq.api.routes_analytics`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from mdq.analytics import FrequencyNotSupportedError, InvalidRangeError, UnknownAnalyticError
from mdq.api import (
    routes_analytics,
    routes_bars,
    routes_datasets,
    routes_health,
    routes_insights,
    routes_quality,
)
from mdq.api.schemas import error_responses
from mdq.domain.config import AppSettings
from mdq.ingest import UnsupportedFormatError
from mdq.quality import UnknownCheckError
from mdq.service import (
    MarketDataService,
    UnknownContractError,
    UnknownDatasetError,
    UploadTooLargeError,
)

__all__ = ["app", "create_app"]

logger = logging.getLogger(__name__)

TITLE: Final = "Market Data Quality & Analytics"
DESCRIPTION: Final = """
Load futures bars, see what is wrong with them, and see what the defects mean.

* **/datasets/summary**, **/contracts** — what is loaded.
* **/bars**, **/analytics** — the data, and what can be computed from it.
* **/quality/summary**, **/quality/findings** — what is wrong, and where.
* **/insights** — why it is wrong, and the rule to adopt about it.
* **/ingest** — upload a CSV or parquet file and get all of the above for it.

Dates are trading **sessions**, not calendar days: a CME session dated Tuesday opens at
17:00 Chicago time on Monday. Both ends of a date range are inclusive.
"""

#: Exception type → HTTP status. One entry per meaning, not one per raise site.
_STATUS_BY_ERROR: Final[tuple[tuple[type[Exception], int], ...]] = (
    (UnsupportedFormatError, status.HTTP_400_BAD_REQUEST),
    (UnknownContractError, status.HTTP_404_NOT_FOUND),
    (UnknownAnalyticError, status.HTTP_404_NOT_FOUND),
    (UnknownCheckError, status.HTTP_404_NOT_FOUND),
    (UnknownDatasetError, status.HTTP_404_NOT_FOUND),
    (UploadTooLargeError, status.HTTP_413_CONTENT_TOO_LARGE),
    (InvalidRangeError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (FrequencyNotSupportedError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (ValidationError, status.HTTP_422_UNPROCESSABLE_CONTENT),
)


def _detail(exc: Exception) -> str:
    """The message, without the quotes `KeyError` adds to its own `str()`."""
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def _handler(code: int) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    """A handler answering `code` with the exception's own message as `detail`."""

    async def handle(request: Request, exc: Exception) -> JSONResponse:
        del request
        return JSONResponse(status_code=code, content={"detail": _detail(exc)})

    return handle


def create_app(service: MarketDataService | None = None) -> FastAPI:
    """Build the application.

    Args:
        service: An already-configured service — how a test injects a fixture directory
            or a stub store. When omitted, one is built from `MDQ_*` settings and
            autoloads `MDQ_DATA_DIR` at startup.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if service is None:
            application.state.service = MarketDataService(AppSettings())
            report = application.state.service.autoload()
            if report is not None:
                logger.info(
                    "loaded %d file(s) from %s (%d skipped)",
                    report.file_count,
                    report.path,
                    len(report.skipped),
                )
        yield

    application = FastAPI(
        title=TITLE,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
    )
    # Set before startup too, so a test that never enters the lifespan still has a service.
    application.state.service = service if service is not None else MarketDataService(AppSettings())

    for router in (
        routes_health.router,
        routes_datasets.router,
        routes_bars.router,
        routes_analytics.router,
        routes_quality.router,
        routes_insights.router,
    ):
        application.include_router(router, responses=error_responses())
    routes_analytics.build_analytic_routes(application)

    for error, code in _STATUS_BY_ERROR:
        application.add_exception_handler(error, _handler(code))
    return application


#: The application `uvicorn mdq.api.app:app` serves.
app = create_app()
