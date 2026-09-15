"""`EmbeddedBackend` and `HttpBackend` must be indistinguishable. This proves it.

The dashboard's whole design rests on one claim: a page cannot tell which backend it got.
If that claim were ever false, `AppTest` — which only ever runs the embedded one — would be
testing a dashboard nobody deploys.

So every test here runs the *same* assertion over both, and the last group compares the two
answers to each other directly, over the committed vendor slices. Each backend gets its own
`MarketDataService`, so equality means the two computed the same thing rather than sharing a
memoised one. The HTTP backend's transport is a `TestClient`, which is an `httpx.Client`:
real requests, real routers, real response models, no socket.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from mdq.dashboard.backend import Backend, EmbeddedBackend, FindingQuery, HttpBackend
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.context import ACTIVITY_SCHEMA, A, Regime
from support.api import PARQUET_FIXTURES, service_for
from support.dashboard import embedded_backend, http_backend, service_dir

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The three committed vendor slices, copied once for the module."""
    return service_dir(tmp_path_factory.mktemp("dashboard"), *PARQUET_FIXTURES)


@pytest.fixture(scope="module")
def embedded(data_dir: Path) -> EmbeddedBackend:
    """The in-process backend, over its own service."""
    return embedded_backend(service_for(data_dir))


@pytest.fixture(scope="module")
def over_http(data_dir: Path) -> HttpBackend:
    """The HTTP backend, over a second, independent service."""
    return http_backend(service_for(data_dir))


@pytest.fixture(params=["embedded", "http"])
def backend(request: pytest.FixtureRequest, embedded: Backend, over_http: Backend) -> Backend:
    """Each backend in turn, for the assertions that must hold of both."""
    return embedded if request.param == "embedded" else over_http


# --------------------------------------------------------------------------- #
# the same assertions, over both
# --------------------------------------------------------------------------- #


def test_the_inventory_names_both_instruments_and_both_frequencies(backend: Backend) -> None:
    summary = backend.summary()
    assert summary.contracts == ["CLG26", "ESH26"]
    assert {item.frequency for item in summary.by_frequency} == set(Frequency)
    assert summary.bars == 16_041


def test_every_instrument_carries_its_span_and_its_frequencies(backend: Backend) -> None:
    contracts = {item.contract: item for item in backend.contracts()}
    assert contracts["ESH26"].frequencies == [Frequency.DAILY, Frequency.MINUTE]
    assert contracts["CLG26"].frequencies == [Frequency.DAILY]
    assert contracts["CLG26"].first_session == date(2018, 1, 22)


def test_the_daily_severity_split_is_dominated_by_expected_findings(backend: Backend) -> None:
    """The reason the findings table opens above INFO, on this very fixture set."""
    counts = backend.quality_summary(Frequency.DAILY).findings_by_severity
    assert counts == {"ERROR": 0, "WARNING": 6, "INFO": 2_096}


def test_the_default_findings_view_returns_only_what_needs_a_human(backend: Backend) -> None:
    page = backend.findings(FindingQuery(severity="WARNING", limit=100))
    assert page.row_count == 6
    assert {finding.check_id for finding in page.findings} == {"invalid_ohlc"}
    assert all(finding.severity == "WARNING" for finding in page.findings)


def test_asking_for_everything_returns_the_expected_findings_too(backend: Backend) -> None:
    page = backend.findings(FindingQuery(limit=5_000))
    assert page.row_count == 2_102
    assert {f.check_id for f in page.findings} >= {"stale_bar", "missing_session"}


def test_findings_page_and_report_their_next_offset(backend: Backend) -> None:
    page = backend.findings(FindingQuery(limit=10))
    assert page.row_count == 10
    assert page.next_offset == 10


def test_the_insights_explain_the_settlement_and_holiday_patterns(backend: Backend) -> None:
    found = {item.id: item for item in backend.insights(Frequency.DAILY).insights}
    assert "carried_forward_settlement" in found
    assert "missing_sessions_are_holidays" in found
    assert found["dormancy_explains_staleness"].rule.confidence > 0.99
    assert found["carried_forward_settlement"].rule.kind in {"cleansing", "validation"}


def test_every_insight_carries_a_rule_a_user_could_paste(backend: Backend) -> None:
    for insight in backend.insights(Frequency.DAILY).insights:
        assert insight.pattern
        assert insight.rule.rule_id
        assert insight.rule.rationale
        assert 0.0 <= insight.rule.confidence <= 1.0


def test_the_activity_profile_classifies_every_session(backend: Backend) -> None:
    activity = backend.activity(Frequency.DAILY)
    assert activity.schema == ACTIVITY_SCHEMA
    assert activity.height == 2_663
    assert set(activity.get_column(A.REGIME).unique().to_list()) == {r.value for r in Regime}


def test_bars_come_back_typed_whichever_backend_answered(backend: Backend) -> None:
    frame = backend.bars(Frequency.DAILY, ["ESH26"], date(2026, 3, 2), date(2026, 3, 6))
    assert frame.schema[C.TS_UTC] == pl.Datetime("us", "UTC")
    assert frame.schema[C.SESSION_DATE] == pl.Date
    assert frame.get_column(C.SESSION_DATE).to_list()[0] == date(2026, 3, 2)


def test_a_selection_that_matches_nothing_is_an_empty_frame_not_an_error(
    backend: Backend,
) -> None:
    """The dashboard's empty states depend on this being 200-with-nothing, not a failure."""
    frame = backend.bars(Frequency.DAILY, ["ESH26"], date(1990, 1, 1), date(1990, 1, 2))
    assert frame.height == 0
    assert frame.columns[:2] == ["row_id", "contract"]


def test_an_upload_is_reported_on_in_full(backend: Backend, tmp_path: Path) -> None:
    csv = b"contract,timestamp,open,high,low,close,volume\nZZZ1,2026-03-02,1,2,0.5,1.5,10\n"
    report = backend.upload("hand_made.csv", csv)
    assert report.ingest.rows_in == 1
    assert report.ingest.rows_out == 1
    assert report.frequency is Frequency.DAILY
    assert report.quality.frequency is Frequency.DAILY


# --------------------------------------------------------------------------- #
# and the two against each other
# --------------------------------------------------------------------------- #


def test_the_two_backends_agree_about_what_is_loaded(embedded: Backend, over_http: Backend) -> None:
    assert embedded.summary() == over_http.summary()
    assert embedded.contracts() == over_http.contracts()


def test_the_two_backends_agree_about_quality_and_insights(
    embedded: Backend, over_http: Backend
) -> None:
    assert embedded.quality_summary() == over_http.quality_summary()
    assert embedded.insights() == over_http.insights()
    assert embedded.findings(FindingQuery(limit=500)) == over_http.findings(FindingQuery(limit=500))


def test_the_two_backends_agree_bar_for_bar(embedded: Backend, over_http: Backend) -> None:
    """Including dtypes: JSON lost them on the way out and `conform` put them back."""
    selection = (Frequency.DAILY, ["ESH26"], date(2025, 1, 1), date(2026, 3, 20))
    assert embedded.bars(*selection).equals(over_http.bars(*selection))


def test_the_two_backends_agree_about_every_analytic(embedded: Backend, over_http: Backend) -> None:
    for name, params in (("daily_bars", None), ("rolling_vwap", {"window": "15m"})):
        mine = embedded.analytic(name, Frequency.MINUTE, ["ESH26"], params=params, limit=2_000)
        theirs = over_http.analytic(name, Frequency.MINUTE, ["ESH26"], params=params, limit=2_000)
        assert mine.equals(theirs), name


def test_the_two_backends_agree_about_the_activity_map(
    embedded: Backend, over_http: Backend
) -> None:
    """The HTTP one rebuilds it from bars using the server's own thresholds; it must match."""
    assert embedded.activity(Frequency.DAILY).equals(over_http.activity(Frequency.DAILY))
