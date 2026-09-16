"""Overview — what is loaded, what is wrong with it, and what is merely expected.

The landing page has one job: stop a business user drowning. The corpus produces 15,018
daily findings, of which 14,974 are INFO — correct settlement prints on days nothing
traded, and exchange holidays. Opening on "15,018 problems" would be false. So the top of
this page is a triage: how many need attention today, how many are explained, and one
sentence saying what "explained" means.

It is set as the schedule of results on a calibration certificate, which is the shape the
argument already had: a figure, what it was measured against, and the deviation drawn to
scale between its limits. That is also why there is no row of metric tiles here any more. A
tile shows a number; a schedule row shows a number *and* the thing that decided it, and
this page is read cold by someone with nobody to explain it to them.

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

import polars as pl
import streamlit as st

from mdq.api.schemas import DatasetsSummaryResponse, QualitySummaryResponse
from mdq.dashboard import cache, certificate, charts, ui
from mdq.dashboard.backend import Backend, check_totals, heatmap_data, severity_triage
from mdq.domain.frequency import Frequency

__all__ = ["render"]

#: What the severities are measured against, stated where a certificate states it: in the
#: head of the results, before any figure. True of every dataset, including an upload —
#: unlike the vendor-oracle agreement, which is a fact about the test suite and is
#: attributed as one in the countersignature.
_REFERENCE_CLAIM = (
    "Severity is measured against an activity regime observed per contract from the data "
    "itself, never a shipped calendar. Every threshold that decided a severity is printed "
    "with the finding it decided."
)


def render(backend: Backend) -> None:
    """Draw the Overview page."""
    summary = ui.guard(lambda: cache.summary(backend), context="Could not read what is loaded.")
    if summary is None:
        return
    ui.certificate_head(summary, "Overview", 1)
    st.title("Market data overview")

    if not summary.by_frequency:
        ui.nothing_loaded()
        ui.signature(backend.label, loaded=False)
        return

    st.subheader("What needs attention")
    frequency = ui.frequency_picker(summary, key="overview_frequency")
    if frequency is Frequency.MINUTE:
        st.warning(ui.MINUTE_WARNING, icon=":material/hourglass_top:")
    quality = ui.guard(
        lambda: _quality(backend, frequency),
        context="Could not run the quality checks.",
    )
    if quality is None:
        return

    _triage(quality)
    certificate.render_traceability("Reference standard", _REFERENCE_CLAIM)

    totals = check_totals(quality)
    _schedule_of_results(totals)
    st.subheader("What the checks found")
    st.caption(
        "The same findings by magnitude rather than by residual. A log axis because the "
        "counts span four orders of magnitude, and the hatch repeats the severity so the "
        "three tiers stay apart in greyscale."
    )
    st.plotly_chart(
        charts.findings_by_check(totals),
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

    _heatmap(backend)
    _inventory(summary)
    ui.signature(
        backend.label,
        extra=[("Frequency", ui.FREQUENCY_LABELS[frequency] if frequency else "—")],
    )


def _quality(backend: Backend, frequency: Frequency | None) -> QualitySummaryResponse:
    """The quality summary, behind a spinner that names the cost when it is large."""
    if frequency is Frequency.MINUTE:
        with st.spinner("Checking every minute bar — this takes a few seconds the first time…"):
            return cache.quality_summary(backend, frequency)
    return cache.quality_summary(backend, frequency)


def _schedule_of_results(totals: pl.DataFrame) -> None:
    """One schedule row per check: what it raised, and what survived the regime test.

    This is the certificate's schedule proper, and it is a real deviation measurement
    rather than a restatement of the chart below it. For each check, the figure is the
    residual — the findings the activity model could *not* explain away — measured against
    everything that check raised. A check that fired 2,026 times and left nothing for a
    person reads as a deviation of zero, which is the product's whole argument stated once
    per check instead of once for the dataset.

    It is also the only honest form the direction's signature interaction can take. The
    vendor-oracle agreement is a fact about the golden tests, not a runtime measurement, so
    dramatizing *that* would mean shipping test fixtures as product data. These rows
    measure what this page actually computed, from the data actually loaded.
    """
    if totals.height == 0:
        return
    per_check = (
        totals.group_by("check")
        .agg(
            pl.col("findings").sum().alias("raised"),
            pl.col("findings").filter(pl.col("severity") != "INFO").sum().alias("residual"),
            pl.col("findings").filter(pl.col("severity") == "ERROR").sum().alias("errors"),
        )
        .sort(["raised", "check"], descending=[True, False])
    )
    st.subheader("Schedule of results")
    st.caption(
        "Per check: what it raised, and what was left once the activity regime had "
        "explained what it could. The bar is the residual — the share a person still has "
        "to look at."
    )
    certificate.render_schedule(
        certificate.ScheduleRow(
            label=row["check"],
            observed=f"{row['residual']:,}",
            of_total=f"of {row['raised']:,} raised",
            ratio=row["residual"] / row["raised"] if row["raised"] else 0.0,
            ratio_label=(
                f"{row['residual'] / row['raised']:.1%} needs a human" if row["raised"] else "—"
            ),
            failed=row["errors"] > 0,
        )
        for row in per_check.to_dicts()
    )


def _triage(quality: QualitySummaryResponse) -> None:
    """The triage band: the whole point of the regime model, measured and up front."""
    triage = severity_triage(quality.findings_by_severity)
    if triage.total == 0:
        certificate.render_schedule(
            [
                certificate.ScheduleRow(
                    label="Requiring review",
                    observed="0",
                    of_total="of 0 findings",
                    note="Every check passed on this data. Nothing to look at.",
                )
            ]
        )
        return

    share = triage.expected / triage.total
    certificate.render_schedule(
        [
            certificate.ScheduleRow(
                label="Requiring review",
                observed=f"{triage.needs_attention:,}",
                of_total=f"of {triage.total:,} findings",
                note=(
                    f"The other {share:.1%} are expected artefacts the activity model "
                    "already explained — settlement prints on dormant days, and exchange "
                    "holidays. Counted here, and hidden from the findings table by default."
                ),
                ratio=triage.needs_attention / triage.total,
                ratio_label=f"{triage.needs_attention / triage.total:.2%} of all findings",
                failed=triage.errors > 0,
            )
        ]
    )
    certificate.render_plates(ui.severity_plates(triage, quality.thresholds))
    certificate.render_reading_note(
        f"{triage.needs_attention:,} of {triage.total:,} findings need a human.",
        "An error is a bar that cannot be right; a warning is suspicious in a session that "
        "was otherwise trading normally; an expected artefact is correct data that looks "
        "odd. Each tier carries its own hatch as well as its own colour, so the triage "
        "survives being printed in greyscale.",
    )
    if triage.needs_attention:
        ui.page_link("data-quality", f"Open the {triage.needs_attention:,} that need a human →")


def _inventory(summary: DatasetsSummaryResponse) -> None:
    """What is loaded, per frequency. The headline totals are in the certificate head."""
    st.subheader("What is loaded")
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
        column_config={
            "First session": st.column_config.DateColumn(format=ui.DATE_FORMAT),
            "Last session": st.column_config.DateColumn(format=ui.DATE_FORMAT),
        },
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
