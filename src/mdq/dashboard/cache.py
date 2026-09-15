"""Memoisation — the one place that decides what this dashboard is allowed to recompute.

Streamlit re-runs a page from the top on every widget change, so without this module
ticking a checkbox would re-run the quality engine. The numbers say how much that matters
(all measured on the full 116 MB corpus):

| What | Cold | Warm |
|---|---|---|
| `GET /datasets/summary` | 1.6 s (ingests both frequencies) | 1 ms |
| `GET /quality/summary` daily | 72 ms | 3 ms |
| `GET /quality/summary` **minute** | **5.2 s** | 6 ms |
| `GET /bars` daily / minute (p95) | 11 ms / 32 ms | — |
| `rolling_vwap` (p95) | 30 ms | — |

So: **everything is cached**, and the sizes of those numbers are why. The minute quality
pass in particular must never be triggered by an idle click, and a page that is about to
trigger it says so behind a spinner (`mdq.dashboard.pages.overview`).

Two mechanisms, chosen deliberately:

* `st.cache_resource` for the `Backend` itself. It holds an httpx connection pool or a
  whole `MarketDataService` — neither is copyable, and both should be shared by every
  session in the process rather than rebuilt per browser tab.
* `st.cache_data` for answers, which Streamlit copies on return so one page cannot mutate
  another page's cached frame.

Every cached function takes the backend as `_backend` — the underscore is Streamlit's
signal not to hash it — *and* `backend_label` as a plain string, which is what the cache is
actually keyed on. That second argument is not decoration: without it, two backends in one
process (a test switching from embedded to HTTP, a re-pointed API URL) would silently share
answers.

The cache is cleared exactly once, after an upload, because an upload is the only thing
that changes what the service holds.
"""

# Every cached function takes `backend_label` without reading it: it exists solely so that
# Streamlit's cache key distinguishes two backends in one process. Removing it would be a
# correctness bug, not a tidy-up, so the rule is turned off for the file rather than
# silenced ten times.
# ruff: noqa: ARG001
from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import polars as pl
import streamlit as st

from mdq.api.schemas import (
    ContractOut,
    DatasetsSummaryResponse,
    FindingsResponse,
    InsightsResponse,
    QualitySummaryResponse,
)
from mdq.dashboard.backend import (
    FINDINGS_PAGE,
    Backend,
    FindingQuery,
    build_backend,
    session_coverage,
    session_vwap,
)
from mdq.domain.frequency import Frequency

__all__ = [
    "activity",
    "analytic",
    "bars",
    "contracts",
    "coverage",
    "findings",
    "forget_everything",
    "get_backend",
    "insights",
    "quality_summary",
    "summary",
    "vwap_series",
]


@st.cache_resource(show_spinner="Connecting to the market data service…")
def get_backend() -> Backend:
    """The process-wide backend, built from the command line and the environment.

    `streamlit run app.py -- --embedded` reaches this as `sys.argv`, which is how `make ui`
    runs the whole platform in one process with no API to start first.
    """
    return build_backend(sys.argv[1:], os.environ)


def forget_everything() -> None:
    """Drop every cached answer. Called after an upload changes what is loaded."""
    st.cache_data.clear()


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #


@st.cache_data(show_spinner="Reading the loaded data…")
def _summary(backend_label: str, _backend: Backend) -> DatasetsSummaryResponse:
    return _backend.summary()


def summary(backend: Backend) -> DatasetsSummaryResponse:
    """What is loaded: contracts, exchanges, bar counts and span per frequency."""
    return _summary(backend.label, backend)


@st.cache_data(show_spinner=False)
def _contracts(
    backend_label: str, frequency: Frequency | None, _backend: Backend
) -> list[ContractOut]:
    return _backend.contracts(frequency)


def contracts(backend: Backend, frequency: Frequency | None = None) -> list[ContractOut]:
    """Every instrument that can be charted."""
    return _contracts(backend.label, frequency, backend)


# --------------------------------------------------------------------------- #
# bars and analytics
# --------------------------------------------------------------------------- #


@st.cache_data(show_spinner=False)
def _bars(
    backend_label: str,
    frequency: Frequency,
    contract_codes: tuple[str, ...] | None,
    start: date | None,
    end: date | None,
    limit: int,
    _backend: Backend,
) -> pl.DataFrame:
    return _backend.bars(frequency, contract_codes, start, end, limit=limit)


def bars(
    backend: Backend,
    frequency: Frequency,
    contract_codes: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50_000,
) -> pl.DataFrame:
    """Bars for a selection. Contracts become a tuple so the cache key is hashable."""
    codes = tuple(contract_codes) if contract_codes else None
    return _bars(backend.label, frequency, codes, start, end, limit, backend)


@st.cache_data(show_spinner=False)
def _analytic(
    backend_label: str,
    name: str,
    frequency: Frequency,
    contract_codes: tuple[str, ...] | None,
    start: date | None,
    end: date | None,
    params: tuple[tuple[str, Any], ...],
    _backend: Backend,
) -> pl.DataFrame:
    return _backend.analytic(name, frequency, contract_codes, start, end, dict(params))


def analytic(
    backend: Backend,
    name: str,
    frequency: Frequency,
    contract_codes: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    params: Mapping[str, Any] | None = None,
) -> pl.DataFrame:
    """One analytic's output. Parameters become sorted pairs so the key is stable."""
    codes = tuple(contract_codes) if contract_codes else None
    pairs = tuple(sorted((params or {}).items()))
    return _analytic(backend.label, name, frequency, codes, start, end, pairs, backend)


@st.cache_data(show_spinner=False)
def _session_coverage(
    backend_label: str,
    contract: str,
    start: date | None,
    end: date | None,
    _backend: Backend,
) -> pl.DataFrame:
    return session_coverage(_backend, contract, start, end)


def coverage(
    backend: Backend, contract: str, start: date | None = None, end: date | None = None
) -> pl.DataFrame:
    """One row per minute session for an instrument, with how many bars it holds."""
    return _session_coverage(backend.label, contract, start, end, backend)


@st.cache_data(show_spinner="Computing the rolling average…")
def _session_vwap(
    backend_label: str, contract: str, session: date, window: str, _backend: Backend
) -> pl.DataFrame:
    return session_vwap(_backend, contract, session, window)


def vwap_series(
    backend: Backend, contract: str, session: date, window: str = "15m"
) -> pl.DataFrame:
    """Close and rolling VWAP for one session, joined bar by bar."""
    return _session_vwap(backend.label, contract, session, window, backend)


# --------------------------------------------------------------------------- #
# quality and insights
# --------------------------------------------------------------------------- #


@st.cache_data(show_spinner=False)
def _quality_summary(
    backend_label: str, frequency: Frequency | None, _backend: Backend
) -> QualitySummaryResponse:
    return _backend.quality_summary(frequency)


def quality_summary(backend: Backend, frequency: Frequency | None = None) -> QualitySummaryResponse:
    """The whole quality picture for one frequency. Minute costs 5.2 s the first time."""
    return _quality_summary(backend.label, frequency, backend)


@st.cache_data(show_spinner=False)
def _findings(backend_label: str, query_json: str, _backend: Backend) -> FindingsResponse:
    return _backend.findings(FindingQuery.model_validate_json(query_json))


def findings(
    backend: Backend,
    frequency: Frequency | None = None,
    contract_codes: Sequence[str] | None = None,
    checks: Sequence[str] | None = None,
    severity: str | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = FINDINGS_PAGE,
) -> FindingsResponse:
    """One page of findings. The query is serialised because it holds unhashable lists."""
    query = FindingQuery(
        frequency=frequency,
        contract=list(contract_codes) if contract_codes else None,
        check=list(checks) if checks else None,
        severity=severity,  # type: ignore[arg-type]
        start=start,
        end=end,
        limit=limit,
    )
    return _findings(backend.label, query.model_dump_json(), backend)


@st.cache_data(show_spinner=False)
def _insights(
    backend_label: str, frequency: Frequency | None, _backend: Backend
) -> InsightsResponse:
    return _backend.insights(frequency)


def insights(backend: Backend, frequency: Frequency | None = None) -> InsightsResponse:
    """Every pattern found, strongest evidence first."""
    return _insights(backend.label, frequency, backend)


@st.cache_data(show_spinner=False)
def _activity(backend_label: str, frequency: Frequency, _backend: Backend) -> pl.DataFrame:
    return _backend.activity(frequency)


def activity(backend: Backend, frequency: Frequency = Frequency.DAILY) -> pl.DataFrame:
    """How busy every `(contract, session)` was — what the Overview heatmap renders."""
    return _activity(backend.label, frequency, backend)
