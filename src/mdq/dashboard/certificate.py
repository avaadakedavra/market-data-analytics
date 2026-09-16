"""The certificate's parts: head, schedule, state plates, traceability, countersignature.

Deliberately split the way `charts.py` is split. Every function here is a pure builder that
takes plain data and returns an HTML string, and the handful of `render_*` wrappers are the
only lines that touch Streamlit. A test can therefore assert that the schedule printed the
threshold beside the figure it decided, or that a failing row carried the fail class,
without running a script runner — which is where a presentation bug actually lives.

The vocabulary replaces `st.metric` everywhere. A certificate does not open with four big
numbers in rounded tiles; it opens by declaring what was measured and against what
reference, and then prints the deviations as a ruled schedule. Two rules follow from that
and are enforced here rather than left to each page:

* **A figure never appears without what it was measured against.** `ScheduleRow` cannot
  carry an observed value without an `of_total` or a `note`, because the reviewer meets
  every screen cold and a bare number asks them to take it on trust.
* **A severity never appears without its hatch.** `state_plates` always draws the ordered
  hatch behind the count, so the triage reads in greyscale.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from html import escape
from typing import Final

import streamlit as st

from mdq.dashboard import theme

__all__ = [
    "Field",
    "Plate",
    "ScheduleRow",
    "head",
    "plates",
    "reading_note",
    "render_head",
    "render_plates",
    "render_reading_note",
    "render_schedule",
    "render_signature",
    "render_traceability",
    "report_number",
    "schedule",
    "signature",
    "traceability",
]

#: A typed field on the certificate head or in the countersignature block: label, value.
Field = tuple[str, str]


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    """One line of the schedule of results.

    Args:
        label: What was measured, set as a small tracked clause.
        observed: The measured figure, set large in the typed face.
        of_total: What it was measured against ("of 15,018 findings"). Either this or
            `note` must be present — see the module docstring.
        note: The threshold, definition or denominator that decided the figure.
        ratio: Where the deviation bar fills to, 0.0–1.0. `None` draws no bar.
        ratio_label: The ratio as a figure, printed under the bar. A bar drawn at 0.3% is
            a three-pixel sliver whatever else is done to it, so the number goes beside it
            rather than leaving the reader to estimate a share off a hairline.
        limit: Where the tolerance limit rule sits, 0.0–1.0. Only pass one where a real
            threshold exists — an invented limit is a fabricated measurement.
        failed: Whether this row is out of tolerance, which sets the fail ink.
    """

    label: str
    observed: str
    of_total: str | None = None
    note: str | None = None
    ratio: float | None = None
    ratio_label: str | None = None
    limit: float | None = None
    failed: bool = False

    def __post_init__(self) -> None:
        if not self.of_total and not self.note:
            raise ValueError(
                f"schedule row {self.label!r} has a figure with nothing to measure it "
                "against; give it an of_total or a note"
            )


@dataclass(frozen=True, slots=True)
class Plate:
    """A stamped state plate: a count, the state's name, and the state's hatch.

    Args:
        count: The figure, already formatted with thousands separators.
        name: The state, which is printed in words at plate scale rather than
            relying on colour — ERROR, WARNING, EXPECTED, DORMANT.
        severity: Which ordered hatch and ink to use. An unknown severity gets the
            expected-artefact treatment rather than raising.
    """

    count: str
    name: str
    severity: str
    rule: str | None = None
    """The threshold that decided this tier, printed on the plate.

    The product's second principle is that every figure carries its own justification, and
    a reader who has to open a JSON expander to find out what "expected" means has been
    handed a number on trust.
    """


def _pct(value: float) -> str:
    """A ratio as a bounded percentage, so a bad input cannot draw outside its track."""
    return f"{max(0.0, min(1.0, value)) * 100:.4g}%"


def report_number(*parts: object) -> str:
    """A short, deterministic report number derived from what is actually loaded.

    Honest by construction: it is a content hash of the datasets in front of the user, so
    the same data always prints the same number and different data never does. It is a run
    identifier and nothing else — this project issues no accreditation and claims none.
    """
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()
    return f"{digest[:4]}-{digest[4:8]}".upper()


def head(title: str, subtitle: str, fields: Sequence[Field]) -> str:
    """The reference-blue certificate head: what this document is, and its typed fields."""
    rows = "".join(f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in fields)
    return (
        '<div class="mdq-head">'
        f'<div class="mdq-head-block"><p class="mdq-head-title">{escape(title)}</p>'
        f'<p class="mdq-head-sub">{escape(subtitle)}</p></div>'
        f'<dl class="mdq-head-fields">{rows}</dl>'
        "</div>"
        '<div class="mdq-guilloche"></div>'
    )


def schedule(rows: Iterable[ScheduleRow]) -> str:
    """The schedule of results — the structure that replaces every KPI tile row."""
    drawn: list[str] = []
    for index, row in enumerate(rows):
        fail = " is-fail" if row.failed else ""
        cells = [
            f'<div class="mdq-row-label">{escape(row.label)}</div>',
            f'<div class="mdq-row-observed{fail}">{escape(row.observed)}</div>',
            f'<div class="mdq-row-of">{escape(row.of_total or "")}</div>',
        ]
        fourth = ""
        if row.ratio is not None:
            limit = (
                f'<span class="mdq-bar-limit" style="left:{_pct(row.limit)}"></span>'
                if row.limit is not None
                else ""
            )
            zero = " is-zero" if row.ratio <= 0 else ""
            fourth += (
                f'<div class="mdq-bar"><span class="mdq-bar-fill{fail}{zero}" '
                f'style="width:{_pct(row.ratio)};--i:{index}"></span>{limit}</div>'
            )
            if row.ratio_label:
                fourth += f'<span class="mdq-row-pct">{escape(row.ratio_label)}</span>'
        if row.note:
            fourth += f'<div class="mdq-row-note">{escape(row.note)}</div>'
        cells.append(f"<div>{fourth}</div>")
        drawn.append(f'<div class="mdq-row">{"".join(cells)}</div>')
    return f'<div class="mdq-schedule">{"".join(drawn)}</div>'


_PLATE_CLASS: Final[dict[str, str]] = {
    "ERROR": "is-error",
    "WARNING": "is-warning",
    "INFO": "is-info",
}


def plates(items: Iterable[Plate]) -> str:
    """Stamped state plates, each carrying its ordered hatch behind the count."""
    drawn: list[str] = []
    for item in items:
        severity = item.severity if item.severity in theme.HATCHES else "INFO"
        hatch = theme.HATCHES[severity].format(ink=theme.SEVERITY_INK[severity])
        size = "5px 5px" if severity == "INFO" else "auto"
        drawn.append(
            f'<div class="mdq-plate {_PLATE_CLASS[severity]}">'
            f'<span class="mdq-plate-hatch" style="background-image:{hatch};'
            f'background-size:{size}"></span>'
            f'<span class="mdq-plate-count">{escape(item.count)}</span>'
            f'<span class="mdq-plate-name">{escape(item.name)}</span>'
            + (f'<span class="mdq-plate-rule">{escape(item.rule)}</span>' if item.rule else "")
            + "</div>"
        )
    return f'<div class="mdq-plates">{"".join(drawn)}</div>'


def reading_note(lead: str, body: str) -> str:
    """The note on how to read the results — the claim, then what the tiers mean.

    Not a caption. A caption is for prose that qualifies something else, and this is the
    sentence the whole product is about: how many of the findings actually need a person.
    It is set in full ink at reading size with its claim in bold, because a reader meeting
    this dashboard cold should be able to take the argument off this one block.
    """
    return f'<p class="mdq-reading"><b>{escape(lead)}</b> {escape(body)}</p>'


def traceability(label: str, claim: str, figure: str | None = None) -> str:
    """The band naming the reference standard this run was measured against."""
    tail = f'<span class="mdq-trace-figure">{escape(figure)}</span>' if figure else ""
    return (
        '<div class="mdq-trace">'
        f'<span class="mdq-trace-label">{escape(label)}</span>'
        f'<span class="mdq-trace-claim">{escape(claim)}</span>{tail}'
        "</div>"
    )


def signature(fields: Sequence[Field]) -> str:
    """The countersignature block: who computed this, when, and under what thresholds."""
    parts = "".join(
        f"<span>{escape(label)} <b>{escape(value)}</b></span>" for label, value in fields
    )
    return f'<div class="mdq-sign">{parts}</div>'


# --------------------------------------------------------------------------- #
# The Streamlit surface. Thin on purpose: everything above is testable without it.
# --------------------------------------------------------------------------- #


def _write(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def render_head(title: str, subtitle: str, fields: Sequence[Field]) -> None:
    """Draw the certificate head."""
    _write(head(title, subtitle, fields))


def render_schedule(rows: Iterable[ScheduleRow]) -> None:
    """Draw the schedule of results."""
    _write(schedule(rows))


def render_plates(items: Iterable[Plate]) -> None:
    """Draw the stamped state plates."""
    _write(plates(items))


def render_reading_note(lead: str, body: str) -> None:
    """Draw the note on how to read the results."""
    _write(reading_note(lead, body))


def render_traceability(label: str, claim: str, figure: str | None = None) -> None:
    """Draw the traceability band."""
    _write(traceability(label, claim, figure))


def render_signature(fields: Sequence[Field]) -> None:
    """Draw the countersignature block."""
    _write(signature(fields))
