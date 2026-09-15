"""Quality thresholds and MDQ_* settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mdq.domain.config import AppSettings, QualityConfig, get_settings

pytestmark = pytest.mark.unit


def test_quality_config_defaults_match_the_plan() -> None:
    config = QualityConfig()
    assert config.activity_percentile == 0.9
    assert config.active_bar_ratio == 0.5
    assert config.dormant_bar_ratio == 0.1
    assert config.daily_thin_volume_ratio == 0.01
    assert config.max_gap_minutes == 5
    assert config.outlier_window == 20
    assert config.min_valid_year == 1900


def test_quality_config_is_frozen_but_copyable() -> None:
    config = QualityConfig()
    with pytest.raises(ValidationError):
        config.max_gap_minutes = 10  # type: ignore[misc]
    assert config.model_copy(update={"max_gap_minutes": 10}).max_gap_minutes == 10


@pytest.mark.parametrize(
    "kwargs",
    [
        {"active_bar_ratio": 1.5},
        {"max_gap_minutes": 0},
        {"outlier_mad_k": 0.0},
        {"outlier_window": 1},
        {"unknown_threshold": 1},
    ],
)
def test_quality_config_rejects_nonsense(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        QualityConfig(**kwargs)  # type: ignore[arg-type]


def test_negative_prices_are_allowed_only_where_documented() -> None:
    config = QualityConfig()
    assert config.allows_negative_price("CL")
    assert config.allows_negative_price("cl")
    assert not config.allows_negative_price("ES")
    assert not config.allows_negative_price(None)


def test_data_dir_defaults_to_data_raw() -> None:
    assert AppSettings().data_dir == Path("data/raw")


def test_data_dir_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDQ_DATA_DIR", "/tmp/elsewhere")
    assert AppSettings().data_dir == Path("/tmp/elsewhere")


def test_nested_quality_thresholds_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MDQ_QUALITY__MAX_GAP_MINUTES", "12")
    assert AppSettings().quality.max_gap_minutes == 12


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDQ_DATA_DIR", "/tmp/one")
    first = get_settings()
    monkeypatch.setenv("MDQ_DATA_DIR", "/tmp/two")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().data_dir == Path("/tmp/two")
