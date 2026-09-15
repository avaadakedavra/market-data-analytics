"""The few widgets and phrases every page shares.

Small on purpose. Anything here is presentation — a label, a spinner, an empty state — and
anything that decides *what* is shown lives in `mdq.dashboard.backend`. The reason it
exists at all is consistency of language: "trading session" and not "date", "instrument"
and not "contract code", one spelling of the warning that precedes a five-second minute
pass, and one shape of error banner, so the dashboard reads as though one person wrote it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Final

import streamlit as st

from mdq.api.schemas import DatasetsSummaryResponse
from mdq.dashboard.backend import BackendError
from mdq.domain.frequency import Frequency

__all__ = [
    "FREQUENCY_LABELS",
    "MINUTE_WARNING",
    "frequency_picker",
    "guard",
    "metric_row",
    "nothing_loaded",
]

#: How the two frequencies are named to a reader who does not work here.
FREQUENCY_LABELS: Final[dict[Frequency, str]] = {
    Frequency.DAILY: "Daily bars",
    Frequency.MINUTE: "Minute bars",
}

#: Said before anything that triggers a minute-frequency quality pass. Measured: 5.2 s the
#: first time on the full corpus, and ~1.3 GB of memory once the bars materialise. A user
#: who is told that waits; a user who is not told assumes the page has hung.
MINUTE_WARNING: Final = (
    "Checking minute bars reads the whole minute history — about five seconds the first "
    "time on a full dataset, then instant. Daily bars are the faster view and are what the "
    "vendor publishes diagnostics for."
)


def frequency_picker(
    summary: DatasetsSummaryResponse,
    *,
    key: str,
    label: str = "Bar frequency",
) -> Frequency | None:
    """A radio over the frequencies actually loaded, defaulting to daily.

    Returns `None` when nothing is loaded, which every caller renders as an empty state
    rather than as an error: an empty service is a perfectly valid state (the API starts
    before `make fetch` has ever run) and must not look like a failure.
    """
    available = [item.frequency for item in summary.by_frequency]
    if not available:
        return None
    ordered = [f for f in Frequency if f in available]
    return st.radio(
        label,
        ordered,
        index=0,
        horizontal=True,
        format_func=lambda f: FREQUENCY_LABELS[f],
        key=key,
    )


def guard[T](work: Callable[[], T], *, context: str) -> T | None:
    """Run `work`, turning a backend failure into a banner instead of a stack trace.

    A page is a place a non-engineer is looking at. When the API is down, or a contract
    was renamed, or an upload was too large, they should read a sentence about it in the
    place the answer would have been — not a red Streamlit traceback quoting httpx.
    """
    try:
        return work()
    except BackendError as exc:
        st.error(f"{context}\n\n{exc}")
        return None


def nothing_loaded(data_hint: str = "") -> None:
    """The empty state for a service holding no data at all."""
    st.info(
        "No market data is loaded yet. Run `make fetch` to download the sample, or upload "
        "a CSV or parquet file in the sidebar. " + data_hint
    )


def metric_row(items: Sequence[tuple[str, Any, str | None]]) -> None:
    """A row of headline numbers, each with an optional one-line explanation."""
    columns = st.columns(len(items))
    for column, (label, value, help_text) in zip(columns, items, strict=True):
        column.metric(label, value, help=help_text)
