"""Each page, rendered for real by Streamlit's script runner.

These are smoke tests by design (PLAN §9: "Streamlit `AppTest` can be brittle; keep smoke
tests minimal and logic in the `Backend`, not in pages"). What they are worth is exactly
what they assert: that a page runs end to end without raising, that it puts the *right*
headline in front of a reader, and that its empty states are real states rather than blank
screens or tracebacks. Everything a page computes is asserted in
`tests/unit/test_dashboard_backend.py`, where a failure names a function.

`AppTest.from_function` runs `render(backend=…)` inside a script runner, so no page needs to
exist as a Streamlit script and no navigation has to be faked. The backend is the embedded
one over the committed fixtures — a page cannot tell the difference, and
`test_dashboard_backends.py` is what proves that.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from mdq.dashboard.backend import Backend, EmbeddedBackend
from support.api import PARQUET_FIXTURES, service_for
from support.dashboard import (
    FailingBackend,
    embedded_backend,
    page_test,
    service_dir,
    upload_test,
)
from support.fixtures import GENERIC_MALFORMED

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: Every page, so "does each one render?" is one parametrisation rather than four copies.
PAGES = ("overview", "analytics", "data_quality", "insights")

OVERVIEW = "mdq.dashboard.pages.overview"
ANALYTICS = "mdq.dashboard.pages.analytics"
DATA_QUALITY = "mdq.dashboard.pages.data_quality"
INSIGHTS = "mdq.dashboard.pages.insights"
SIDEBAR = "mdq.dashboard.sidebar"
SHELL = "mdq.dashboard.shell"


@pytest.fixture(autouse=True)
def _clean_caches() -> Iterator[None]:
    """Streamlit's caches are process-global; never let one page's answers reach another."""
    st.cache_data.clear()
    st.cache_resource.clear()
    yield
    st.cache_data.clear()
    st.cache_resource.clear()


@pytest.fixture(scope="module")
def loaded(tmp_path_factory: pytest.TempPathFactory) -> EmbeddedBackend:
    """A backend over the three committed vendor slices."""
    directory = service_dir(tmp_path_factory.mktemp("pages"), *PARQUET_FIXTURES)
    return embedded_backend(service_for(directory))


@pytest.fixture(scope="module")
def empty(tmp_path_factory: pytest.TempPathFactory) -> EmbeddedBackend:
    """A backend over a directory with nothing in it — the pre-`make fetch` state."""
    return embedded_backend(service_for(tmp_path_factory.mktemp("nothing")))


def _text(app: AppTest) -> str:
    """Everything the page put on screen, as one string."""
    parts = [
        *(item.value for item in app.markdown),
        *(item.value for item in app.title),
        *(item.value for item in app.subheader),
        *(item.value for item in app.caption),
        *(item.value for item in app.info),
        *(item.value for item in app.warning),
        *(item.value for item in app.error),
        *(item.value for item in app.success),
    ]
    return "\n".join(str(part) for part in parts)


# --------------------------------------------------------------------------- #
# every page renders
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", PAGES)
def test_every_page_renders_without_raising(name: str, loaded: Backend) -> None:
    app = page_test(f"mdq.dashboard.pages.{name}", loaded)
    assert not app.exception, app.exception
    assert app.title, f"{name} drew no title"


@pytest.mark.parametrize("name", PAGES)
def test_every_page_has_an_empty_state_rather_than_a_blank_screen(
    name: str, empty: Backend
) -> None:
    """With nothing loaded, every page must say so — and say what to do about it."""
    app = page_test(f"mdq.dashboard.pages.{name}", empty)
    assert not app.exception, app.exception
    assert any("No market data is loaded" in item.value for item in app.info)
    assert "make fetch" in _text(app)


@pytest.mark.parametrize("name", PAGES)
def test_every_page_turns_a_dead_backend_into_a_sentence(name: str) -> None:
    """The API being down must read as an explanation, never as a traceback."""
    app = page_test(f"mdq.dashboard.pages.{name}", FailingBackend())
    assert not app.exception, app.exception
    assert app.error, f"{name} showed nothing when the backend failed"
    assert "not answering" in app.error[0].value


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #


def test_overview_leads_with_the_triage_not_with_the_raw_total(loaded: Backend) -> None:
    app = page_test(OVERVIEW, loaded)
    labels = {item.label: item.value for item in app.metric}
    assert labels["Errors"] == "0"
    assert labels["Warnings"] == "6"
    assert labels["Expected"] == "2,096"
    assert "need a human" in _text(app)


def test_overview_draws_the_check_chart_and_the_activity_heatmap(loaded: Backend) -> None:
    app = page_test(OVERVIEW, loaded)
    keys = {chart.proto.id for chart in app.get("plotly_chart")}
    assert len(app.get("plotly_chart")) == 2
    assert keys  # both charts carry the keys the page gave them


def test_overview_defaults_to_daily_and_warns_before_the_minute_pass(loaded: Backend) -> None:
    app = page_test(OVERVIEW, loaded)
    picker = app.radio[0]
    assert picker.value.value == "daily"
    assert not app.warning

    after = picker.set_value(next(f for f in picker.options if "Minute" in str(f))).run(timeout=120)
    assert not after.exception, after.exception
    assert any("five seconds" in item.value for item in after.warning)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #


def test_analytics_charts_the_chosen_instrument(loaded: Backend) -> None:
    app = page_test(ANALYTICS, loaded)
    assert app.selectbox[0].value == "CLG26"
    assert "CLG26 — daily" in _text(app)
    assert app.get("plotly_chart")


def test_analytics_shows_the_intraday_view_for_an_instrument_with_minute_bars(
    loaded: Backend,
) -> None:
    app = page_test(ANALYTICS, loaded)
    chosen = app.selectbox[0].set_value("ESH26").run(timeout=120)
    assert not chosen.exception, chosen.exception
    text = _text(chosen)
    assert "ESH26 — inside one session" in text
    assert "settlement price" in text
    # daily candles, the VWAP pair, and the per-session coverage bars
    assert len(chosen.get("plotly_chart")) == 3


def test_analytics_says_so_when_an_instrument_has_no_minute_bars(loaded: Backend) -> None:
    app = page_test(ANALYTICS, loaded)
    assert any("No minute bars are loaded for CLG26" in item.value for item in app.info)
    assert not app.error


# --------------------------------------------------------------------------- #
# Data Quality
# --------------------------------------------------------------------------- #


def test_data_quality_hides_the_expected_findings_and_says_how_many(loaded: Backend) -> None:
    app = page_test(DATA_QUALITY, loaded)
    assert app.checkbox[0].value is False
    assert any("2,096 further finding(s) are hidden" in item.value for item in app.info)
    assert "Showing 6 finding(s)" in _text(app)


def test_data_quality_can_be_asked_for_the_expected_findings_too(loaded: Backend) -> None:
    app = page_test(DATA_QUALITY, loaded)
    after = app.checkbox[0].set_value(True).run(timeout=120)
    assert not after.exception, after.exception
    text = _text(after)
    # 2,102 findings exist; one page holds 2,000, and the page says so rather than
    # pretending the table is the whole answer.
    assert "Showing 2,000 finding(s)" in text
    assert "more are available beyond the first 2,000" in text
    # …and now the gap strip has something to draw.
    assert after.get("plotly_chart")


def test_data_quality_says_so_when_a_filter_matches_nothing(loaded: Backend) -> None:
    """`missing_value` runs on every daily bar and finds none — a clean answer, not a blank."""
    app = page_test(DATA_QUALITY, loaded)
    checks = next(item for item in app.multiselect if item.label == "Checks")
    after = checks.set_value(["missing_value"]).run(timeout=120)
    assert not after.exception, after.exception
    assert any("No findings match these filters" in item.value for item in after.success)


def test_data_quality_shows_the_evidence_behind_one_finding(loaded: Backend) -> None:
    app = page_test(DATA_QUALITY, loaded)
    assert app.get("json"), "the evidence expander rendered nothing"


# --------------------------------------------------------------------------- #
# Insights
# --------------------------------------------------------------------------- #


def test_insights_renders_one_card_per_pattern_with_a_copyable_rule(loaded: Backend) -> None:
    app = page_test(INSIGHTS, loaded)
    text = _text(app)
    assert "Suggested rule" in text
    blocks = [item.value for item in app.code]
    assert blocks, "no rule block was rendered"
    assert any(block.lstrip().startswith("{") for block in blocks), "no JSON block"
    assert any("rule_id:" in block for block in blocks), "no YAML block"


def test_insights_says_so_when_nothing_recurs(loaded: Backend) -> None:
    """The minute fixture produces findings but no pattern — a real answer, not a failure."""
    app = page_test(INSIGHTS, loaded)
    minute = (
        app.radio[0]
        .set_value(next(f for f in app.radio[0].options if "Minute" in str(f)))
        .run(timeout=120)
    )
    assert not minute.exception, minute.exception
    assert any("No recurring pattern was found" in item.value for item in minute.info)


# --------------------------------------------------------------------------- #
# the sidebar upload
# --------------------------------------------------------------------------- #


def test_the_sidebar_names_the_backend_and_offers_an_uploader(loaded: Backend) -> None:
    app = page_test(SIDEBAR, loaded)
    assert not app.exception, app.exception
    assert any("Reading from" in item.value for item in app.caption)
    assert app.get("file_uploader")


def test_an_uploaded_file_reports_its_rejects_and_its_findings(loaded: Backend) -> None:
    app = upload_test(loaded, GENERIC_MALFORMED.name, GENERIC_MALFORMED.read_bytes())
    assert not app.exception, app.exception
    text = _text(app)
    assert "could not be placed on the timeline" in text
    assert "Rows that became bars" in {item.label for item in app.metric}


def test_a_clean_upload_says_there_is_nothing_wrong_with_it(loaded: Backend) -> None:
    csv = b"contract,timestamp,open,high,low,close,volume\nZZZ1,2026-03-02,1,2,0.5,1.5,10\n"
    app = upload_test(loaded, "clean.csv", csv)
    assert not app.exception, app.exception
    assert any("No quality findings in your file" in item.value for item in app.info)


# --------------------------------------------------------------------------- #
# the whole app
# --------------------------------------------------------------------------- #


def test_the_whole_app_assembles_and_lands_on_the_overview(loaded: Backend) -> None:
    """Navigation, sidebar and the default page, run together the way a user meets them."""
    app = page_test(SHELL, loaded)
    assert not app.exception, app.exception
    assert app.title[0].value == "Market data overview"
    assert any("Reading from" in item.value for item in app.sidebar.caption)


def _navigation_script(backend):
    """A page's title is only resolvable inside a script run, so ask for it in one."""
    import streamlit as st

    from mdq.dashboard import shell

    for page in shell.pages(backend):
        st.text(page.title)


def test_the_navigation_lists_the_four_pages_in_reading_order(loaded: Backend) -> None:
    app = AppTest.from_function(
        _navigation_script, default_timeout=120, kwargs={"backend": loaded}
    ).run()
    assert not app.exception, app.exception
    assert [item.value for item in app.text] == [
        "Overview",
        "Price analytics",
        "Data quality",
        "Insights",
    ]
