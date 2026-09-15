"""Analytics: session aggregation, rolling VWAP, and slicing.

The package's public surface is the registry — `REGISTRY.all()` is how the API
discovers what it can serve — plus the two analytics and `filter_bars`. Adding an
analytic means adding a module here and decorating it with `@register_analytic`;
nothing in this file, the API or the dashboard needs to change.

Importing this package does *not* import the analytic modules; discovery is lazy and
happens the first time the registry is queried.
"""

from __future__ import annotations

from mdq.analytics.filtering import InvalidRangeError, filter_bars
from mdq.analytics.registry import (
    REGISTRY,
    Analytic,
    AnalyticRegistry,
    FrequencyNotSupportedError,
    UnknownAnalyticError,
    coerce_params,
    register_analytic,
    run_analytic,
)

__all__ = [
    "REGISTRY",
    "Analytic",
    "AnalyticRegistry",
    "FrequencyNotSupportedError",
    "InvalidRangeError",
    "UnknownAnalyticError",
    "coerce_params",
    "filter_bars",
    "register_analytic",
    "run_analytic",
]
