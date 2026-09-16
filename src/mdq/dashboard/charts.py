"""Plotly figures, built from frames and returned — no Streamlit, no fetching.

Every function here takes a Polars frame that `mdq.dashboard.backend` already shaped and
returns a `go.Figure`. That split is what lets a chart be checked without a script runner:
a test asserts on the traces, the axis titles and the colours, which is where a charting
bug actually lives, instead of asserting that *a* chart was drawn.

Four conventions run through all of them.

**Colour means severity, or the reference standard, and nothing else.** Vermilion is out of
tolerance, amber a warning, grey an expected artefact, and reference blue marks anything
measured against the vendor's published diagnostics. No chart uses colour for a fifth
purpose, because a business user who has learned "grey is fine" on one page must not meet
grey meaning "CL" on the next. It is also why the candles are hollow-and-filled rather than
green-and-red: green/red for direction would collide with the severity vocabulary, and
hollow-for-up is the original candlestick convention anyway.

**Severity carries a hatch as well as a hue.** Every severity-coloured mark also takes its
ordered pattern from `theme.PATTERN_SHAPES` — cross-hatch for an error, diagonal for a
warning, stipple for an expected artefact — so the charts separate the three tiers when
printed in greyscale or read by someone with a colour vision deficiency.

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
from mdq.dashboard import theme
from mdq.dashboard.backend import HeatmapData
from mdq.domain.schema import C

__all__ = [
    "SEVERITY_COLOURS",
    "SEVERITY_PATTERNS",
    "candlestick",
    "coverage_bars",
    "empty_figure",
    "findings_by_check",
    "gap_timeline",
    "regime_heatmap",
    "vwap_chart",
]

#: Severity ink, shared with the state plates so one word means one colour everywhere.
SEVERITY_COLOURS: Final[dict[str, str]] = dict(theme.SEVERITY_INK)

#: The non-colour channel for the same three tiers.
SEVERITY_PATTERNS: Final[dict[str, str]] = dict(theme.PATTERN_SHAPES)

#: A candle that closed up is hollow; one that closed down is filled. Both are ink, which
#: leaves the whole colour vocabulary to severity and the reference standard.
_UP_FILL: Final = theme.STOCK
_DOWN_FILL: Final = theme.INK

#: The reference-blue ramp: "nothing traded" through to "traded normally all month". This is
#: a measurement, not a severity, which is why it is allowed the reference hue.
_ACTIVITY_SCALE: Final = [
    (0.0, theme.STOCK_SUNK),
    (0.5, "#7C97BE"),
    (1.0, theme.REFERENCE_DEEP),
]

_MARGIN: Final = {"l": 10, "r": 10, "t": 34, "b": 10}


def _layout(figure: go.Figure, height: int) -> go.Figure:
    """The house style: the certificate template, no title, and no chart junk.

    No title by design, and the parameter is gone rather than passed empty so the rule
    cannot be forgotten at one call site. Two reasons it belongs to the page instead:

    * A Plotly title and a horizontal legend occupy the same band above the plot, so a
      title long enough to reach the legend's first swatch collides with it — which is
      exactly what "CLG26 — daily bars as published by the vendor" did.
    * Every chart here sits under a page heading that already names it, and a heading is a
      ruled certificate clause. A second title in Plotly's own type is both a duplicate and
      the one piece of text on the page that is not set in the document's voice.
    """
    figure.update_layout(
        template=theme.register_template(),
        height=height,
        margin=_MARGIN,
        hovermode="closest",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
    )
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
        font={"family": theme.FONT_BODY, "size": 13, "color": theme.INK_MUTED},
    )
    figure.update_layout(
        template=theme.register_template(),
        height=height,
        margin=_MARGIN,
        xaxis={"visible": False},
        yaxis={"visible": False},
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
    # Ordered by total findings, largest first, and tie-broken by name. The previous order
    # came from row order in the frame, which meant the same data could arrange itself
    # differently on two screens — and a reader is meant to grasp this in seconds.
    order = [
        row["check"]
        for row in totals.group_by("check")
        .agg(pl.col("findings").sum().alias("total"))
        .sort(["total", "check"], descending=[True, False])
        .to_dicts()
    ]
    for severity, colour in SEVERITY_COLOURS.items():
        part = totals.filter(pl.col("severity") == severity)
        if part.height == 0:
            continue
        figure.add_bar(
            y=part.get_column("check").to_list(),
            x=part.get_column("findings").to_list(),
            name=_severity_label(severity),
            orientation="h",
            marker={
                "color": colour,
                "pattern": {
                    "shape": SEVERITY_PATTERNS[severity],
                    "fgcolor": theme.STOCK,
                    "size": 5,
                    "solidity": 0.28,
                    # Without this the pattern *replaces* the fill — Plotly's default —
                    # and the bar renders as pale hatching on transparent, which is to
                    # say invisible. Overlay keeps `color` as the fill and draws the
                    # hatch on top, which is the whole point of having both channels.
                    "fillmode": "overlay",
                },
            },
            hovertemplate="%{y}<br>%{x:,} finding(s), "
            + _severity_label(severity)
            + "<extra></extra>",
        )
    figure.update_layout(barmode="stack")
    figure.update_yaxes(categoryorder="array", categoryarray=list(reversed(order)))
    # `dtick=1` is one tick per decade. Without it a log axis over four orders of magnitude
    # labels every minor tick, and the axis arrives as forty tiny numbers.
    figure.update_xaxes(title="Findings (log scale)", type="log", dtick=1)
    return _layout(figure, 40 * len(order) + 140)


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
            # Horizontal, under the plot. A vertical colourbar beside a two-row heatmap
            # gets `len` scaled off the figure height and collapses to a stub with its
            # title and both tick labels overprinting each other.
            colorbar={
                "title": {"text": "Share of sessions trading", "side": "top"},
                "tickformat": ".0%",
                "tickvals": [0.0, 0.5, 1.0],
                "orientation": "h",
                # Above the plot, where the other chart puts its legend. Below it, the key
                # collides with the "Month" axis title on a short two-row heatmap.
                "y": 1.0,
                "yanchor": "bottom",
                "x": 0,
                "xanchor": "left",
                "thickness": 9,
                "len": 0.4,
                "outlinewidth": 1,
                "outlinecolor": theme.HAIRLINE,
                "tickfont": {"family": theme.FONT_TYPED, "size": 10},
                "title_font": {"size": 10.5, "color": theme.INK_MUTED},
            },
            xgap=1,
            ygap=1,
        )
    )
    figure.update_xaxes(title="Month")
    figure.update_yaxes(title="Instrument", autorange="reversed")
    # No title: the page already sets this heading as a ruled clause above the chart, and
    # printing it twice was the kind of duplication a reader reads as a mistake.
    return _layout(figure, 30 * len(data.contracts) + 210)


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
            increasing={"line": {"color": theme.INK, "width": 1}, "fillcolor": _UP_FILL},
            decreasing={"line": {"color": theme.INK, "width": 1}, "fillcolor": _DOWN_FILL},
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Bar(
            x=sessions,
            y=bars.get_column(C.VOLUME).to_list(),
            name="Volume",
            marker_color=theme.HAIRLINE,
            hovertemplate="%{x|%Y-%m-%d}<br>volume %{y:,}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    figure.update_xaxes(rangeslider_visible=False, row=1, col=1)
    figure.update_xaxes(title="Trading session", row=2, col=1)
    figure.update_yaxes(title="Price", row=1, col=1)
    figure.update_yaxes(title="Volume", row=2, col=1)
    return _layout(figure, 520)


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
            line={"color": theme.HAIRLINE, "width": 1},
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
            line={"color": theme.REFERENCE, "width": 2},
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
            line={"color": theme.WARNING_INK, "width": 0.5},
            fill="tozeroy",
            fillcolor="rgba(140,90,16,0.20)",
            hovertemplate="%{x|%H:%M}<br>%{y} bar(s) in the window<extra></extra>",
        ),
        row=2,
        col=1,
    )
    figure.update_xaxes(title="Chicago wall clock", row=2, col=1)
    figure.update_yaxes(title="Price", row=1, col=1)
    figure.update_yaxes(title="Bars", rangemode="tozero", row=2, col=1)
    return _layout(figure, 520)


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
            marker={
                "color": SEVERITY_COLOURS.get(severity, theme.EXPECTED_INK),
                "pattern": {
                    "shape": SEVERITY_PATTERNS.get(severity, "."),
                    "fgcolor": theme.STOCK,
                    "size": 4,
                    "solidity": 0.28,
                    "fillmode": "overlay",
                },
            },
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
    return _layout(figure, 46 * len(lanes) + 160)


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
            marker_color=[
                theme.INK if count >= expected * 0.95 else theme.WARNING_INK for count in counts
            ],
            hovertemplate="%{x|%Y-%m-%d}<br>%{y:,} minute bar(s)<extra></extra>",
            name="Minute bars",
        )
    )
    # The tolerance limit: a full CME session. Drawn, not described, so a truncated session
    # reads as a deviation from a standard rather than as a short bar.
    #
    # No label on the line. Plotly draws an `annotation_text` *inside* the plot at the
    # line's own height, and these bars run to the limit — so the text sat on top of both
    # the rule and the bars, with no clear space to move it to. The page's caption names
    # the threshold instead, and the y-axis is labelled, so the rule is still readable as
    # 1,380 without putting ink over the data.
    figure.add_hline(
        y=expected,
        line={"color": theme.REFERENCE, "dash": "dot", "width": 1},
    )
    figure.update_xaxes(title="Trading session")
    # A tick on the limit itself, so the dotted rule is readable as 1,380 off the axis
    # rather than only from the caption. This is the one chart with a real threshold, and
    # a drawn limit nobody can put a number to is decoration.
    figure.update_yaxes(
        title="Minute bars",
        tickvals=[0, expected // 2, expected],
        range=[0, expected * 1.06],
    )
    return _layout(figure, 260)
