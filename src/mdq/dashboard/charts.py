"""Plotly figures, built from frames and returned — no Streamlit, no fetching.

Every function here takes a Polars frame that `mdq.dashboard.backend` already shaped and
returns a `go.Figure`. That split is what lets a chart be checked without a script runner:
a test asserts on the traces, the axis titles and the colours, which is where a charting
bug actually lives, instead of asserting that *a* chart was drawn.

Three conventions run through all of them.

**Colour means severity and nothing else.** Red is an error, amber a warning, grey an
expected artefact. No chart uses colour for a second purpose, because a business user who
has learned "grey is fine" on one page must not meet grey meaning "CL" on the next.

**An empty frame still produces a figure.** It carries a centred sentence saying what is
missing rather than an empty set of axes, so a page never has to choose between drawing a
chart and explaining its absence.

**Nothing is invented.** A null VWAP leaves a hole in the line (`connectgaps=False`); a
month a contract was not listed in is a blank heatmap cell, not a zero. A chart that
interpolates over missing data is the exact failure this whole tool exists to catch.
"""

from __future__ import annotations

from typing import Any, Final

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from mdq.analytics.daily_bars import BAR_COUNT
from mdq.analytics.vwap import BARS_IN_WINDOW, VWAP
from mdq.dashboard.backend import HeatmapData
from mdq.domain.schema import C

__all__ = [
    "SEVERITY_COLOURS",
    "candlestick",
    "coverage_bars",
    "empty_figure",
    "findings_by_check",
    "gap_timeline",
    "regime_heatmap",
    "vwap_chart",
]

#: Severity is the only thing colour ever encodes in this dashboard.
SEVERITY_COLOURS: Final[dict[str, str]] = {
    "ERROR": "#b3261e",
    "WARNING": "#c77700",
    "INFO": "#8a8f98",
}

#: A candle that closed up, and one that closed down.
_UP: Final = "#1f7a4d"
_DOWN: Final = "#b3261e"

#: Two blues: "nothing traded" through to "traded normally all month".
_ACTIVITY_SCALE: Final = [(0.0, "#eef1f5"), (0.5, "#7aa6d2"), (1.0, "#0b3d66")]

_MARGIN: Final = {"l": 10, "r": 10, "t": 40, "b": 10}


def _layout(figure: go.Figure, title: str, height: int) -> go.Figure:
    """The house style: a title, a sensible height, and no chart junk."""
    figure.update_layout(
        title=title,
        height=height,
        margin=_MARGIN,
        hovermode="closest",
        plot_bgcolor="rgba(0,0,0,0)",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
    )
    figure.update_xaxes(showgrid=True, gridcolor="rgba(0,0,0,0.07)")
    figure.update_yaxes(showgrid=True, gridcolor="rgba(0,0,0,0.07)")
    return figure


def empty_figure(message: str, height: int = 240) -> go.Figure:
    """A figure that says why there is nothing to see.

    Returned instead of `None` so a caller never has to branch: the page draws whatever it
    is given, and the reason lands where the chart would have been rather than in a
    footnote somewhere else on the screen.
    """
    figure = go.Figure()
    figure.add_annotation(
        text=message,
        showarrow=False,
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
        font={"size": 14, "color": "#6b7280"},
    )
    figure.update_layout(
        height=height,
        margin=_MARGIN,
        xaxis={"visible": False},
        yaxis={"visible": False},
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return figure


def findings_by_check(totals: pl.DataFrame) -> go.Figure:
    """Horizontal stacked bars: what each check found, split by how serious it is.

    Horizontal because check names are sentences ("Settlement close outside the bar's own
    range"), and a log x-axis because the counts span four orders of magnitude on the real
    corpus — 14,152 stale bars beside 43 incoherent ones would otherwise render the thing
    that matters as a line one pixel wide.
    """
    if totals.height == 0:
        return empty_figure("No quality findings — every check passed on this data.")
    figure = go.Figure()
    order = [row["check"] for row in totals.unique(subset=["check"], keep="first").to_dicts()]
    for severity, colour in SEVERITY_COLOURS.items():
        part = totals.filter(pl.col("severity") == severity)
        if part.height == 0:
            continue
        figure.add_bar(
            y=part.get_column("check").to_list(),
            x=part.get_column("findings").to_list(),
            name=_severity_label(severity),
            orientation="h",
            marker_color=colour,
            hovertemplate="%{y}<br>%{x:,} finding(s), "
            + _severity_label(severity)
            + "<extra></extra>",
        )
    figure.update_layout(barmode="stack")
    figure.update_yaxes(categoryorder="array", categoryarray=list(reversed(order)))
    figure.update_xaxes(title="Findings (log scale)", type="log")
    return _layout(figure, "What the checks found", 40 * len(order) + 140)


def _severity_label(severity: str) -> str:
    """The reader's word for a severity."""
    return {"ERROR": "Error", "WARNING": "Warning", "INFO": "Expected"}.get(severity, severity)


def regime_heatmap(data: HeatmapData) -> go.Figure:
    """Contract x month, shaded by the share of sessions that traded normally.

    This is the picture behind the whole severity model: a deferred contract is a dark row
    for years and then brightens a few months before expiry, and every "stale bar" inside
    the dark part is a settlement print on a day nobody traded — correct data, not a
    defect. Seeing that is what stops a user asking why 47% of the corpus is "flagged".
    """
    if data.is_empty:
        return empty_figure("No sessions to classify yet.")
    figure = go.Figure(
        go.Heatmap(
            z=data.active_share,
            x=data.months,
            y=data.contracts,
            customdata=data.hover,
            hovertemplate="%{customdata}<extra></extra>",
            colorscale=_ACTIVITY_SCALE,
            zmin=0.0,
            zmax=1.0,
            colorbar={"title": "Share of<br>sessions<br>trading", "tickformat": ".0%"},
            xgap=1,
            ygap=1,
        )
    )
    figure.update_xaxes(title="Month")
    figure.update_yaxes(title="Instrument", autorange="reversed")
    return _layout(
        figure, "How much each instrument actually traded", 28 * len(data.contracts) + 180
    )


def candlestick(bars: pl.DataFrame, contract: str) -> go.Figure:
    """Daily candles over a volume strip — the ordinary way a trader reads a series."""
    if bars.height == 0:
        return empty_figure(f"No {contract} bars in the selected dates.", height=420)
    sessions = bars.get_column(C.SESSION_DATE).to_list()
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.75, 0.25],
        vertical_spacing=0.04,
    )
    figure.add_trace(
        go.Candlestick(
            x=sessions,
            open=bars.get_column(C.OPEN).to_list(),
            high=bars.get_column(C.HIGH).to_list(),
            low=bars.get_column(C.LOW).to_list(),
            close=bars.get_column(C.CLOSE).to_list(),
            name=contract,
            increasing_line_color=_UP,
            decreasing_line_color=_DOWN,
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Bar(
            x=sessions,
            y=bars.get_column(C.VOLUME).to_list(),
            name="Volume",
            marker_color="#9aa5b1",
            hovertemplate="%{x|%Y-%m-%d}<br>volume %{y:,}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    figure.update_xaxes(rangeslider_visible=False, row=1, col=1)
    figure.update_xaxes(title="Trading session", row=2, col=1)
    figure.update_yaxes(title="Price", row=1, col=1)
    figure.update_yaxes(title="Volume", row=2, col=1)
    return _layout(figure, f"{contract} — daily bars as published by the vendor", 520)


def vwap_chart(series: pl.DataFrame, contract: str, session: Any, window: str) -> go.Figure:
    """Close against a rolling VWAP, over a strip showing how thin each window was.

    The strip is the point. A "15-minute VWAP" that averaged one bar is a price, not an
    average, and the only honest way to show it is to show the bar count underneath —
    which is also what makes a gap in an otherwise liquid session jump off the page.
    """
    if series.height == 0:
        return empty_figure(f"No {contract} minute bars for {session}.", height=420)
    clock = series.get_column(C.TS_LOCAL).to_list()
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.72, 0.28],
        vertical_spacing=0.05,
    )
    figure.add_trace(
        go.Scatter(
            x=clock,
            y=series.get_column(C.CLOSE).to_list(),
            name="Close",
            mode="lines",
            line={"color": "#9aa5b1", "width": 1},
            connectgaps=False,
            hovertemplate="%{x|%H:%M}<br>close %{y:,.2f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=clock,
            y=series.get_column(VWAP).to_list(),
            name=f"{window} VWAP",
            mode="lines",
            line={"color": "#0b3d66", "width": 2},
            connectgaps=False,
            hovertemplate="%{x|%H:%M}<br>VWAP %{y:,.2f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    counts = series.get_column(BARS_IN_WINDOW).to_list()
    figure.add_trace(
        go.Scatter(
            x=clock,
            y=counts,
            name="Bars in the window",
            mode="lines",
            line={"color": "#c77700", "width": 0.5},
            fill="tozeroy",
            fillcolor="rgba(199,119,0,0.20)",
            hovertemplate="%{x|%H:%M}<br>%{y} bar(s) in the window<extra></extra>",
        ),
        row=2,
        col=1,
    )
    figure.update_xaxes(title="Chicago wall clock", row=2, col=1)
    figure.update_yaxes(title="Price", row=1, col=1)
    figure.update_yaxes(title="Bars", rangemode="tozero", row=2, col=1)
    return _layout(figure, f"{contract} — {session}: close against a {window} VWAP", 520)


def gap_timeline(gaps: pl.DataFrame) -> go.Figure:
    """One lane per instrument, with every hole in its data drawn to scale.

    Holes come in two kinds and the chart keeps them apart: a bar of real width is minutes
    missing inside a session that was otherwise trading, and a full-session block is a day
    with no bars at all. Both are drawn in their finding's own severity colour, so a strip
    that is entirely grey is a holiday calendar and a strip with an amber bar in it is a
    feed problem.
    """
    if gaps.height == 0:
        return empty_figure("No gaps in the data for this selection.")
    lanes = sorted({str(value) for value in gaps.get_column(C.CONTRACT).to_list()})
    figure = go.Figure()
    seen: set[str] = set()
    for row in gaps.to_dicts():
        severity = str(row["severity"])
        span_ms = (row["to_utc"] - row["from_utc"]).total_seconds() * 1_000
        figure.add_bar(
            x=[span_ms],
            y=[str(row[C.CONTRACT])],
            base=[row["from_utc"]],
            orientation="h",
            marker_color=SEVERITY_COLOURS.get(severity, "#8a8f98"),
            name=_severity_label(severity),
            legendgroup=severity,
            showlegend=severity not in seen,
            hovertemplate=f"{row['what']}<extra></extra>",
            width=0.6,
        )
        seen.add(severity)
    figure.update_layout(barmode="overlay", bargap=0.35)
    figure.update_xaxes(title="When", type="date")
    figure.update_yaxes(
        title="Instrument", categoryorder="array", categoryarray=list(reversed(lanes))
    )
    return _layout(figure, "Where the data is missing", 46 * len(lanes) + 160)


def coverage_bars(coverage: pl.DataFrame, expected: int = 1_380) -> go.Figure:
    """How many minute bars each session actually holds, against a full CME session.

    A truncated session is invisible in a price chart and fatal to an intraday average, so
    it gets its own small chart rather than a footnote: 2026-03-02 in the committed sample
    holds 960 of 1,380 minutes, and a reader should see that before trusting a VWAP on it.
    """
    if coverage.height == 0:
        return empty_figure("No minute sessions for this instrument.", height=220)
    sessions = coverage.get_column(C.SESSION_DATE).to_list()
    counts = coverage.get_column(BAR_COUNT).to_list()
    figure = go.Figure(
        go.Bar(
            x=sessions,
            y=counts,
            marker_color=[_UP if count >= expected * 0.95 else "#c77700" for count in counts],
            hovertemplate="%{x|%Y-%m-%d}<br>%{y:,} minute bar(s)<extra></extra>",
            name="Minute bars",
        )
    )
    figure.add_hline(
        y=expected,
        line={"color": "#6b7280", "dash": "dot", "width": 1},
        annotation_text=f"a full session is {expected:,} minutes",
        annotation_position="top left",
    )
    figure.update_xaxes(title="Trading session")
    figure.update_yaxes(title="Minute bars")
    return _layout(figure, "Minute coverage per session", 260)
