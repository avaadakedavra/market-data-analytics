"""Data Quality — every finding, filtered the way a person actually asks for it.

**This page opens on warnings and errors, not on everything.** The reason is measured: the
full corpus produces 5 daily errors, 39 daily warnings and 14,974 daily INFO findings, and
the minute frame produces 14,899 `stale_bar` findings covering 2,559,602 bars, of which
*none* are above INFO.
A table defaulting to "all" would hand a risk manager two and a half million correct
settlement prints and hide the five things that are actually wrong. The INFO tier is one
checkbox away and its size is printed above the table, so nothing is concealed — it is
ranked.

Two views of the same findings sit on this page. The table is for reading one finding at a
time, with its evidence — the values and the row ids behind the sentence — in an expander,
so a claim can be traced back to the source rows rather than taken on trust. The timeline
strip is for seeing *where the data is not*: the holes in a session, and the sessions that
are missing altogether. A strip of evenly spaced grey blocks across every instrument is a
holiday calendar; one amber block on one instrument is a feed problem.
"""

from __future__ import annotations

import json
from datetime import date

import polars as pl
import streamlit as st

from mdq.api.schemas import FindingsResponse, QualitySummaryResponse
from mdq.dashboard import cache, charts, ui
from mdq.dashboard.backend import (
    DEFAULT_MIN_SEVERITY,
    FINDINGS_PAGE,
    Backend,
    findings_table,
    gap_segments,
    severity_triage,
)
from mdq.domain.frequency import Frequency
from mdq.service import check_title

__all__ = ["render"]

#: Columns the table shows, in reading order. `check_id` and `evidence` travel with the
#: frame for the expander below and are not put in front of the reader.
_TABLE_COLUMNS = (
    "Severity",
    "Instrument",
    "Session",
    "What was found",
    "Bars affected",
    "Detail",
)


def render(backend: Backend) -> None:
    """Draw the Data Quality page."""
    st.title("Data quality")
    st.caption(
        "Every defect the checks found, worst first. Findings the activity model explained "
        "as normal market behaviour are counted but hidden until you ask for them."
    )

    summary = ui.guard(lambda: cache.summary(backend), context="Could not read what is loaded.")
    if summary is None:
        return
    if not summary.by_frequency:
        ui.nothing_loaded()
        return

    frequency = ui.frequency_picker(summary, key="quality_frequency")
    if frequency is Frequency.MINUTE:
        st.warning(ui.MINUTE_WARNING, icon="⏳")

    quality = ui.guard(
        lambda: _quality(backend, frequency), context="Could not run the quality checks."
    )
    if quality is None:
        return

    include_info, contracts, checks, dates = _filters(quality)
    triage = severity_triage(quality.findings_by_severity)
    if not include_info:
        st.info(triage.hidden_note, icon=":material/info:")

    response = ui.guard(
        lambda: cache.findings(
            backend,
            frequency=frequency,
            contract_codes=contracts,
            checks=checks,
            severity=None if include_info else DEFAULT_MIN_SEVERITY,
            start=dates[0],
            end=dates[1],
        ),
        context="Could not read the findings.",
    )
    if response is None:
        return

    _table(response)
    st.divider()
    _timeline(response)


def _quality(backend: Backend, frequency: Frequency | None) -> QualitySummaryResponse:
    """The quality summary, behind a spinner when the frequency makes it expensive."""
    if frequency is Frequency.MINUTE:
        with st.spinner("Checking every minute bar — this takes a few seconds the first time…"):
            return cache.quality_summary(backend, frequency)
    return cache.quality_summary(backend, frequency)


def _filters(
    quality: QualitySummaryResponse,
) -> tuple[bool, list[str], list[str], tuple[date | None, date | None]]:
    """The filter row. Defaults are chosen so the first screen is the useful one."""
    instruments = sorted({str(row["contract"]) for row in quality.by_contract if row["contract"]})
    left, middle, right = st.columns([2, 2, 3])
    contracts = left.multiselect("Instruments", instruments, default=[], key="quality_contracts")
    checks = middle.multiselect(
        "Checks",
        list(quality.checks_run),
        default=[],
        format_func=check_title,
        key="quality_checks",
    )
    picked = right.date_input(
        "Trading sessions (optional)",
        value=(),
        key="quality_dates",
        help="Leave empty for the whole history. A date range excludes findings that "
        "belong to no session, such as a malformed input row.",
    )
    include_info = st.checkbox(
        "Include expected findings (settlement prints, holidays, dormant contracts)",
        value=False,
        key="quality_include_info",
    )
    start, end = (
        (picked[0], picked[1])
        if isinstance(picked, tuple) and len(picked) == 2
        else (
            None,
            None,
        )
    )
    return include_info, contracts, checks, (start, end)


def _table(response: FindingsResponse) -> None:
    """The findings table, and the evidence behind one chosen row."""
    frame = findings_table(response.findings)
    if frame.height == 0:
        st.success(
            "No findings match these filters. If you have hidden the expected findings, "
            "that means nothing here needs a human."
        )
        return
    st.caption(
        f"Showing {frame.height:,} finding(s)"
        + (
            f"; more are available beyond the first {FINDINGS_PAGE:,}. Narrow the filters "
            "to see them."
            if response.next_offset is not None
            else "."
        )
    )
    st.dataframe(
        frame.select(_TABLE_COLUMNS),
        width="stretch",
        hide_index=True,
        height=min(520, 40 + 36 * frame.height),
    )
    _evidence(frame)


def _evidence(frame: pl.DataFrame) -> None:
    """One finding's evidence, decoded — the traceability back to the source rows."""
    labels = [
        f"{row['Severity']} · {row['Instrument'] or '—'} · {row['Session'] or '—'} · "
        f"{row['What was found']}"
        for row in frame.to_dicts()
    ]
    with st.expander("Evidence for one finding"):
        st.caption(
            "The values behind the sentence, including a sample of the source row numbers "
            "so the finding can be traced back to the file it came from."
        )
        chosen = st.selectbox(
            "Finding",
            range(len(labels)),
            format_func=labels.__getitem__,
            key="quality_evidence_row",
        )
        row = frame.row(chosen, named=True)
        st.markdown(f"**{row['Detail']}**")
        st.json(json.loads(row["evidence"] or "{}"), expanded=True)


def _timeline(response: FindingsResponse) -> None:
    """The gap strip: where the data simply is not there."""
    st.subheader("Where the data is missing")
    gaps = gap_segments(response.findings)
    if gaps.height == 0:
        st.info(
            "No gaps among the findings shown. Missing sessions and holes inside a session "
            "are usually expected findings — tick “include expected” above to see them."
        )
        return
    whole = gaps.filter(pl.col("whole_session")).height
    partial = gaps.height - whole
    st.caption(
        f"{whole:,} session(s) with no bars at all and {partial:,} hole(s) inside a session "
        "that was otherwise trading. Colour is the finding's severity: grey means the check "
        "corroborated it against the other instruments on the exchange and concluded it was "
        "a holiday."
    )
    st.plotly_chart(charts.gap_timeline(gaps), width="stretch", key="quality_gaps")
