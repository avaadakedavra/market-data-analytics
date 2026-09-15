"""The registry contract — what WP6 generates its routes from."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, ClassVar

import polars as pl
import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mdq.analytics.registry import (
    REGISTRY,
    Analytic,
    AnalyticRegistry,
    FrequencyNotSupportedError,
    UnknownAnalyticError,
    coerce_params,
    run_analytic,
)
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame

pytestmark = pytest.mark.unit


class SpyParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: int = Field(default=1, ge=1)


class Spy:
    """A minimal conforming analytic, used to exercise the registry itself."""

    name: ClassVar[str] = "spy"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset({Frequency.DAILY})
    Params: ClassVar[type[BaseModel]] = SpyParams

    def run(
        self,
        bars: BarFrame,
        params: BaseModel | Mapping[str, Any] | None = None,
    ) -> pl.DataFrame:
        factor = coerce_params(SpyParams, params).factor
        return pl.DataFrame({"factor": [factor]})


@pytest.fixture
def registry() -> AnalyticRegistry:
    """A registry of its own.

    `load()` re-imports the analytics package, but Python caches modules, so the
    shipped analytics register on the *process-wide* registry (as their decorator
    says) and never leak into this one.
    """
    fresh = AnalyticRegistry()
    fresh.load()
    return fresh


# --- the shipped analytics ------------------------------------------------- #


def test_both_shipped_analytics_are_discovered() -> None:
    assert REGISTRY.names() == ["daily_bars", "rolling_vwap"]


def test_shipped_analytics_satisfy_the_protocol() -> None:
    for analytic in REGISTRY.all():
        assert isinstance(analytic, Analytic)
        assert issubclass(analytic.Params, BaseModel)


def test_frequency_filtering_is_what_the_api_will_list() -> None:
    assert [a.name for a in REGISTRY.for_frequency(Frequency.DAILY)] == ["daily_bars"]
    assert [a.name for a in REGISTRY.for_frequency(Frequency.MINUTE)] == [
        "daily_bars",
        "rolling_vwap",
    ]


def test_params_models_are_introspectable_for_route_generation() -> None:
    """WP6 turns `Params` into query parameters; that requires a real JSON schema."""
    vwap = REGISTRY.get("rolling_vwap")
    assert set(vwap.Params.model_fields) == {"window", "price", "min_volume"}
    schema = vwap.Params.model_json_schema()
    assert schema["properties"]["window"]["default"] == "15m"


def test_an_unknown_name_names_the_known_ones() -> None:
    with pytest.raises(UnknownAnalyticError, match=r"unknown analytic 'nope'.*daily_bars"):
        REGISTRY.get("nope")


# --- registration ----------------------------------------------------------- #


def test_register_stores_one_instance_and_returns_the_class(registry: AnalyticRegistry) -> None:
    assert registry.register(Spy) is Spy
    assert registry.names() == ["spy"]
    assert registry.get("spy") is registry.get("spy")


def test_registering_the_same_name_twice_is_refused(registry: AnalyticRegistry) -> None:
    registry.register(Spy)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Spy)


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("name", "", "non-empty string"),
        ("name", None, "non-empty string"),
        ("frequencies", frozenset(), "non-empty frozenset"),
        ("frequencies", {Frequency.DAILY}, "non-empty frozenset"),
        ("frequencies", frozenset({"daily"}), "only Frequency members"),
        ("Params", dict, "pydantic BaseModel subclass"),
        ("Params", None, "pydantic BaseModel subclass"),
        ("run", "not callable", "run must be callable"),
    ],
)
def test_a_malformed_analytic_is_refused_at_import_time(
    registry: AnalyticRegistry,
    attribute: str,
    value: object,
    message: str,
) -> None:
    broken = type("Broken", (Spy,), {attribute: value})
    with pytest.raises(TypeError, match=message):
        registry.register(broken)


def test_an_analytic_missing_run_entirely_is_refused(registry: AnalyticRegistry) -> None:
    class NoRun:
        name: ClassVar[str] = "no_run"
        frequencies: ClassVar[frozenset[Frequency]] = frozenset({Frequency.DAILY})
        Params: ClassVar[type[BaseModel]] = SpyParams

    with pytest.raises(TypeError, match="run must be callable"):
        registry.register(NoRun)  # type: ignore[arg-type]


# --- discovery -------------------------------------------------------------- #


@pytest.fixture
def scratch_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A throwaway importable package, so auto-import is tested for real."""
    marker = tmp_path / "scratch_marker.py"
    marker.write_text("imported: list[str] = []\n")
    package = tmp_path / "scratch_analytics"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "visible.py").write_text(
        "import scratch_marker\nscratch_marker.imported.append('visible')\n"
    )
    # Both of these must be skipped; importing either would blow up the test.
    (package / "registry.py").write_text("raise AssertionError('own registry re-imported')")
    (package / "_private.py").write_text("raise AssertionError('private module imported')")

    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    yield "scratch_analytics"
    for name in [n for n in sys.modules if n.startswith(("scratch_analytics", "scratch_marker"))]:
        del sys.modules[name]


def test_load_imports_siblings_once_skipping_registry_and_private(scratch_package: str) -> None:
    registry = AnalyticRegistry(scratch_package)
    registry.load()
    registry.load()  # idempotent: a second call must not re-import anything

    marker = importlib.import_module("scratch_marker")
    assert marker.imported == ["visible"]


def test_lookup_helpers_trigger_discovery_without_an_explicit_load(scratch_package: str) -> None:
    assert AnalyticRegistry(scratch_package).all() == []
    marker = importlib.import_module("scratch_marker")
    assert marker.imported == ["visible"]


# --- parameter coercion ------------------------------------------------------ #


def test_coerce_params_accepts_none_a_mapping_and_a_model() -> None:
    assert coerce_params(SpyParams, None).factor == 1
    assert coerce_params(SpyParams, {"factor": 3}).factor == 3
    already = SpyParams(factor=5)
    assert coerce_params(SpyParams, already) is already


def test_coerce_params_converts_a_foreign_model_by_field() -> None:
    class Other(BaseModel):
        factor: int = 7

    assert coerce_params(SpyParams, Other()).factor == 7


def test_coerce_params_rejects_bad_values_so_the_api_can_answer_422() -> None:
    with pytest.raises(ValidationError):
        coerce_params(SpyParams, {"factor": 0})
    with pytest.raises(ValidationError):
        coerce_params(SpyParams, {"unknown": 1})


# --- run_analytic ------------------------------------------------------------ #


def test_run_analytic_validates_params_and_dispatches(registry: AnalyticRegistry) -> None:
    registry.register(Spy)
    bars = BarFrame.empty(Frequency.DAILY)
    result = run_analytic("spy", bars, {"factor": 4}, registry=registry)
    assert result["factor"].to_list() == [4]


def test_run_analytic_refuses_a_frequency_the_analytic_does_not_declare() -> None:
    daily = BarFrame.empty(Frequency.DAILY)
    with pytest.raises(FrequencyNotSupportedError, match=r"rolling_vwap.*daily.*\['minute'\]"):
        run_analytic("rolling_vwap", daily)


def test_run_analytic_defaults_to_the_process_registry() -> None:
    result = run_analytic("daily_bars", BarFrame.empty(Frequency.MINUTE))
    assert result.height == 0
