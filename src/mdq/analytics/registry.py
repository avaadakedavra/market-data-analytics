"""The analytic registry — the seam that turns "add a file" into "ship a feature".

An analytic is a tiny object with four things: a `name`, the `frequencies` it applies
to, a pydantic `Params` model, and a `run`. Nothing else in the platform knows what
analytics exist: the API enumerates this registry at startup and *generates* a
`GET /analytics/{name}` route per entry, deriving its query parameters from `Params`.
That is why `Params` must be a real, introspectable pydantic model and not a `dict`
— the OpenAPI document and the 422-on-bad-input behaviour both fall out of it.

Registration is by decorator; discovery is by `pkgutil`, so a new analytic is a new
module in this package and one `@register_analytic`. No existing file is edited.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping
from typing import Any, ClassVar, Protocol, runtime_checkable

import polars as pl
from pydantic import BaseModel

from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame

__all__ = [
    "REGISTRY",
    "Analytic",
    "AnalyticRegistry",
    "FrequencyNotSupportedError",
    "UnknownAnalyticError",
    "coerce_params",
    "register_analytic",
    "run_analytic",
]


class UnknownAnalyticError(LookupError):
    """Raised when no analytic is registered under a requested name (404 at the API)."""


class FrequencyNotSupportedError(ValueError):
    """Raised when an analytic is asked to run on a frequency it does not declare."""


@runtime_checkable
class Analytic(Protocol):
    """What every analytic must look like.

    Attributes:
        name: Stable identifier; becomes the URL segment `GET /analytics/{name}`.
        frequencies: The frequencies the analytic is meaningful for. The registry
            filters on this so that neither the API nor the dashboard branches on
            frequency itself.
        Params: A pydantic model describing every knob. Defaults must be sensible,
            because the API exposes them as optional query parameters.
    """

    name: ClassVar[str]
    frequencies: ClassVar[frozenset[Frequency]]
    Params: ClassVar[type[BaseModel]]

    def run(self, bars: BarFrame, params: BaseModel) -> pl.DataFrame:
        """Compute the analytic. Must accept an empty `bars` and return empty rows."""
        ...


def coerce_params[P: BaseModel](
    model: type[P],
    params: BaseModel | Mapping[str, Any] | None,
) -> P:
    """Normalise whatever a caller passed into the analytic's own `Params` model.

    The API hands us an already-validated model, the dashboard a plain dict, and a
    test often nothing at all. All three end up as one validated object, so that
    validation lives in exactly one place and every analytic can trust its params.

    Raises:
        pydantic.ValidationError: if the values do not satisfy `model` (422 at the API).
    """
    if params is None:
        return model()
    if isinstance(params, model):
        return params
    if isinstance(params, BaseModel):
        return model.model_validate(params.model_dump())
    return model.model_validate(dict(params))


def _validate_analytic(cls: type[Analytic]) -> None:
    """Fail loudly at import time rather than at request time."""
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name:
        raise TypeError(f"{cls.__name__}.name must be a non-empty string")
    frequencies = getattr(cls, "frequencies", None)
    if not isinstance(frequencies, frozenset) or not frequencies:
        raise TypeError(f"{cls.__name__}.frequencies must be a non-empty frozenset[Frequency]")
    if any(not isinstance(f, Frequency) for f in frequencies):
        raise TypeError(f"{cls.__name__}.frequencies must contain only Frequency members")
    params = getattr(cls, "Params", None)
    if not (isinstance(params, type) and issubclass(params, BaseModel)):
        raise TypeError(f"{cls.__name__}.Params must be a pydantic BaseModel subclass")
    if not callable(getattr(cls, "run", None)):
        raise TypeError(f"{cls.__name__}.run must be callable")


class AnalyticRegistry:
    """A name → analytic table that discovers its own contents.

    Instances are independent, which is what lets a test build a throwaway registry
    instead of mutating the process-wide one.
    """

    def __init__(self, package: str = "mdq.analytics") -> None:
        self._package = package
        self._analytics: dict[str, Analytic] = {}
        self._loaded = False

    # --- registration ------------------------------------------------------ #

    def register(self, cls: type[Analytic]) -> type[Analytic]:
        """Decorator: validate an analytic class and store one shared instance.

        Analytics are stateless, so a single instance per process is enough. The
        class is returned unchanged so the module can still export it.
        """
        _validate_analytic(cls)
        if cls.name in self._analytics:
            raise ValueError(
                f"analytic {cls.name!r} is already registered by "
                f"{type(self._analytics[cls.name]).__name__}"
            )
        self._analytics[cls.name] = cls()
        return cls

    # --- discovery --------------------------------------------------------- #

    def load(self) -> None:
        """Import every sibling module once, so decorators have run."""
        if self._loaded:
            return
        # Set the flag first: a module being imported may itself touch the registry.
        self._loaded = True
        package = importlib.import_module(self._package)
        for info in pkgutil.iter_modules(package.__path__):
            if info.name.startswith("_") or info.name == "registry":
                continue
            importlib.import_module(f"{self._package}.{info.name}")

    # --- lookup ------------------------------------------------------------ #

    def all(self) -> list[Analytic]:
        """Every registered analytic, ordered by name for a stable API listing."""
        self.load()
        return [self._analytics[name] for name in sorted(self._analytics)]

    def names(self) -> list[str]:
        """The registered names, sorted."""
        self.load()
        return sorted(self._analytics)

    def get(self, name: str) -> Analytic:
        """Look one up by name.

        Raises:
            UnknownAnalyticError: if nothing is registered under `name`.
        """
        self.load()
        try:
            return self._analytics[name]
        except KeyError:
            raise UnknownAnalyticError(
                f"unknown analytic {name!r}; known analytics are {sorted(self._analytics)}"
            ) from None

    def for_frequency(self, frequency: Frequency) -> list[Analytic]:
        """The analytics that declare `frequency`, ordered by name."""
        return [a for a in self.all() if frequency in a.frequencies]


#: The process-wide registry the API and dashboard read.
REGISTRY = AnalyticRegistry()


def register_analytic(cls: type[Analytic]) -> type[Analytic]:
    """Register an analytic on the process-wide registry."""
    return REGISTRY.register(cls)


def run_analytic(
    name: str,
    bars: BarFrame,
    params: BaseModel | Mapping[str, Any] | None = None,
    registry: AnalyticRegistry | None = None,
) -> pl.DataFrame:
    """Look an analytic up, check it applies to `bars`, validate params, run it.

    This is the one place the frequency guard lives, so the API and the dashboard
    both get the same 404 / 422 behaviour without repeating themselves.

    Raises:
        UnknownAnalyticError: no analytic registered under `name`.
        FrequencyNotSupportedError: the analytic does not declare `bars.frequency`.
        pydantic.ValidationError: `params` does not satisfy the analytic's model.
    """
    analytic = (registry or REGISTRY).get(name)
    if bars.frequency not in analytic.frequencies:
        supported = sorted(f.value for f in analytic.frequencies)
        raise FrequencyNotSupportedError(
            f"analytic {name!r} does not support {bars.frequency.value} bars "
            f"(supported: {supported})"
        )
    return analytic.run(bars, coerce_params(analytic.Params, params))
