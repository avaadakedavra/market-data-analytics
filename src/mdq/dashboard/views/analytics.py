"""Analytics — one instrument, its daily history, and one session in minute detail.

Two zoom levels, because those are the two questions a desk asks. *How has this contract
traded?* is a candlestick over daily bars. *What happened inside that session?* is the
close against a rolling 15-minute VWAP, over a strip showing how many bars each window
actually averaged.

That strip is not decoration. A time-based VWAP over sparse minute bars can average a
single print and still be called a "15-minute average"; corpus-wide minute coverage is
a median of 222 bars per contract-day against 1,380 possible, so this is the normal case rather
than the exception. Showing the window's bar count is the difference between a number a
risk manager can use and one they cannot.

The page also states, rather than hides, the one cross-frequency difference in this data:
rebuilding a session from minute bars reproduces the vendor's open, high and low exactly on
liquid sessions and its close on **none** of them, because the vendor's daily close is a
settlement price. That is a convention, not a bug, and the reconciliation table says so in
those words.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import streamlit as st

from mdq.analytics.daily_bars import BAR_COUNT
from mdq.analytics.vwap import BARS_IN_WINDOW, VWAP
from mdq.dashboard import cache, certificate, charts, ui
from mdq.dashboard.backend import Backend, reconciliation
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C

__all__ = ["render"]

#: VWAP windows offered. 15 minutes is the default PLAN §3.5 asks for; the others are there
#: because the honest answer to "is this average thin?" changes with the window.
_WINDOWS = ("5m", "15m", "30m", "1h")


def render(backend: Backend) -> None:
    """Draw the Analytics page."""
    summary = ui.guard(lambda: cache.summary(backend), context="Could not read what is loaded.")
    if summary is None:
        return
    ui.certificate_head(summary, "Price analytics", 2)
    st.title("Price analytics")
    st.caption("Daily history for one instrument, and one trading session minute by minute.")

    instruments = ui.guard(
        lambda: cache.contracts(backend), context="Could not list the instruments."
    )
    if instruments is None:
        return
    if not instruments:
        ui.nothing_loaded()
        ui.signature(backend.label, loaded=False)
        return

    codes = [item.contract for item in instruments]
    chosen = st.selectbox("Instrument", codes, key="analytics_contract")
    info = next(item for item in instruments if item.contract == chosen)

    start, end = _date_range(chosen, info.first_session, info.last_session)
    if start is None or end is None:
        st.info("Pick a start and an end date to draw the chart.")
        return
    if start > end:
        st.warning("The start date is after the end date — nothing to draw.")
        return

    _daily(backend, chosen, start, end)
    st.divider()
    if Frequency.MINUTE in info.frequencies:
        _intraday(backend, chosen, start, end)
    else:
        st.subheader(f"{chosen} — inside one session")
        st.info(
            f"No minute bars are loaded for {chosen}. Minute data covers a subset of the "
            "instruments; the daily chart above is the whole of what is known about this one."
        )


def _date_range(
    contract: str, first: date | None, last: date | None
) -> tuple[date | None, date | None]:
    """A date-range picker bounded by the instrument's own history.

    Bounded deliberately: offering a user a date the instrument did not exist on is an
    invitation to an empty chart they cannot explain. The widget key carries the contract
    for the same reason — Streamlit remembers a widget's value by key, so a shared key
    would carry one instrument's dates onto the next one and silently produce an empty
    chart for an instrument that trades perfectly well.
    """
    if first is None or last is None:
        return None, None
    default_start = max(first, _minus_year(last))
    picked = st.date_input(
        "Trading sessions (both ends included)",
        value=(default_start, last),
        min_value=first,
        max_value=last,
        format=ui.DATE_FORMAT,
        key=f"analytics_dates_{contract}",
        help="Day/month/year. These are Chicago trading sessions — the session that rolls "
        f"at 17:00 CT — bounded by {contract}'s own history.",
    )
    if isinstance(picked, date):
        return picked, picked
    if len(picked) == 2:
        return picked[0], picked[1]
    return (picked[0], None) if picked else (None, None)


def _minus_year(day: date) -> date:
    """A year before `day`, clamped through a leap day rather than raising on one."""
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=day.day - 1)


def _daily(backend: Backend, contract: str, start: date, end: date) -> None:
    """The daily candlestick and the numbers under it."""
    st.subheader(f"{contract} — daily")
    bars = ui.guard(
        lambda: cache.bars(backend, Frequency.DAILY, [contract], start, end),
        context="Could not read the daily bars.",
    )
    if bars is None:
        return
    if bars.height == 0:
        st.info(
            f"No {contract} daily bars between {ui.au_date(start)} and {ui.au_date(end)}. "
            "Widen the dates, or pick another instrument."
        )
        return
    ordered = bars.sort(C.SESSION_DATE)
    st.caption(
        "Bars as published by the vendor, not rebuilt from minutes — the distinction "
        "matters, because the vendor's daily close is a settlement price. A hollow candle "
        "closed up and a filled one closed down; colour on this page means severity."
    )
    st.plotly_chart(
        charts.candlestick(ordered, contract),
        width="stretch",
        key="analytics_candles",
    )
    traded = ordered.filter(pl.col(C.VOLUME) > 0).height
    # "Sessions that traded" is the figure that decides whether anything else on this page
    # can be trusted, so it gets the bar: the share of the range that was genuinely active.
    certificate.render_schedule(
        [
            certificate.ScheduleRow(
                label="Sessions that traded",
                observed=f"{traded:,}",
                of_total=f"of {ordered.height:,} sessions",
                note=(
                    "Bars with non-zero volume. The rest are settlement-only prints, which "
                    "are correct data and not defects."
                ),
                ratio=traded / ordered.height if ordered.height else None,
            ),
            certificate.ScheduleRow(
                label="Total volume",
                observed=f"{int(ordered.get_column(C.VOLUME).fill_null(0).sum()):,}",
                of_total="contracts",
                note="Summed daily volume over the selected range.",
            ),
        ]
    )


def _intraday(backend: Backend, contract: str, start: date, end: date) -> None:
    """The session picker, the VWAP chart and the cross-frequency reconciliation."""
    st.subheader(f"{contract} — inside one session")
    coverage = ui.guard(
        lambda: cache.coverage(backend, contract, start, end),
        context="Could not read the minute sessions.",
    )
    if coverage is None:
        return
    if coverage.height == 0:
        st.info(
            f"No {contract} minute bars fall in the selected dates. Widen the range above "
            "to reach a session with minute data."
        )
        return

    sessions = coverage.get_column(C.SESSION_DATE).to_list()
    counts = dict(zip(sessions, coverage.get_column(BAR_COUNT).to_list(), strict=True))
    session = st.selectbox(
        "Trading session",
        sessions,
        index=len(sessions) - 1,
        format_func=lambda day: f"{ui.au_date(day)}  ({counts[day]:,} minute bars)",
        key="analytics_session",
    )
    # A segmented control, not a slider. Four named windows are four discrete choices, and
    # a slider affords a continuous drag it cannot honour: the handle moves through dead
    # space and nothing changes until it crosses the next stop, so the control lies about
    # what it does. `required` keeps a window always selected — deselecting the last option
    # would hand `None` to the VWAP call.
    window = st.segmented_control(
        "VWAP window",
        _WINDOWS,
        default="15m",
        required=True,
        key="analytics_window",
        help="The averaging window. A shorter window averages fewer bars, so the thin-window "
        "count below it rises.",
    )

    if counts[session] < 1_300:
        st.warning(
            f"{ui.au_date(session)} holds {counts[session]:,} minute bars; a full CME session "
            "is 1,380. "
            "The average below is computed over what is there, and the strip under the "
            "chart shows where it is thin.",
            icon=":material/warning:",
        )

    series = ui.guard(
        lambda: cache.vwap_series(backend, contract, session, window),
        context="Could not compute the VWAP.",
    )
    if series is None:
        return
    st.caption(
        f"{ui.au_date(session)} — close against a {window} VWAP, over the bar count each "
        "window averaged."
    )
    st.plotly_chart(
        charts.vwap_chart(series, contract, session, window),
        width="stretch",
        key="analytics_vwap",
    )
    _thin_note(series)

    st.subheader("Minute coverage per session")
    st.caption(
        "How many minute bars each session actually holds. The dotted reference rule is "
        "1,380 minutes — a full CME session, and the one drawn tolerance limit in this "
        "dashboard, because it is the one place a real threshold exists. A bar short of it "
        "is a truncated session, which is invisible in a price chart and fatal to an "
        "intraday average."
    )
    st.plotly_chart(charts.coverage_bars(coverage), width="stretch", key="analytics_coverage")
    _reconciliation(backend, contract, session, coverage)


def _thin_note(series: pl.DataFrame) -> None:
    """Say plainly how much of the session's average rested on almost no data."""
    if series.height == 0 or BARS_IN_WINDOW not in series.columns:
        return
    thin = series.filter(pl.col(BARS_IN_WINDOW) < 5).height
    missing = series.filter(pl.col(VWAP).is_null()).height
    st.caption(
        f"{thin:,} of {series.height:,} windows averaged fewer than five bars"
        + (
            f"; {missing:,} produced no average at all because nothing traded in them "
            "(the line breaks rather than being filled in)."
            if missing
            else "."
        )
    )


def _reconciliation(backend: Backend, contract: str, session: date, coverage: pl.DataFrame) -> None:
    """Vendor daily bar against the same session rebuilt from minutes."""
    with st.expander("Compare this session with the vendor's daily bar"):
        st.caption(
            "Open, high and low are rebuilt from the minute file and should match. The "
            "close is expected to differ: the vendor publishes a **settlement price**, "
            "which is set by the exchange and is not the last trade of the session."
        )
        vendor = ui.guard(
            lambda: cache.bars(backend, Frequency.DAILY, [contract], session, session),
            context="Could not read the vendor daily bar.",
        )
        if vendor is None:
            return
        derived = coverage.filter(pl.col(C.SESSION_DATE) == session)
        rows = reconciliation(
            vendor.to_dicts()[0] if vendor.height else None,
            derived.to_dicts()[0] if derived.height else None,
        )
        if not rows:
            st.info(f"No vendor daily bar is loaded for {contract} on {ui.au_date(session)}.")
            return
        st.dataframe(rows, width="stretch", hide_index=True)
