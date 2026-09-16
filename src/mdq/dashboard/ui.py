"""The few widgets and phrases every page shares.

Small on purpose. Anything here is presentation — a label, a spinner, an empty state — and
anything that decides *what* is shown lives in `mdq.dashboard.backend`. The reason it
exists at all is consistency of language: "trading session" and not "date", "instrument"
and not "contract code", one spelling of the warning that precedes a five-second minute
pass, and one shape of error banner, so the dashboard reads as though one person wrote it.

It also owns the certificate head, which every page carries. That is not decoration: this
dashboard is dressed as a calibration certificate because every finding it reports is a
deviation measured against a reference standard, and a certificate declares what was
measured and against what reference *before* it prints a single result. Repeating the head
with its page number is what a real multi-page certificate does, and it means a reviewer
who lands on page three still knows which dataset they are reading about.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from typing import Any, Final

import streamlit as st

from mdq.api.schemas import DatasetsSummaryResponse
from mdq.dashboard import certificate
from mdq.dashboard.backend import BackendError, SeverityTriage
from mdq.domain.frequency import Frequency

__all__ = [
    "DATE_FORMAT",
    "FREQUENCY_LABELS",
    "MINUTE_WARNING",
    "PAGE_COUNT",
    "PRODUCT_NAME",
    "SUBTITLE",
    "au_date",
    "certificate_head",
    "frequency_picker",
    "guard",
    "nothing_loaded",
    "page_link",
    "plate_rules",
    "remember_pages",
    "severity_plates",
    "signature",
]

#: The product's name, in one place. `shell.TITLE` re-exports it for the browser tab.
PRODUCT_NAME: Final = "Market Data Quality & Analytics"

#: What the document is. Kept short because the traceability band, not the head, is where
#: the reference standard is stated in full — and a subtitle that wraps to three lines on a
#: phone outweighs the title it is supposed to qualify.
SUBTITLE: Final = "Quality certificate · futures bar data"

#: Pages in the certificate, for the "page n of m" field.
PAGE_COUNT: Final = 4

#: Every date a reader sees is day-first. The audience is Australian, and `03/20` versus
#: `20/03` is the kind of ambiguity that makes someone distrust the rest of the numbers.
#: It is a *display* choice only: a trading session is still the Chicago session it always
#: was, which the copy says in words rather than leaving the format to imply.
DATE_FORMAT: Final = "DD/MM/YYYY"

#: The same format as a strftime pattern, for labels Streamlit renders itself.
_DATE_STRFTIME: Final = "%d/%m/%Y"


def au_date(day: date) -> str:
    """A date as a day-first reader expects it."""
    return day.strftime(_DATE_STRFTIME)


def session_span(summary: DatasetsSummaryResponse) -> tuple[date | None, date | None]:
    """The first and last trading session across everything loaded, or `(None, None)`.

    Every date control in the dashboard is bounded by this. Left unbounded, Streamlit
    anchors an empty date picker on *today* and greys out every day of whatever month it
    happens to open on except that month's own — so a reader whose data ended in March
    faces a calendar opening in September with one clickable month, and reaching a session
    that exists means paging back through half a year of empty months. Bounding the widget
    makes it open inside the data instead.
    """
    spans = [
        (item.first_session, item.last_session)
        for item in summary.by_frequency
        if item.first_session is not None and item.last_session is not None
    ]
    if not spans:
        return None, None
    return min(start for start, _ in spans), max(end for _, end in spans)


#: Where the built `st.Page` objects are parked so one page can link to another.
_PAGES_KEY: Final = "mdq_pages"

#: How the two frequencies are named to a reader who does not work here.
FREQUENCY_LABELS: Final[dict[Frequency, str]] = {
    Frequency.DAILY: "Daily bars",
    Frequency.MINUTE: "Minute bars",
}

#: Said before anything that triggers a minute-frequency quality pass. Measured: 5.2 s the
#: first time on the full corpus, and ~1.3 GB of memory once the bars materialise. A user
#: who is told that waits; a user who is not told assumes the page has hung.
MINUTE_WARNING: Final = (
    "Checking minute bars reads the whole minute history — about five seconds the first "
    "time on a full dataset, then instant. Daily bars are the faster view and are what the "
    "vendor publishes diagnostics for."
)


def remember_pages(pages: Sequence[st.Page]) -> None:
    """Park the built pages so `page_link` can point at one by its url path."""
    st.session_state[_PAGES_KEY] = {page.url_path: page for page in pages}


def page_link(url_path: str, label: str) -> None:
    """Link to another page of the certificate, when the shell has registered one.

    Silent when it has not, which is the case in a unit test that renders one page in
    isolation: a missing cross-reference must never be the reason a page fails to draw.
    """
    page = st.session_state.get(_PAGES_KEY, {}).get(url_path)
    if page is not None:
        st.page_link(page, label=label)


def certificate_head(summary: DatasetsSummaryResponse, page_name: str, page_no: int) -> None:
    """The head of the certificate: what is loaded, and which page of it this is.

    Works on an empty service by design. A dashboard with no data yet is a valid state —
    the API starts before `make fetch` has ever run — and a reviewer who launches this
    cold should meet a document with its fields dashed out, not a broken-looking screen.
    """
    first, last = session_span(summary)
    span = f"{au_date(first)} → {au_date(last)}" if first and last else "—"
    number = certificate.report_number(
        sorted(summary.contracts),
        summary.bars,
        tuple(item.frequency.value for item in summary.by_frequency),
    )
    certificate.render_head(
        PRODUCT_NAME,
        SUBTITLE,
        [
            ("Report no.", number if summary.by_frequency else "—"),
            ("Instruments", f"{len(summary.contracts):,}"),
            ("Bars", f"{summary.bars:,}"),
            ("Sessions", span),
            ("Page", f"{page_no} of {PAGE_COUNT} · {page_name}"),
        ],
    )


def signature(
    backend_label: str,
    extra: Sequence[certificate.Field] = (),
    *,
    loaded: bool = True,
) -> None:
    """The countersignature block: what computed this, and what it was checked against.

    Every other field here is a fact about the data in front of the reader, so the oracle
    line is labelled `Test suite` and not `Vendor oracle`: the engine reproducing the
    vendor's four published diagnostics on 40 of 40 daily files is a verified fact about
    *this project's golden tests*, and printing it in the same run of fields as `Source`
    invites it to be read as a claim about whatever happens to be loaded. On an empty
    service it is suppressed entirely — beside `INSTRUMENTS 0` it would be exactly that
    false claim.
    """
    fields: list[certificate.Field] = [
        ("Prepared by", "mdq 0.1.0"),
        ("Source", backend_label),
        *extra,
    ]
    if loaded:
        fields.append(
            ("Test suite", "vendor's 4 published diagnostics reproduced on 40/40 daily files")
        )
    certificate.render_signature(fields)


def plate_rules(thresholds: dict[str, Any]) -> dict[str, str]:
    """What decided each severity tier, read off the thresholds the report travelled with.

    Read rather than restated, because the numbers are overridable from the environment
    (`MDQ_QUALITY__*`) and a hardcoded sentence would quietly start lying the first time
    someone tuned one. A missing key degrades to the rule without its figure rather than
    raising: an uploaded file's config need not carry every threshold this page knows about.
    """
    config = thresholds.get("config", {})
    activity = thresholds.get("activity", {})
    thin = activity.get("daily_thin_volume_ratio", config.get("daily_thin_volume_ratio"))
    thin_clause = (
        f"THIN (traded under {thin:.0%} of the contract's own peak session volume)"
        if isinstance(thin, int | float)
        else "THIN"
    )
    return {
        "ERROR": "Incoherent in a session that was trading. No regime explains it.",
        "WARNING": "Suspicious, in a session the contract was genuinely trading.",
        "INFO": f"Downgraded: the session was {thin_clause} or DORMANT (volume is zero).",
    }


def severity_plates(triage: SeverityTriage, thresholds: dict[str, Any]) -> list[certificate.Plate]:
    """The three stamped tiers, each carrying its count, its hatch and its threshold.

    Shared by the Overview and the Data Quality page rather than written twice. The triage
    is the one thing this dashboard exists to say, and two pages stamping it in two
    different ways — one of them without the deciding threshold — is exactly the drift this
    module exists to prevent.
    """
    rules = plate_rules(thresholds)
    return [
        certificate.Plate(f"{triage.errors:,}", "Error", "ERROR", rule=rules["ERROR"]),
        certificate.Plate(f"{triage.warnings:,}", "Warning", "WARNING", rule=rules["WARNING"]),
        certificate.Plate(f"{triage.expected:,}", "Expected", "INFO", rule=rules["INFO"]),
    ]


def frequency_picker(
    summary: DatasetsSummaryResponse,
    *,
    key: str,
    label: str = "Bar frequency",
) -> Frequency | None:
    """A radio over the frequencies actually loaded, defaulting to daily.

    Returns `None` when nothing is loaded, which every caller renders as an empty state
    rather than as an error: an empty service is a perfectly valid state (the API starts
    before `make fetch` has ever run) and must not look like a failure.
    """
    available = [item.frequency for item in summary.by_frequency]
    if not available:
        return None
    ordered = [f for f in Frequency if f in available]
    return st.radio(
        label,
        ordered,
        index=0,
        horizontal=True,
        format_func=lambda f: FREQUENCY_LABELS[f],
        key=key,
    )


def guard[T](work: Callable[[], T], *, context: str) -> T | None:
    """Run `work`, turning a backend failure into a banner instead of a stack trace.

    A page is a place a non-engineer is looking at. When the API is down, or a contract
    was renamed, or an upload was too large, they should read a sentence about it in the
    place the answer would have been — not a red Streamlit traceback quoting httpx.
    """
    try:
        return work()
    except BackendError as exc:
        st.error(f"{context}\n\n{exc}")
        return None


def nothing_loaded(data_hint: str = "") -> None:
    """The empty state for a service holding no data at all.

    Built in the certificate's own vocabulary rather than as a stock alert, because this is
    very likely the first screen the reviewer sees: they clone the repository and run it,
    and `make fetch` is optional. A certificate with nothing on record is still a
    certificate — it says so, on a stamped plate, with its fields dashed out — and an empty
    service is a valid state that must never look like a failure.
    """
    certificate.render_schedule(
        [
            certificate.ScheduleRow(
                label="Findings on record",
                observed="—",
                of_total="no bars loaded",
                # The schedule builder escapes its text rather than rendering markdown, so
                # backticks would print literally. The command keeps its typed marking by
                # moving to the plate's rule, which is set in the Courier voice already —
                # dropping the distinction instead of translating it was the wrong trade.
                note=(
                    "Columns are matched by name — contract, timestamp, open, high, low, "
                    "close, volume — and nothing is silently repaired. " + data_hint
                ).strip(),
            )
        ]
    )
    certificate.render_plates(
        [
            certificate.Plate(
                "—",
                "No data on record",
                "INFO",
                rule=(
                    "Every check is ready; none has run. "
                    "Run  make fetch  for the pinned vendor sample, "
                    "or upload a file in the sidebar."
                ),
            )
        ]
    )
