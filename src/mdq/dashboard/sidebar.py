"""The sidebar: where the data came from, and how to add your own.

Upload is in the sidebar rather than on a page of its own because it is not a destination —
it is something a user does *to* the dataset, after which every page should be about their
file too. Dropping a CSV here ingests it, reports on it immediately, and clears the cached
answers so the Overview, Data Quality and Insights pages all recompute with it included.

What comes back is deliberately the whole story, in the order a person asks for it:

1. *Did it load?* — rows in, rows that became bars, rows that could not be placed.
2. *What could not be placed, and why?* — the rejects table, with the original values, so
   the fix is obvious rather than guessed at. A row with a bad price is **not** here: it
   was kept and flagged, because a business user needs to see it in context.
3. *What is wrong with the rows that did load?* — findings, worst first.
4. *Does it change the picture?* — the insights, recomputed across everything loaded,
   because one small file rarely contains a pattern on its own.
"""

from __future__ import annotations

import streamlit as st

from mdq.api.schemas import IngestResponse
from mdq.dashboard import cache, certificate, ui
from mdq.dashboard.backend import Backend, findings_table

__all__ = ["render", "report_card"]

#: Extensions the readers claim. Anything else is a 400 before a byte is parsed.
_ACCEPTED = ("csv", "parquet")

#: Upload results survive a rerun here, so switching page does not lose the report.
_REPORT_KEY = "upload_report"


def render(backend: Backend) -> None:
    """Draw the sidebar: provenance, then the uploader, then the last upload's report."""
    with st.sidebar:
        st.caption(f"Reading from: {backend.label}")
        st.divider()
        st.subheader("Add your own data")
        st.caption(
            "A CSV or parquet file of bars. Columns are matched by name — contract, "
            "timestamp, open, high, low, close, volume — and nothing is silently repaired."
        )
        uploaded = st.file_uploader(
            "File", type=list(_ACCEPTED), key="upload_file", label_visibility="collapsed"
        )
        if uploaded is not None and st.button("Ingest this file", key="upload_button"):
            report = ui.guard(
                lambda: backend.upload(uploaded.name, uploaded.getvalue()),
                context=f"Could not ingest {uploaded.name}.",
            )
            if report is not None:
                # The dataset changed, so every cached answer about it is now stale.
                cache.forget_everything()
                st.session_state[_REPORT_KEY] = report
        report = st.session_state.get(_REPORT_KEY)
        if report is not None:
            report_card(report)


def report_card(report: IngestResponse) -> None:
    """What happened to the uploaded file."""
    stats = report.ingest
    st.success(f"Loaded {stats.source} as {report.frequency.value} bars.")
    st.caption(f"Stored as dataset `{report.dataset_id}`.")
    certificate.render_schedule(
        [
            certificate.ScheduleRow(
                label="Rows that became bars",
                observed=f"{stats.rows_out:,}",
                of_total=f"of {stats.rows_in:,} rows",
                ratio=stats.rows_out / stats.rows_in if stats.rows_in else None,
                failed=stats.rows_rejected > 0,
            )
        ]
    )

    if stats.rows_rejected:
        st.warning(
            f"{stats.rows_rejected:,} row(s) could not be placed on the timeline: "
            + ", ".join(f"{count} {reason}" for reason, count in stats.rejects_by_reason.items()),
            icon=":material/warning:",
        )
    if report.rejects:
        with st.expander(f"Rows that could not be used ({len(report.rejects)} shown)"):
            st.caption(
                "A row is rejected only when it cannot be located — no contract, or no "
                "usable timestamp. A row with a bad price is kept and flagged instead, so "
                "you can see it in context."
            )
            st.dataframe(report.rejects, width="stretch", hide_index=True)

    if report.findings:
        with st.expander(f"Findings in your file ({len(report.findings)})"):
            frame = findings_table(report.findings)
            st.dataframe(
                frame.select("Severity", "Instrument", "Session", "What was found", "Detail"),
                width="stretch",
                hide_index=True,
            )
    else:
        st.info("No quality findings in your file.")

    if report.insights:
        st.caption(
            f"{len(report.insights)} pattern(s) across all loaded data, with your file "
            "included — see the Insights page."
        )
