"""The activity regime model — the thing that decides whether a finding is noise."""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from mdq.domain.config import QualityConfig
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame, C
from mdq.quality.context import ACTIVITY_SCHEMA, A, ActivityProfile, Regime, activity_profile
from support import faults as F
from support import scenarios, synth

pytestmark = pytest.mark.unit


def _profile(frame: pl.DataFrame, frequency: Frequency = Frequency.MINUTE) -> pl.DataFrame:
    bars = synth.as_bar_frame(frame, frequency)
    return activity_profile(bars).collect()


def _regimes(profile: pl.DataFrame) -> dict[date, str]:
    return dict(zip(profile[C.SESSION_DATE], profile[A.REGIME], strict=True))


def test_schema_is_exactly_as_declared() -> None:
    assert _profile(scenarios.minute_frame()).schema == ACTIVITY_SCHEMA


def test_dense_minute_sessions_are_active() -> None:
    regimes = _regimes(_profile(scenarios.minute_frame()))
    assert set(regimes.values()) == {Regime.ACTIVE.value}


def test_zero_volume_minute_session_is_dormant() -> None:
    """Volume, not bar count, has the final word: a full session that traded nothing
    cannot be ACTIVE however many bars it printed."""
    frame, _ = F.inject(scenarios.minute_frame(), F.FlatZeroVolumeBar(list(range(240, 360))))
    regimes = _regimes(_profile(frame))
    assert regimes[date(2026, 3, 5)] == Regime.DORMANT.value
    assert regimes[date(2026, 3, 3)] == Regime.ACTIVE.value


def test_sparse_minute_session_is_dormant() -> None:
    """10 bars against a p90 of 120 is below the 0.1 dormant ratio."""
    frame, _ = F.inject(
        scenarios.minute_frame(),
        F.DropRange(
            datetime(2026, 3, 4, 17, 5), datetime(2026, 3, 4, 18, 55), expect_finding=False
        ),
    )
    assert _regimes(_profile(frame))[date(2026, 3, 5)] == Regime.DORMANT.value


def test_middling_minute_session_is_thin() -> None:
    """Between the two ratios a session is THIN — trading, but not like it usually does."""
    frame, _ = F.inject(
        scenarios.minute_frame(),
        F.DropRange(
            datetime(2026, 3, 4, 17, 40), datetime(2026, 3, 4, 18, 50), expect_finding=False
        ),
    )
    profile = _profile(frame)
    assert _regimes(profile)[date(2026, 3, 5)] == Regime.THIN.value


def test_minute_thresholds_are_observed_per_contract_not_hardcoded() -> None:
    profile = _profile(scenarios.minute_frame(bars_per_session=200))
    row = profile.row(0, named=True)
    assert row[A.BASELINE] == 200.0
    assert row[A.ACTIVE_THRESHOLD] == pytest.approx(100.0)
    assert row[A.DORMANT_THRESHOLD] == pytest.approx(20.0)


def test_daily_regime_is_volume_driven() -> None:
    frame, _ = F.inject(scenarios.daily_frame(), F.FlatZeroVolumeBar([1]))
    regimes = _regimes(_profile(frame, Frequency.DAILY))
    assert regimes[date(2026, 3, 4)] == Regime.DORMANT.value
    assert regimes[date(2026, 3, 3)] == Regime.ACTIVE.value


def test_daily_thin_session_is_below_the_configured_share_of_the_contract_maximum() -> None:
    frame = scenarios.daily_frame()
    biggest = int(frame.get_column(C.VOLUME).max())
    frame = frame.with_columns(
        pl.when(pl.col(C.ROW_ID) == 1)
        .then(pl.lit(biggest // 1000, pl.Int64))
        .otherwise(pl.col(C.VOLUME))
        .alias(C.VOLUME)
    )
    assert _regimes(_profile(frame, Frequency.DAILY))[date(2026, 3, 4)] == Regime.THIN.value


def test_thresholds_are_surfaced_so_a_downgrade_can_be_explained() -> None:
    bars = synth.as_bar_frame(scenarios.minute_frame(), Frequency.MINUTE)
    minute = activity_profile(bars).thresholds()
    assert minute["metric"] == A.BAR_COUNT
    assert minute["active_bar_ratio"] == QualityConfig().active_bar_ratio

    daily = activity_profile(
        synth.as_bar_frame(scenarios.daily_frame(), Frequency.DAILY)
    ).thresholds()
    assert daily["metric"] == A.VOLUME
    assert daily["daily_thin_volume_ratio"] == QualityConfig().daily_thin_volume_ratio


def test_config_changes_the_classification() -> None:
    """The heuristic is configuration, not a constant buried in the check."""
    frame, _ = F.inject(
        scenarios.minute_frame(),
        F.DropRange(
            datetime(2026, 3, 4, 17, 40), datetime(2026, 3, 4, 18, 50), expect_finding=False
        ),
    )
    bars = synth.as_bar_frame(frame, Frequency.MINUTE)
    strict = QualityConfig(active_bar_ratio=0.2)
    regimes = _regimes(activity_profile(bars, strict).collect())
    assert regimes[date(2026, 3, 5)] == Regime.ACTIVE.value


def test_active_span_bounds_expected_coverage() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.FlatZeroVolumeBar(list(range(240, 360))))
    bars = synth.as_bar_frame(frame, Frequency.MINUTE)
    span = activity_profile(bars).active_span().collect().row(0, named=True)
    assert span["first_active"] == date(2026, 3, 3)
    # The dormant third session is outside the span, so it can never be "missing".
    assert span["last_active"] == date(2026, 3, 4)


def test_sessions_in_and_regime_counts() -> None:
    frame, _ = F.inject(scenarios.minute_frame(), F.FlatZeroVolumeBar(list(range(240, 360))))
    profile = activity_profile(synth.as_bar_frame(frame, Frequency.MINUTE))
    assert profile.sessions_in(Regime.DORMANT).collect().height == 1
    counts = dict(
        zip(
            profile.regime_counts()[A.REGIME],
            profile.regime_counts()["sessions"],
            strict=True,
        )
    )
    assert counts == {Regime.ACTIVE.value: 2, Regime.DORMANT.value: 1}


def test_join_leaves_an_unknown_session_null_rather_than_guessing_dormant() -> None:
    """A null regime must never be read as "nothing was trading" — that would silently
    downgrade findings for any frame the profile was not built from."""
    profile = activity_profile(synth.as_bar_frame(scenarios.minute_frame(), Frequency.MINUTE))
    other = synth.minute_sessions("ESH26", [date(2026, 3, 10)], bars_per_session=5, seed=2)
    joined = profile.join(other.lazy()).collect()
    assert joined.get_column(A.REGIME).null_count() == joined.height


def test_empty_profile_has_the_right_shape() -> None:
    empty = ActivityProfile.empty(Frequency.MINUTE)
    assert empty.collect().schema == ACTIVITY_SCHEMA
    assert empty.collect().height == 0
    assert empty.active_span().collect().height == 0


def test_empty_bar_frame_yields_an_empty_profile() -> None:
    profile = activity_profile(BarFrame.empty(Frequency.DAILY))
    assert profile.collect().height == 0
    assert profile.metric == A.VOLUME
