"""Route generation in isolation — including the case that must fail loudly at startup.

`build_analytic_routes` merges each analytic's `Params` with the shared `BarSelection`. If
an analytic ever declared a parameter the selection already uses, `?limit=` would mean one
thing on one route and something else on the next. That has to be an import-time error,
not a request-time surprise, and it is the one branch no real analytic reaches.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl
import pytest
from pydantic import BaseModel, ConfigDict

from mdq.api.routes_analytics import _request_model, _title
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame

pytestmark = pytest.mark.unit


class _CollidingParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: int = 10
    offset: int = 0


class _Colliding:
    """An analytic that would shadow the shared paging parameters."""

    name: ClassVar[str] = "collides"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    Params: ClassVar[type[BaseModel]] = _CollidingParams

    def run(self, bars: BarFrame, params: BaseModel) -> pl.DataFrame:
        return bars.collect()


class _Undocumented:
    name: ClassVar[str] = "undocumented"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    Params: ClassVar[type[BaseModel]] = BaseModel

    def run(self, bars: BarFrame, params: BaseModel) -> pl.DataFrame:
        return bars.collect()


def test_an_analytic_that_shadows_the_bar_selection_is_refused() -> None:
    with pytest.raises(ValueError, match=r"limit.*offset|offset.*limit") as raised:
        _request_model(_Colliding())  # type: ignore[arg-type]
    assert "collides" in str(raised.value)


def test_the_merged_model_carries_both_sets_of_parameters() -> None:
    from mdq.analytics.vwap import RollingVwap

    model = _request_model(RollingVwap())
    fields = set(model.model_fields)
    assert {"window", "price", "min_volume"} <= fields
    assert {"contract", "start", "end", "frequency", "view", "limit", "offset"} <= fields


def test_an_analytics_title_comes_from_its_docstring_and_falls_back_to_its_name() -> None:
    assert _title(_Colliding()) == "An analytic that would shadow the shared paging parameters."
    assert _title(_Undocumented()) == "undocumented"
