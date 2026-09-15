"""The app itself: four pages, one sidebar, one backend — assembled but not launched.

Separate from `app.py` so that it can be *imported*. `app.py` is the file Streamlit runs,
and importing a Streamlit script runs it; keeping the assembly here means the navigation,
the page order and the backend wiring are all reachable from a test, and `app.py` is left
with nothing in it that could be wrong.

Two ways to run the result:

* `make ui` — `streamlit run … -- --embedded`. The service runs in this process, so the
  whole platform is one command with no port to coordinate.
* `make api` in one terminal, `make ui-http` in another (or `make dev` for both). The
  dashboard is then an ordinary HTTP client of the documented API — what a real deployment
  looks like, and what `HttpBackend` exists to demonstrate.

`build_backend` reads `sys.argv` and the environment to decide; no page knows which it got.
"""

from __future__ import annotations

from typing import Final

import streamlit as st

from mdq.dashboard import cache, sidebar
from mdq.dashboard.backend import Backend
from mdq.dashboard.pages import analytics, data_quality, insights, overview

__all__ = ["TITLE", "pages", "render"]

TITLE: Final = "Market Data Quality & Analytics"


def pages(backend: Backend) -> list[st.Page]:
    """The navigation, in the order a user should meet it.

    Overview first because it is the triage — the handful of findings that need a person,
    and the thousands that do not. Insights last because it is the conclusion, and it reads
    better once the findings behind it have been seen.
    """
    return [
        st.Page(
            lambda: overview.render(backend),
            title="Overview",
            icon=":material/dashboard:",
            url_path="overview",
            default=True,
        ),
        st.Page(
            lambda: analytics.render(backend),
            title="Price analytics",
            icon=":material/show_chart:",
            url_path="analytics",
        ),
        st.Page(
            lambda: data_quality.render(backend),
            title="Data quality",
            icon=":material/rule:",
            url_path="data-quality",
        ),
        st.Page(
            lambda: insights.render(backend),
            title="Insights",
            icon=":material/lightbulb:",
            url_path="insights",
        ),
    ]


def render(backend: Backend | None = None) -> None:
    """Configure the app, pick a backend once, and run the selected page.

    Args:
        backend: Used as given — how a test drives the whole app against the committed
            fixtures. When omitted, the process-wide backend is built from the command
            line and the environment and cached for every session.
    """
    st.set_page_config(page_title=TITLE, page_icon="📈", layout="wide")
    chosen = backend if backend is not None else cache.get_backend()
    navigation = st.navigation(pages(chosen))
    sidebar.render(chosen)
    navigation.run()
