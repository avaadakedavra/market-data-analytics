"""`GET /analytics` and one generated `GET /analytics/{name}` per registered analytic.

**Nothing in this module names an analytic.** `rolling_vwap` and `daily_bars` appear in the
OpenAPI document, each with its own typed query parameters, because `build_analytic_routes`
walks `mdq.analytics.REGISTRY` at startup and synthesises a route per entry. Adding a third
analytic is a new module in `mdq/analytics/` with an `@register_analytic` on it: the route,
its parameters, its validation and its documentation all follow, and no file here changes.
That is the seam PLAN §1 promises, made real rather than asserted.

**How the parameters are generated.** Each analytic declares a pydantic `Params` model, so
the machinery is:

1. merge `Params` with `BarSelection` into one request model (`create_model` with both as
   bases) — a name collision between the two is a hard error at startup, not a silent
   shadowing at request time;
2. hand that model to FastAPI as `Annotated[Merged, Query()]`, which flattens it into
   individual query parameters and gives the OpenAPI document their real types, defaults
   and constraints;
3. attach it to a plain function through `__signature__`, because the model is only known
   at runtime.

Everything else falls out of that. `?window=bogus`, `?min_volume=-1` and a misspelt
parameter are all 422s produced by the analytic's own model — the same validation a direct
call to `run_analytic` would get — and the API contributes no rules of its own.

The merge is one model rather than two because FastAPI flattens exactly one query model per
endpoint; with two, `extra="forbid"` on each would reject the other's parameters.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Query
from pydantic import BaseModel, create_model

from mdq.analytics import REGISTRY, Analytic, UnknownAnalyticError
from mdq.api.deps import BarSelection, ServiceDep
from mdq.api.schemas import (
    AnalyticInfo,
    AnalyticResult,
    AnalyticsResponse,
    error_responses,
    page_rows,
)
from mdq.service import MarketDataService

__all__ = ["analytic_catalogue", "build_analytic_routes", "router"]

router = APIRouter(tags=["analytics"])

#: Where a generated analytic route lives.
_PREFIX = "/analytics"


def _path(name: str) -> str:
    return f"{_PREFIX}/{name}"


def _title(analytic: Analytic) -> str:
    """The analytic's own one-line description, taken from its class docstring."""
    doc = (type(analytic).__doc__ or "").strip()
    return doc.splitlines()[0] if doc else analytic.name


def analytic_catalogue() -> AnalyticsResponse:
    """Every registered analytic, with its parameter schema. Read from the registry."""
    return AnalyticsResponse(
        analytics=[
            AnalyticInfo(
                name=analytic.name,
                title=_title(analytic),
                frequencies=sorted(analytic.frequencies, key=lambda f: f.value),
                path=_path(analytic.name),
                parameters=analytic.Params.model_json_schema(),
            )
            for analytic in REGISTRY.all()
        ]
    )


@router.get("/analytics", response_model=AnalyticsResponse, summary="Analytics available")
def analytics() -> AnalyticsResponse:
    """List what this build can compute, and the parameters each one takes."""
    return analytic_catalogue()


def _request_model(analytic: Analytic) -> type[BaseModel]:
    """One query model carrying both the analytic's parameters and the bar selection.

    Raises:
        ValueError: if the analytic declares a parameter that `BarSelection` already uses.
            Shadowing would make `?limit=` mean one thing on one route and another on the
            next, which is exactly the kind of inconsistency a generated API exists to
            prevent.
    """
    clash = sorted(set(analytic.Params.model_fields) & set(BarSelection.model_fields))
    if clash:
        raise ValueError(
            f"analytic {analytic.name!r} declares parameter(s) {clash}, which collide with "
            f"the shared bar selection; rename them in its Params model"
        )
    model: type[BaseModel] = create_model(
        f"{type(analytic).__name__}Query",
        __base__=(analytic.Params, BarSelection),
    )
    return model


def _endpoint(analytic: Analytic, model: type[BaseModel]) -> Any:
    """A route handler for one analytic, with `model` flattened into query parameters."""

    def run(service: MarketDataService, query: BaseModel) -> AnalyticResult:
        selection = BarSelection.model_validate(
            {name: getattr(query, name) for name in BarSelection.model_fields}
        )
        params = analytic.Params.model_validate(
            {name: getattr(query, name) for name in analytic.Params.model_fields}
        )
        frequency = selection.frequency or service.frequency_for(analytic)
        frame = service.run_analytic(
            analytic.name,
            params,
            frequency=frequency,
            contracts=selection.contract,
            start=selection.start,
            end=selection.end,
            view=selection.view,
        )
        rows, next_offset = page_rows(frame, selection.limit, selection.offset)
        return AnalyticResult(
            analytic=analytic.name,
            frequency=frequency,
            view=selection.view,
            parameters=params.model_dump(mode="json"),
            rows=rows,
            row_count=len(rows),
            limit=selection.limit,
            offset=selection.offset,
            next_offset=next_offset,
        )

    run.__name__ = f"run_{analytic.name}"
    run.__doc__ = _title(analytic)
    # Built by hand because the request model only exists at runtime: FastAPI reads the
    # signature, not the `def`, so this is what turns `Params` into query parameters.
    run.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter("service", inspect.Parameter.KEYWORD_ONLY, annotation=ServiceDep),
            inspect.Parameter(
                "query",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Annotated[model, Query()],
            ),
        ]
    )
    return run


def build_analytic_routes(app: FastAPI) -> list[str]:
    """Add a route per registered analytic to `app`; return the paths added, in order.

    Called once by `create_app`. The catch-all registered afterwards is what turns an
    unknown analytic into a 404 that says which analytics *do* exist, instead of FastAPI's
    bare "Not Found".
    """
    paths: list[str] = []
    for analytic in REGISTRY.all():
        model = _request_model(analytic)
        app.add_api_route(
            _path(analytic.name),
            _endpoint(analytic, model),
            methods=["GET"],
            response_model=AnalyticResult,
            name=f"analytic_{analytic.name}",
            summary=f"Run {analytic.name}",
            tags=["analytics"],
            responses=error_responses(),
        )
        paths.append(_path(analytic.name))
    app.add_api_route(
        f"{_PREFIX}/{{name}}",
        _unknown_analytic,
        methods=["GET"],
        include_in_schema=False,
        name="analytic_unknown",
    )
    return paths


def _unknown_analytic(name: str) -> None:
    """Reject a name no analytic claims, naming the ones that exist.

    Raises:
        UnknownAnalyticError: always — a generated route would have matched otherwise.
    """
    raise UnknownAnalyticError(f"unknown analytic {name!r}; known analytics are {REGISTRY.names()}")
