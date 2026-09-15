"""Configuration: quality thresholds and application settings.

Two objects, deliberately separate:

* `QualityConfig` — the *numbers* that decide severities. Every heuristic threshold in
  the quality engine lives here (and only here) so it can be surfaced in the report:
  a business user must be able to see why a row was downgraded.
* `AppSettings` — deployment knobs read from the environment (`MDQ_*`).

`QualityConfig` deliberately does **not** hold the per-exchange session table: sessions
are behaviour (`mdq.time.sessions`), not thresholds, and are passed to checks through
`CheckContext.sessions`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["AppSettings", "QualityConfig", "get_settings"]


class QualityConfig(BaseModel):
    """Thresholds for the quality engine. Frozen; use `model_copy(update=...)`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- activity regime (mdq.quality.context) ---------------------------- #
    #: Per-contract bar-count percentile used as the "busy session" baseline.
    activity_percentile: float = Field(default=0.9, ge=0.5, le=1.0)
    #: Fraction of that baseline at or above which a session counts as ACTIVE.
    active_bar_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Fraction of that baseline below which a session counts as DORMANT.
    dormant_bar_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    #: Daily volume below this fraction of the contract maximum counts as THIN.
    daily_thin_volume_ratio: float = Field(default=0.01, ge=0.0, le=1.0)

    # --- gaps ------------------------------------------------------------- #
    #: Largest tolerated hole between consecutive minute bars of an ACTIVE session.
    max_gap_minutes: int = Field(default=5, gt=0)

    # --- outliers --------------------------------------------------------- #
    #: Rolling window, in ACTIVE bars, for the MAD baseline.
    outlier_window: int = Field(default=20, gt=1)
    #: Multiple of the rolling MAD beyond which a return is an outlier.
    outlier_mad_k: float = Field(default=8.0, gt=0.0)

    # --- timestamps ------------------------------------------------------- #
    #: Bars later than `now + this many days` are flagged.
    future_tolerance_days: int = Field(default=1, ge=0)
    #: Bars earlier than this year are flagged.
    min_valid_year: int = Field(default=1900, ge=1)

    # --- ingestion -------------------------------------------------------- #
    #: Abort ingestion when more than this fraction of lines are malformed.
    max_reject_ratio: float = Field(default=0.05, ge=0.0, le=1.0)

    # --- per-root overrides ----------------------------------------------- #
    #: Roots whose prices may legitimately be non-positive (CL settled negative
    #: in April 2020), documented rather than silently special-cased.
    negative_price_roots: frozenset[str] = Field(default=frozenset({"CL"}))

    def allows_negative_price(self, root: str | None) -> bool:
        """True when a non-positive price is legitimate for this root."""
        return root is not None and root.upper() in self.negative_price_roots


class AppSettings(BaseSettings):
    """Runtime settings, read from `MDQ_*` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="MDQ_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    #: Directory autoloaded at startup — `MDQ_DATA_DIR`.
    data_dir: Path = Field(default=Path("data/raw"))
    #: Upload size ceiling; the API answers 413 above it.
    max_upload_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    #: Default row limit for row-returning endpoints.
    row_limit_default: int = Field(default=5_000, gt=0)
    #: Hard ceiling for the `limit` query parameter.
    row_limit_max: int = Field(default=50_000, gt=0)
    #: Quality thresholds; override with `MDQ_QUALITY__MAX_GAP_MINUTES=…`.
    quality: QualityConfig = Field(default_factory=QualityConfig)


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Process-wide settings. Call `get_settings.cache_clear()` in tests."""
    return AppSettings()
