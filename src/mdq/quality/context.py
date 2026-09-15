"""The activity-regime model — the thing that makes this tool usable.

Unconditional flagging would bury the user. Measured on the real corpus: 47% of daily
rows and 48% of minute rows are flat bars, 81% of CLG26's daily bars are flat (71% are
also untraded), and a contract-day carrying any minute data holds a median of 222 bars
against the 1,380 a full CME session would have. A checker that shouts about every one of
those is noise, not quality control.

So every severity that could be explained by "nothing was trading" is conditioned on an
**observed** regime rather than on a hardcoded calendar. For each `(contract,
session_date)` we classify:

* ``DORMANT`` — no volume, or almost no bars relative to how busy this contract gets.
  Staleness and gaps here are expected, so they are downgraded to INFO.
* ``THIN``    — trading, but far below this contract's own norm.
* ``ACTIVE``  — a normal session for this contract. A stale bar or a 40-minute hole
  *here* is a real defect and keeps its full severity.

The baseline is always **per contract and observed from the data** (the p90 of
`bar_count` for minute frames, the maximum session volume for daily frames), never a
constant, which is what lets the model survive listing/expiry ramps, holidays and the
ESH26 contrast (median 2 bars/day in Jun-2025, 1,379 in Mar-2026) without a holiday
calendar.

Every threshold comes from `QualityConfig` and is written into the profile frame
alongside the regime, so a business user can always see *why* a row was downgraded.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import polars as pl

from mdq.domain.config import QualityConfig
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame, C

__all__ = [
    "ACTIVITY_SCHEMA",
    "A",
    "ActivityProfile",
    "Regime",
    "activity_profile",
]


class Regime(StrEnum):
    """How busy one `(contract, session_date)` was, relative to that contract's norm."""

    DORMANT = "DORMANT"
    THIN = "THIN"
    ACTIVE = "ACTIVE"


class A:
    """Column names of `ACTIVITY_SCHEMA` (`contract`/`session_date` come from `C`)."""

    BAR_COUNT: Final = "bar_count"
    VOLUME: Final = "session_volume"
    REGIME: Final = "regime"
    BASELINE: Final = "baseline"
    ACTIVE_THRESHOLD: Final = "active_threshold"
    DORMANT_THRESHOLD: Final = "dormant_threshold"


#: One row per `(contract, session_date)`. The three numeric columns after `regime` are
#: the *reason* for the classification and travel with it into the report.
ACTIVITY_SCHEMA: Final[pl.Schema] = pl.Schema(
    [
        (C.CONTRACT, pl.String),
        (C.SESSION_DATE, pl.Date),
        (A.BAR_COUNT, pl.UInt32),
        (A.VOLUME, pl.Int64),
        (A.REGIME, pl.String),
        (A.BASELINE, pl.Float64),
        (A.ACTIVE_THRESHOLD, pl.Float64),
        (A.DORMANT_THRESHOLD, pl.Float64),
    ]
)

#: Columns a check gains when it joins the profile onto its bars.
_JOINED_COLUMNS: Final = (
    A.BAR_COUNT,
    A.VOLUME,
    A.REGIME,
    A.BASELINE,
    A.ACTIVE_THRESHOLD,
    A.DORMANT_THRESHOLD,
)


@dataclass(frozen=True)
class ActivityProfile:
    """The regime classification for one dataset, plus the thresholds that produced it.

    Attributes:
        lf: One row per `(contract, session_date)`, conforming to `ACTIVITY_SCHEMA`.
        frequency: The frequency the profile was computed from. Minute profiles are
            bar-count driven, daily profiles volume driven.
        config: The thresholds in force, kept so the report can explain itself.
    """

    lf: pl.LazyFrame
    frequency: Frequency
    config: QualityConfig

    @classmethod
    def empty(cls, frequency: Frequency, config: QualityConfig | None = None) -> ActivityProfile:
        """A profile over no sessions — the neutral element for empty datasets."""
        return cls(
            pl.DataFrame(schema=ACTIVITY_SCHEMA).lazy(),
            frequency,
            config or QualityConfig(),
        )

    @property
    def metric(self) -> str:
        """Which observed quantity the baseline is measured in."""
        return A.BAR_COUNT if self.frequency is Frequency.MINUTE else A.VOLUME

    def collect(self) -> pl.DataFrame:
        """Materialise the profile."""
        return self.lf.collect()

    def join(self, bars: pl.LazyFrame) -> pl.LazyFrame:
        """Left-join the regime and its thresholds onto a bar frame.

        Rows whose session is unknown (only possible when the profile was built from a
        different frame) keep a null `regime`, which every check treats as "no
        downgrade" — never silently as DORMANT.
        """
        return bars.join(
            self.lf.select(C.CONTRACT, C.SESSION_DATE, *_JOINED_COLUMNS),
            on=[C.CONTRACT, C.SESSION_DATE],
            how="left",
        )

    def sessions_in(self, regime: Regime) -> pl.LazyFrame:
        """The `(contract, session_date)` pairs classified as `regime`."""
        return self.lf.filter(pl.col(A.REGIME) == regime.value).select(C.CONTRACT, C.SESSION_DATE)

    def active_span(self) -> pl.LazyFrame:
        """Per contract, the first and last ACTIVE `session_date`.

        This is what bounds "expected coverage": a contract cannot be missing a session
        before it started trading or after it expired, so `missing_session` only looks
        between these two dates. No listing calendar is needed.
        """
        return (
            self.lf.filter(pl.col(A.REGIME) == Regime.ACTIVE.value)
            .group_by(C.CONTRACT)
            .agg(
                pl.col(C.SESSION_DATE).min().alias("first_active"),
                pl.col(C.SESSION_DATE).max().alias("last_active"),
            )
        )

    def regime_counts(self) -> pl.DataFrame:
        """Sessions per `(contract, regime)` — what the Overview heatmap renders."""
        return (
            self.lf.group_by(C.CONTRACT, A.REGIME)
            .agg(pl.len().cast(pl.UInt32).alias("sessions"))
            .sort(C.CONTRACT, A.REGIME)
            .collect()
        )

    def thresholds(self) -> dict[str, Any]:
        """The knobs that produced this classification, for display in the report."""
        if self.frequency is Frequency.MINUTE:
            return {
                "metric": A.BAR_COUNT,
                "baseline": f"per-contract p{self.config.activity_percentile:.0%} of bar_count",
                "active_bar_ratio": self.config.active_bar_ratio,
                "dormant_bar_ratio": self.config.dormant_bar_ratio,
            }
        return {
            "metric": A.VOLUME,
            "baseline": "per-contract maximum session volume",
            "daily_thin_volume_ratio": self.config.daily_thin_volume_ratio,
            "dormant_rule": "session volume == 0",
        }


def activity_profile(bars: BarFrame, config: QualityConfig | None = None) -> ActivityProfile:
    """Classify every `(contract, session_date)` of `bars` into an activity regime.

    Minute frames are classified on **bar count** against the contract's own
    `activity_percentile` (p90 by default): ACTIVE at or above `active_bar_ratio` of
    that baseline with non-zero volume, DORMANT with zero volume or below
    `dormant_bar_ratio`, THIN in between.

    Daily frames have exactly one bar per session, so bar count says nothing; they are
    classified on **volume**: DORMANT at zero, THIN below `daily_thin_volume_ratio` of
    the contract's busiest session, ACTIVE otherwise.
    """
    cfg = config or QualityConfig()
    per_session = bars.lf.group_by(C.CONTRACT, C.SESSION_DATE).agg(
        pl.len().cast(pl.UInt32).alias(A.BAR_COUNT),
        pl.col(C.VOLUME).fill_null(0).sum().cast(pl.Int64).alias(A.VOLUME),
    )
    classified = (
        _minute_regimes(per_session, cfg)
        if bars.frequency is Frequency.MINUTE
        else _daily_regimes(per_session, cfg)
    )
    lf = (
        classified.select(ACTIVITY_SCHEMA.names())
        .cast(dict(ACTIVITY_SCHEMA))  # type: ignore[arg-type]
        .sort(C.CONTRACT, C.SESSION_DATE)
    )
    return ActivityProfile(lf, bars.frequency, cfg)


def _minute_regimes(per_session: pl.LazyFrame, cfg: QualityConfig) -> pl.LazyFrame:
    """Bar-count driven classification, baselined per contract."""
    baseline = (
        pl.col(A.BAR_COUNT)
        .quantile(cfg.activity_percentile, interpolation="nearest")
        .over(C.CONTRACT)
        .cast(pl.Float64)
    )
    with_thresholds = per_session.with_columns(
        baseline.alias(A.BASELINE),
    ).with_columns(
        (pl.col(A.BASELINE) * cfg.active_bar_ratio).alias(A.ACTIVE_THRESHOLD),
        (pl.col(A.BASELINE) * cfg.dormant_bar_ratio).alias(A.DORMANT_THRESHOLD),
    )
    bar_count = pl.col(A.BAR_COUNT).cast(pl.Float64)
    return with_thresholds.with_columns(
        pl.when((pl.col(A.VOLUME) <= 0) | (bar_count < pl.col(A.DORMANT_THRESHOLD)))
        .then(pl.lit(Regime.DORMANT.value))
        .when(bar_count >= pl.col(A.ACTIVE_THRESHOLD))
        .then(pl.lit(Regime.ACTIVE.value))
        .otherwise(pl.lit(Regime.THIN.value))
        .alias(A.REGIME)
    )


def _daily_regimes(per_session: pl.LazyFrame, cfg: QualityConfig) -> pl.LazyFrame:
    """Volume-driven classification — a daily session has only one bar to count."""
    with_thresholds = per_session.with_columns(
        pl.col(A.VOLUME).max().over(C.CONTRACT).cast(pl.Float64).alias(A.BASELINE),
    ).with_columns(
        (pl.col(A.BASELINE) * cfg.daily_thin_volume_ratio).alias(A.ACTIVE_THRESHOLD),
        pl.lit(0.0, pl.Float64).alias(A.DORMANT_THRESHOLD),
    )
    volume = pl.col(A.VOLUME).cast(pl.Float64)
    return with_thresholds.with_columns(
        pl.when(volume <= pl.col(A.DORMANT_THRESHOLD))
        .then(pl.lit(Regime.DORMANT.value))
        .when(volume < pl.col(A.ACTIVE_THRESHOLD))
        .then(pl.lit(Regime.THIN.value))
        .otherwise(pl.lit(Regime.ACTIVE.value))
        .alias(A.REGIME)
    )
