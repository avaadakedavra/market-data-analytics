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
from mdq.dashboard import cache, certificate, charts, ui
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
    summary = ui.guard(lambda: cache.summary(backend), context="Could not read what is loaded.")
    if summary is None:
        return
    ui.certificate_head(summary, "Data quality", 3)
    st.title("Data quality")
    st.caption(
        "Every defect the checks found, worst first. Findings the activity model explained "
        "as normal market behaviour are counted but hidden until you ask for them."
    )
    if not summary.by_frequency:
        ui.nothing_loaded()
        ui.signature(backend.label, loaded=False)
        return

    frequency = ui.frequency_picker(summary, key="quality_frequency")
    if frequency is Frequency.MINUTE:
        st.warning(ui.MINUTE_WARNING, icon=":material/hourglass_top:")

    quality = ui.guard(
        lambda: _quality(backend, frequency), context="Could not run the quality checks."
    )
    if quality is None:
        return

    include_info, contracts, checks, dates = _filters(quality, ui.session_span(summary))
    triage = severity_triage(quality.findings_by_severity)
    # The three tiers stay on screen while the filters move, so a reader who narrows to one
    # instrument can still see what share of the whole they are looking at.
    certificate.render_plates(ui.severity_plates(triage, quality.thresholds))
    if not include_info:
        # A persistent statement about what this measurement excludes, so it takes the
        # traceability band rather than an alert: an alert reads as something that happened,
        # and nothing happened here — this is the table's standing scope.
        certificate.render_traceability("Excluded from this table", triage.hidden_note)

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
    ui.signature(
        backend.label,
        extra=[("Frequency", ui.FREQUENCY_LABELS[frequency] if frequency else "—")],
    )


def _quality(backend: Backend, frequency: Frequency | None) -> QualitySummaryResponse:
    """The quality summary, behind a spinner when the frequency makes it expensive."""
    if frequency is Frequency.MINUTE:
        with st.spinner("Checking every minute bar — this takes a few seconds the first time…"):
            return cache.quality_summary(backend, frequency)
    return cache.quality_summary(backend, frequency)


def _filters(
    quality: QualitySummaryResponse,
    span: tuple[date | None, date | None],
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
    first, last = span
    picked = right.date_input(
        "Trading sessions (optional)",
        value=(),
        # Bounded by the data, not by today. An unbounded picker opens on the current month
        # and only that month is clickable, so with a corpus ending in March a reader had
        # to page back through every empty month to reach a session that exists.
        min_value=first,
        max_value=last,
        format=ui.DATE_FORMAT,
        key="quality_dates",
        help=(
            "Day/month/year, and these are Chicago trading sessions. Leave empty for the "
            "whole history. A date range excludes findings that belong to no session, such "
            "as a malformed input row."
            + (
                f" Loaded sessions run {ui.au_date(first)} to {ui.au_date(last)}."
                if first and last
                else ""
            )
        ),
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
        # `Detail` carries the sentence a reader is actually here for, so the columns whose
        # values are short give up their slack to it. `Bars affected` is deliberately not
        # among them: `small` sizes to the *values*, and a 13-character heading over
        # single-digit counts clips to "Bars affecte" — a narrow column is only narrow when
        # its heading is short too.
        column_config={
            "Severity": st.column_config.TextColumn(width="small"),
            "Instrument": st.column_config.TextColumn(width="small"),
            # Day-first, and as a date rather than a datetime: the column is a `pl.Date`,
            # which Streamlit otherwise renders as "2021-11-11 00:00:00" — a midnight that
            # means nothing, since a trading session is a day and not an instant.
            "Session": st.column_config.DateColumn(format=ui.DATE_FORMAT, width="small"),
            "Detail": st.column_config.TextColumn(width="large"),
        },
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
