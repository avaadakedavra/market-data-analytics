"""Overview — what is loaded, what is wrong with it, and what is merely expected.

The landing page has one job: stop a business user drowning. The corpus produces 15,018
daily findings, of which 14,974 are INFO — correct settlement prints on days nothing
traded, and exchange holidays. Opening on "15,018 problems" would be false. So the top of
this page is a triage: how many need attention today, how many are explained, and one
sentence saying what "explained" means.

Underneath it, the two pictures that justify that split. The bar chart shows *which* check
produced the volume (stale bars, by four orders of magnitude). The heatmap shows *why* they
are not defects: a deferred contract is dark for years and brightens a few months before
expiry, and every stale bar inside the dark part is a settlement print on a day the
instrument did not trade.

Daily is the default and the heatmap is always daily — measured, a minute-frequency quality
pass costs 5.2 seconds and 1.3 GB, so it happens only when a user asks for it and only
behind a spinner that says what is happening.
"""

from __future__ import annotations

import streamlit as st

from mdq.api.schemas import DatasetsSummaryResponse, QualitySummaryResponse
from mdq.dashboard import cache, charts, ui
from mdq.dashboard.backend import Backend, check_totals, heatmap_data, severity_triage
from mdq.domain.frequency import Frequency

__all__ = ["render"]


def render(backend: Backend) -> None:
    """Draw the Overview page."""
    st.title("Market data overview")
    st.caption(
        "Futures bars, the defects found in them, and the ones that turned out to be "
        "normal market behaviour."
    )

    summary = ui.guard(lambda: cache.summary(backend), context="Could not read what is loaded.")
    if summary is None:
        return
    if not summary.by_frequency:
        ui.nothing_loaded()
        return

    _inventory(summary)
    st.divider()

    frequency = ui.frequency_picker(summary, key="overview_frequency")
    if frequency is Frequency.MINUTE:
        st.warning(ui.MINUTE_WARNING, icon="⏳")
    quality = ui.guard(
        lambda: _quality(backend, frequency),
        context="Could not run the quality checks.",
    )
    if quality is None:
        return

    _triage(quality.findings_by_severity)
    st.plotly_chart(
        charts.findings_by_check(check_totals(quality)),
        width="stretch",
        key="overview_checks",
    )
    with st.expander("The thresholds behind these severities"):
        st.caption(
            "Every severity that could be explained by “nothing was trading” is decided by "
            "these numbers, observed per instrument from the data itself rather than from a "
            "calendar. They travel with the report so a downgrade can always be explained."
        )
        st.json(quality.thresholds, expanded=False)

    st.divider()
    _heatmap(backend)


def _quality(backend: Backend, frequency: Frequency | None) -> QualitySummaryResponse:
    """The quality summary, behind a spinner that names the cost when it is large."""
    if frequency is Frequency.MINUTE:
        with st.spinner("Checking every minute bar — this takes a few seconds the first time…"):
            return cache.quality_summary(backend, frequency)
    return cache.quality_summary(backend, frequency)


def _inventory(summary: DatasetsSummaryResponse) -> None:
    """The headline "what have I got?" numbers."""
    spans = [
        (item.first_session, item.last_session)
        for item in summary.by_frequency
        if item.first_session is not None
    ]
    first = min((s for s, _ in spans), default=None)
    last = max((e for _, e in spans), default=None)
    ui.metric_row(
        [
            ("Instruments", f"{len(summary.contracts):,}", "Distinct contracts loaded."),
            ("Bars", f"{summary.bars:,}", "Every row, across every frequency."),
            (
                "First session",
                first.isoformat() if first else "—",
                "Earliest trading session in the data.",
            ),
            (
                "Last session",
                last.isoformat() if last else "—",
                "Latest trading session in the data.",
            ),
        ]
    )
    st.dataframe(
        [
            {
                "Frequency": ui.FREQUENCY_LABELS[item.frequency],
                "Files": item.files,
                "Instruments": len(item.contracts),
                "Bars": item.bars,
                "First session": item.first_session,
                "Last session": item.last_session,
            }
            for item in summary.by_frequency
        ],
        width="stretch",
        hide_index=True,
    )


def _triage(counts: dict[str, int]) -> None:
    """Three numbers and one sentence: the whole point of the regime model, up front."""
    triage = severity_triage(counts)
    st.subheader("What needs attention")
    ui.metric_row(
        [
            ("Errors", f"{triage.errors:,}", "Bars that cannot be right — act on these first."),
            (
                "Warnings",
                f"{triage.warnings:,}",
                "Suspicious in a session that was otherwise trading normally.",
            ),
            (
                "Expected",
                f"{triage.expected:,}",
                "Correct data that looks odd: settlement prints on dormant days, holidays.",
            ),
        ]
    )
    if triage.total == 0:
        st.success("Every check passed. Nothing to look at.")
        return
    share = triage.expected / triage.total
    st.caption(
        f"{triage.needs_attention:,} of {triage.total:,} findings need a human. The other "
        f"{share:.1%} are expected artefacts the activity model already explained — they are "
        "counted here and hidden from the findings table by default."
    )


def _heatmap(backend: Backend) -> None:
    """Contract x month activity, always from daily bars."""
    st.subheader("How much each instrument actually traded")
    st.caption(
        "Darker means the instrument barely traded that month. A deferred contract sits "
        "dark for years and brightens as it nears expiry — and a flat, zero-volume bar "
        "inside a dark month is a settlement print, not a defect. Always computed from "
        "daily bars."
    )
    activity = ui.guard(
        lambda: cache.activity(backend, Frequency.DAILY),
        context="Could not build the activity map.",
    )
    if activity is None:
        return
    st.plotly_chart(
        charts.regime_heatmap(heatmap_data(activity)),
        width="stretch",
        key="overview_heatmap",
    )
