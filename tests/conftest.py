"""Shared fixtures.

Nothing here touches the network or `data/raw`: unit tests run entirely on synthetic
data built by `tests.support.synth`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from mdq.domain.config import QualityConfig, get_settings
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame
from support import synth

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the project root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def raw_data_dir(repo_root: Path) -> Path:
    """The (gitignored) real-data directory. Only golden tests may look at it."""
    return repo_root / "data" / "raw"


@pytest.fixture
def quality_config() -> QualityConfig:
    """Default thresholds."""
    return QualityConfig()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Settings are process-cached; never let one test's environment leak."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


#: Three consecutive, DST-free CME session dates (Mon–Wed).
SESSION_DATES: tuple[date, ...] = (date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5))


@pytest.fixture
def minute_df() -> pl.DataFrame:
    """Two short, perfectly clean CME minute sessions."""
    return synth.minute_sessions("ESH26", SESSION_DATES[:2], bars_per_session=120, seed=7)


@pytest.fixture
def minute_bars(minute_df: pl.DataFrame) -> BarFrame:
    """`minute_df` wrapped in the domain carrier."""
    return synth.as_bar_frame(minute_df, Frequency.MINUTE, "<synthetic-minute>")


@pytest.fixture
def daily_df() -> pl.DataFrame:
    """Three perfectly clean daily bars."""
    return synth.daily_series("ESH26", SESSION_DATES, seed=7)


@pytest.fixture
def daily_bars(daily_df: pl.DataFrame) -> BarFrame:
    """`daily_df` wrapped in the domain carrier."""
    return synth.as_bar_frame(daily_df, Frequency.DAILY, "<synthetic-daily>")
