"""Insights — why the data looks the way it does, and the rule to adopt about it.

The Data Quality page says *what* is wrong. This one says *why*, and that is the difference
between a ticket and a phone call. Forty-three incoherent bars in a table look like
forty-three separate problems. The insight that thirty-nine of them share one settlement
convention — and that all of them land on a handful of dates, striking whole contract
families at once — says the vendor's feed had bad days, and names them.

**The feed-incident card leads.** It is pinned to the top and labelled, because it is the
only finding on this page that implies an action outside this team: a date on which every
CL contract broke simultaneously is not something a data steward cleans, it is something
someone raises with the vendor. On the real corpus it fires across 5 CL contracts on 6
dates with a confidence of 0.65 — lower than the others precisely because it explains a
smaller share of the findings, which is the number working as intended rather than a defect.

Every card carries the same three things: the pattern in plain English, the evidence behind
it, and the suggested rule in a block a user can copy. The rule is offered as both JSON and
YAML because the two audiences differ — one pastes it into a pipeline config, the other
into a ticket.
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from mdq.api.schemas import InsightOut, InsightsResponse
from mdq.dashboard import cache, certificate, ui
from mdq.dashboard.backend import Backend, to_yaml
from mdq.domain.frequency import Frequency

__all__ = ["render"]

#: The insight that matters most to someone outside this team, pinned to the top.
_HEADLINE_RULE = "feed_incident_date_cluster"

#: What the two rule kinds actually do, said without the word "kind".
_KIND_NOTES = {
    "cleansing": "Adopting this **changes the data**: rows are relabelled or removed.",
    "validation": "Adopting this **refuses data**: matching rows are rejected or alerted on.",
}


def render(backend: Backend) -> None:
    """Draw the Insights page."""
    summary = ui.guard(lambda: cache.summary(backend), context="Could not read what is loaded.")
    if summary is None:
        return
    ui.certificate_head(summary, "Insights", 4)
    st.title("What the defects mean")
    st.caption(
        "Patterns across the whole dataset, each with the evidence behind it and a rule "
        "you can adopt. Derived by explainable rules, not a model — every number below can "
        "be traced back to the findings it came from."
    )
    if not summary.by_frequency:
        ui.nothing_loaded()
        ui.signature(backend.label, loaded=False)
        return

    frequency = ui.frequency_picker(summary, key="insights_frequency")
    if frequency is Frequency.MINUTE:
        st.warning(ui.MINUTE_WARNING, icon=":material/hourglass_top:")

    response = ui.guard(
        lambda: _insights(backend, frequency), context="Could not derive the insights."
    )
    if response is None:
        return
    if not response.insights:
        st.info(
            "No recurring pattern was found in this data. That is a real answer, not a "
            "failure: the rules look for defects that repeat across instruments, sessions "
            "or input columns, and a clean or very small dataset contains none."
        )
        return

    for insight in _ordered(response.insights):
        _card(insight, headline=insight.id == _HEADLINE_RULE)


def _insights(backend: Backend, frequency: Frequency | None) -> InsightsResponse:
    """The insights, behind a spinner when the frequency makes them expensive."""
    if frequency is Frequency.MINUTE:
        with st.spinner("Checking every minute bar — this takes a few seconds the first time…"):
            return cache.insights(backend, frequency)
    return cache.insights(backend, frequency)


def _ordered(insights: list[InsightOut]) -> list[InsightOut]:
    """Strongest evidence first, except that the feed incident is always first.

    The API already sorts by confidence, and confidence is the right default order. The one
    exception is deliberate: a cross-contract feed incident is the only pattern here that
    points at something outside this system, and burying it under a higher-confidence
    observation about settlement conventions would cost the reader the most actionable
    thing on the page.
    """
    return sorted(insights, key=lambda item: (item.id != _HEADLINE_RULE,))


def _card(insight: InsightOut, *, headline: bool) -> None:
    """One insight: the pattern, its evidence, and the rule it argues for."""
    with st.container(border=True):
        if headline:
            st.markdown("###### :red[Most actionable — raise this outside the team]")
        st.subheader(insight.title)
        st.write(insight.pattern)

        # Confidence always means the same thing here — the share of the relevant findings
        # the pattern accounts for — so it is the one figure on the page that earns a
        # deviation bar against a drawn scale: two confidences can be compared by eye.
        certificate.render_schedule(
            [
                certificate.ScheduleRow(
                    label="Confidence",
                    observed=f"{insight.rule.confidence:.0%}",
                    of_total="of the relevant findings",
                    note="The share of the relevant findings this pattern accounts for.",
                    ratio=insight.rule.confidence,
                ),
                certificate.ScheduleRow(
                    label="Findings explained",
                    observed=f"{insight.finding_count:,}",
                    of_total="findings",
                    note="How many individual findings this one pattern covers.",
                ),
                certificate.ScheduleRow(
                    label="Instruments",
                    observed=f"{len(insight.affected_contracts):,}",
                    of_total="contracts",
                    note="Contracts the pattern touches.",
                ),
            ]
        )
        if insight.affected_contracts:
            st.caption("Affects: " + ", ".join(insight.affected_contracts))

        with st.expander("The evidence"):
            st.json(insight.evidence, expanded=True)

        st.markdown(f"**Suggested rule — `{insight.rule.rule_id}`**")
        st.caption(_KIND_NOTES.get(insight.rule.kind, ""))
        st.write(insight.rule.rationale)
        yaml_tab, json_tab = st.tabs(["YAML", "JSON"])
        rule = insight.rule.model_dump(mode="json")
        # `wrap_lines` rather than CSS: the highlighter sets `white-space:pre` on the code
        # element, and overriding that from an injected stylesheet is a specificity fight
        # against an internal selector that would break on the next upgrade. The rule is
        # meant to be read and copied, and its `rationale` is a full sentence, so the line
        # has to wrap.
        yaml_tab.code(to_yaml(rule), language="yaml", wrap_lines=True)
        json_tab.code(_pretty(rule), language="json", wrap_lines=True)


def _pretty(rule: dict[str, Any]) -> str:
    """The rule as indented JSON, sorted so two copies of it diff cleanly."""
    return json.dumps(rule, indent=2, sort_keys=True)
