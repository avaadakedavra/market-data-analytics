"""The figures, asserted on directly — no script runner, no screenshot.

A charting bug is almost never "no chart appeared". It is a line drawn through a gap that
is not there, a severity rendered in the wrong colour, or an axis that turns four orders of
magnitude into a one-pixel bar. Those live in the figure object, so that is what these
tests read.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from mdq.dashboard import charts
from mdq.dashboard.backend import CHECK_SCHEMA, GAP_SCHEMA, HeatmapData, conform
from mdq.domain.schema import BAR_SCHEMA, C

pytestmark = pytest.mark.unit


def _bars() -> pl.DataFrame:
    rows = [
        {
            C.ROW_ID: index,
            C.CONTRACT: "ESH26",
            C.TS_UTC: datetime(2026, 3, day, tzinfo=UTC),
            C.TS_LOCAL: datetime(2026, 3, day),
            C.SESSION_DATE: date(2026, 3, day),
            C.OPEN: 100.0 + index,
            C.HIGH: 101.0 + index,
            C.LOW: 99.0 + index,
            C.CLOSE: 100.5 + index,
            C.VOLUME: 1_000 * (index + 1),
        }
        for index, day in enumerate((2, 3, 4))
    ]
    return conform(rows, BAR_SCHEMA)


# --------------------------------------------------------------------------- #
# empty states
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "figure",
    [
        charts.findings_by_check(pl.DataFrame(schema=CHECK_SCHEMA)),
        charts.regime_heatmap(HeatmapData([], [], [], [])),
        charts.candlestick(pl.DataFrame(schema=BAR_SCHEMA), "ESH26"),
        charts.vwap_chart(pl.DataFrame(), "ESH26", date(2026, 3, 2), "15m"),
        charts.gap_timeline(pl.DataFrame(schema=GAP_SCHEMA)),
        charts.coverage_bars(pl.DataFrame()),
    ],
)
def test_an_empty_input_draws_an_explanation_rather_than_empty_axes(figure: object) -> None:
    annotations = figure.layout.annotations  # type: ignore[attr-defined]
    assert annotations, "an empty chart drew no explanation"
    assert annotations[0].text


def test_the_empty_state_names_the_instrument_and_the_dates_that_found_nothing() -> None:
    figure = charts.candlestick(pl.DataFrame(schema=BAR_SCHEMA), "CLG26")
    assert "CLG26" in figure.layout.annotations[0].text


# --------------------------------------------------------------------------- #
# the Overview charts
# --------------------------------------------------------------------------- #


def test_findings_by_check_colours_by_severity_and_uses_a_log_axis() -> None:
    """Four orders of magnitude on one axis: 43 errors beside 14,152 expected findings."""
    totals = pl.DataFrame(
        [
            {
                "check": "Incoherent OHLC",
                "severity": "ERROR",
                "findings": 43,
                "bars_affected": 43,
            },
            {
                "check": "Stale (no-range) bar",
                "severity": "INFO",
                "findings": 14_152,
                "bars_affected": 14_152,
            },
        ],
        schema=CHECK_SCHEMA,
    )
    figure = charts.findings_by_check(totals)
    colours = {trace.name: trace.marker.color for trace in figure.data}
    assert colours["Error"] == charts.SEVERITY_COLOURS["ERROR"]
    assert colours["Expected"] == charts.SEVERITY_COLOURS["INFO"]
    assert figure.layout.xaxis.type == "log"
    assert figure.layout.barmode == "stack"


def test_the_heatmap_keeps_a_null_cell_null() -> None:
    """A month a contract was not listed in must stay blank, never be drawn as zero."""
    figure = charts.regime_heatmap(
        HeatmapData(["ESH26"], ["2026-01", "2026-02"], [[None, 1.0]], [["a", "b"]])
    )
    assert list(figure.data[0].z[0]) == [None, 1.0]
    assert figure.data[0].zmin == 0.0
    assert figure.data[0].zmax == 1.0


# --------------------------------------------------------------------------- #
# the Analytics charts
# --------------------------------------------------------------------------- #


def test_the_candlestick_carries_volume_underneath_it() -> None:
    figure = charts.candlestick(_bars(), "ESH26")
    kinds = [trace.type for trace in figure.data]
    assert kinds == ["candlestick", "bar"]
    # Direction is hollow-versus-filled, not green-versus-red: colour in this dashboard
    # means severity or the reference standard, and a third meaning would break both.
    candles = figure.data[0]
    assert candles.increasing.fillcolor != candles.decreasing.fillcolor
    assert candles.increasing.line.color == candles.decreasing.line.color


def test_the_vwap_chart_breaks_the_line_rather_than_bridging_a_missing_average() -> None:
    series = pl.DataFrame(
        {
            C.TS_LOCAL: [datetime(2026, 3, 3, 9, minute) for minute in range(3)],
            C.CLOSE: [100.0, 101.0, 102.0],
            "vwap": [100.0, None, 102.0],
            "bars_in_window": [1, 1, 3],
        }
    )
    figure = charts.vwap_chart(series, "ESH26", date(2026, 3, 3), "15m")
    close, vwap, window = figure.data
    assert close.connectgaps is False
    assert vwap.connectgaps is False
    assert vwap.y == (100.0, None, 102.0)
    assert window.fill == "tozeroy"


def test_the_coverage_chart_marks_a_truncated_session() -> None:
    """2026-03-02 holds 960 of 1,380 minutes and must not look like a full session."""
    coverage = pl.DataFrame(
        {C.SESSION_DATE: [date(2026, 3, 2), date(2026, 3, 3)], "bar_count": [960, 1_380]}
    )
    figure = charts.coverage_bars(coverage)
    assert figure.data[0].marker.color[0] != figure.data[0].marker.color[1]
    assert figure.layout.shapes, "the full-session reference line was not drawn"


# --------------------------------------------------------------------------- #
# the gap strip
# --------------------------------------------------------------------------- #


def test_the_gap_strip_gives_each_instrument_a_lane_and_each_hole_its_width() -> None:
    gaps = pl.DataFrame(
        [
            {
                C.CONTRACT: "ESH26",
                C.SESSION_DATE: date(2026, 3, 4),
                "from_utc": datetime(2026, 3, 4, 14, tzinfo=UTC),
                "to_utc": datetime(2026, 3, 4, 14, 40, tzinfo=UTC),
                "minutes_missing": 39,
                "whole_session": False,
                "severity": "WARNING",
                "what": "39 minute bar(s) missing",
            },
            {
                C.CONTRACT: "CLG26",
                C.SESSION_DATE: date(2026, 3, 5),
                "from_utc": datetime(2026, 3, 5, tzinfo=UTC),
                "to_utc": datetime(2026, 3, 6, tzinfo=UTC),
                "minutes_missing": None,
                "whole_session": True,
                "severity": "INFO",
                "what": "no bars for CLG26",
            },
        ],
        schema=GAP_SCHEMA,
    )
    figure = charts.gap_timeline(gaps)
    widths = {trace.name: trace.x[0] for trace in figure.data}
    assert widths["Warning"] == 40 * 60 * 1_000
    assert widths["Expected"] == 24 * 60 * 60 * 1_000
    assert figure.layout.xaxis.type == "date"
    assert set(figure.layout.yaxis.categoryarray) == {"ESH26", "CLG26"}


def test_the_gap_strip_shows_each_severity_in_the_legend_exactly_once() -> None:
    row = {
        C.CONTRACT: "ESH26",
        C.SESSION_DATE: date(2026, 3, 4),
        "from_utc": datetime(2026, 3, 4, 14, tzinfo=UTC),
        "to_utc": datetime(2026, 3, 4, 15, tzinfo=UTC),
        "minutes_missing": 59,
        "whole_session": False,
        "severity": "WARNING",
        "what": "hole",
    }
    figure = charts.gap_timeline(pl.DataFrame([row, row], schema=GAP_SCHEMA))
    assert [trace.showlegend for trace in figure.data] == [True, False]
