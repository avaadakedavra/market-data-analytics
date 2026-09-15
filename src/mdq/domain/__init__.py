"""Foundation: the canonical schema, frequency, findings and configuration."""

from mdq.domain.config import AppSettings, QualityConfig, get_settings
from mdq.domain.findings import Finding, RejectReason, Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import (
    BAR_SCHEMA,
    FINDING_SCHEMA,
    BarFrame,
    C,
    SchemaValidationError,
    empty_bar_lf,
    empty_finding_df,
)

__all__ = [
    "BAR_SCHEMA",
    "FINDING_SCHEMA",
    "AppSettings",
    "BarFrame",
    "C",
    "Finding",
    "Frequency",
    "QualityConfig",
    "RejectReason",
    "SchemaValidationError",
    "Severity",
    "empty_bar_lf",
    "empty_finding_df",
    "get_settings",
]
