"""The certificate's builders, checked as strings rather than through a script runner.

These are the presentation equivalent of `test_dashboard_charts.py`: every function in
`mdq.dashboard.certificate` takes plain data and returns HTML, so the things that actually
go wrong in a presentation layer — a figure printed without the total it was measured
against, a severity that reads as colour alone, an instrument name that closes the tag it
was interpolated into — are assertable directly.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mdq.dashboard import certificate, theme

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# the schedule of results
# --------------------------------------------------------------------------- #


def test_a_figure_cannot_be_printed_with_nothing_to_measure_it_against() -> None:
    """The rule the whole page rests on, enforced at construction rather than in review.

    A reviewer meets every screen cold, with nobody to explain it. A bare number asks them
    to take it on trust, so a row carrying neither a total nor a note is a programming
    error and says so.
    """
    with pytest.raises(ValueError, match="nothing to measure it against"):
        certificate.ScheduleRow(label="Requiring review", observed="44")


def test_a_schedule_row_prints_the_figure_the_total_and_the_deciding_note() -> None:
    markup = certificate.schedule(
        [
            certificate.ScheduleRow(
                label="Requiring review",
                observed="44",
                of_total="of 15,018 findings",
                note="Decided by an activity regime observed per contract.",
                ratio=44 / 15_018,
            )
        ]
    )
    assert "Requiring review" in markup
    assert ">44<" in markup
    assert "of 15,018 findings" in markup
    assert "Decided by an activity regime observed per contract." in markup
    assert "mdq-bar-fill" in markup


def test_a_row_out_of_tolerance_marks_both_the_figure_and_its_bar() -> None:
    """Out of tolerance has to be legible on the number itself, not only on the bar."""
    markup = certificate.schedule(
        [
            certificate.ScheduleRow(
                label="Invalid OHLC",
                observed="5",
                of_total="of 43 bars",
                ratio=5 / 43,
                failed=True,
            )
        ]
    )
    assert "mdq-row-observed is-fail" in markup
    assert "mdq-bar-fill is-fail" in markup


def test_a_row_without_a_ratio_draws_no_bar() -> None:
    markup = certificate.schedule(
        [certificate.ScheduleRow(label="Bars", observed="5,325,341", of_total="rows")]
    )
    assert "mdq-bar" not in markup


def test_a_tolerance_limit_is_drawn_only_when_one_is_given() -> None:
    row = certificate.ScheduleRow(
        label="Minute coverage", observed="960", of_total="of 1,380 minutes", ratio=0.7
    )
    assert "mdq-bar-limit" not in certificate.schedule([row])
    with_limit = certificate.schedule([replace(row, limit=0.95)])
    assert "mdq-bar-limit" in with_limit
    assert "left:95%" in with_limit


def test_a_measured_zero_draws_nothing_but_a_real_sliver_survives() -> None:
    """The `min-width` floor must rescue a sliver without inventing one.

    On the Overview's schedule a check that left nothing for a person reads `0`, and a 3px
    mark beside it would say something different from what was measured.
    """
    zero = certificate.schedule(
        [certificate.ScheduleRow(label="Stale bar", observed="0", of_total="of 2,026", ratio=0.0)]
    )
    assert "mdq-bar-fill is-zero" in zero
    sliver = certificate.schedule(
        [certificate.ScheduleRow(label="x", observed="6", of_total="of 2,102", ratio=0.0029)]
    )
    assert "is-zero" not in sliver


@pytest.mark.parametrize(("ratio", "expected"), [(-4.0, "0%"), (0.0, "0%"), (12.5, "100%")])
def test_a_ratio_outside_its_track_is_clamped_rather_than_drawn_outside_it(
    ratio: float, expected: str
) -> None:
    """A bad denominator upstream must not paint a bar across the rest of the page."""
    markup = certificate.schedule(
        [certificate.ScheduleRow(label="x", observed="1", of_total="y", ratio=ratio)]
    )
    assert f"width:{expected}" in markup


# --------------------------------------------------------------------------- #
# stamped state plates
# --------------------------------------------------------------------------- #


def test_every_plate_carries_its_state_in_words_and_its_own_hatch() -> None:
    """Severity may never ride on hue alone — the hatch is the second channel."""
    markup = certificate.plates(
        [
            certificate.Plate("5", "Error", "ERROR"),
            certificate.Plate("39", "Warning", "WARNING"),
            certificate.Plate("14,974", "Expected", "INFO"),
        ]
    )
    for name in ("Error", "Warning", "Expected"):
        assert f'mdq-plate-name">{name}<' in markup
    # Three tiers, three different hatches, so a greyscale print still separates them.
    hatches = {shape.split("(")[0] for shape in theme.HATCHES.values()}
    assert len(hatches) >= 2
    assert markup.count("mdq-plate-hatch") == 3
    assert "repeating-linear-gradient(-45deg" in markup  # the error cross-hatch
    assert "radial-gradient" in markup  # the expected-artefact stipple


def test_a_plate_prints_the_threshold_that_decided_its_tier() -> None:
    """The product's second principle: no figure without its justification beside it."""
    markup = certificate.plates(
        [certificate.Plate("2,096", "Expected", "INFO", rule="Session was THIN or DORMANT.")]
    )
    assert "Session was THIN or DORMANT." in markup
    assert "mdq-plate-rule" in markup
    assert "mdq-plate-rule" not in certificate.plates(
        [certificate.Plate("2,096", "Expected", "INFO")]
    )


def test_the_deciding_thresholds_are_read_from_the_report_not_restated() -> None:
    """They are overridable from the environment, so a hardcoded sentence would go stale.

    `MDQ_QUALITY__DAILY_THIN_VOLUME_RATIO=0.05` has to change what the plate says, or the
    page is asserting a threshold the report was not actually run with.
    """
    from mdq.dashboard import ui

    rules = ui.plate_rules({"activity": {"daily_thin_volume_ratio": 0.05}})
    assert "5%" in rules["INFO"]
    assert "volume is zero" in rules["INFO"]
    # A config that carries no threshold still produces a readable rule.
    bare = ui.plate_rules({})
    assert "THIN" in bare["INFO"] and "%" not in bare["INFO"]


def test_the_ratio_is_printed_as_a_figure_beside_its_bar() -> None:
    """A 0.3% bar is a sliver at any size, so the number carries the meaning."""
    markup = certificate.schedule(
        [
            certificate.ScheduleRow(
                label="Requiring review",
                observed="44",
                of_total="of 15,018 findings",
                ratio=44 / 15_018,
                ratio_label="0.29% of all findings",
            )
        ]
    )
    assert "0.29% of all findings" in markup
    assert "mdq-row-pct" in markup


def test_an_unknown_severity_is_treated_as_expected_rather_than_raising() -> None:
    """A new severity upstream must degrade, never take a page down."""
    markup = certificate.plates([certificate.Plate("1", "Fatal", "CATASTROPHE")])
    assert "is-info" in markup
    assert 'mdq-plate-name">Fatal<' in markup


# --------------------------------------------------------------------------- #
# head, traceability, countersignature
# --------------------------------------------------------------------------- #


def test_the_head_prints_every_field_it_is_given() -> None:
    markup = certificate.head(
        "Market Data Quality & Analytics",
        "Quality certificate",
        [("Report no.", "1A2B-3C4D"), ("Bars", "5,325,341")],
    )
    assert "Market Data Quality &amp; Analytics" in markup
    assert "<dt>Report no.</dt><dd>1A2B-3C4D</dd>" in markup
    assert "mdq-guilloche" in markup


def test_the_reading_note_sets_its_claim_apart_from_the_definitions() -> None:
    """The page's central sentence is a statement, not a caption.

    It was a `st.caption` once, which this theme renders at 0.8rem in muted ink on a
    two-thirds measure — footnote treatment for the one claim the product is about.
    """
    markup = certificate.reading_note(
        "44 of 15,018 findings need a human.",
        "An error is a bar that cannot be right.",
    )
    assert "<b>44 of 15,018 findings need a human.</b>" in markup
    assert "An error is a bar that cannot be right." in markup
    assert 'class="mdq-reading"' in markup


def test_the_traceability_band_names_the_standard_and_its_figure_when_given_one() -> None:
    assert "mdq-trace-figure" not in certificate.traceability("Reference standard", "A claim.")
    with_figure = certificate.traceability("Reference standard", "A claim.", "40/40")
    assert "40/40" in with_figure


def test_the_countersignature_prints_its_fields_in_order() -> None:
    markup = certificate.signature([("Prepared by", "mdq 0.1.0"), ("Source", "in-process")])
    assert markup.index("Prepared by") < markup.index("Source")
    assert "<b>mdq 0.1.0</b>" in markup


@pytest.mark.parametrize(
    "builder",
    [
        lambda bad: certificate.head(bad, bad, [(bad, bad)]),
        lambda bad: certificate.schedule(
            [certificate.ScheduleRow(label=bad, observed=bad, of_total=bad, note=bad)]
        ),
        lambda bad: certificate.plates([certificate.Plate(bad, bad, "ERROR")]),
        lambda bad: certificate.traceability(bad, bad, bad),
        lambda bad: certificate.signature([(bad, bad)]),
    ],
)
def test_nothing_interpolated_into_the_certificate_can_close_a_tag(builder) -> None:  # type: ignore[no-untyped-def]
    """Contract codes and file names come from uploaded data, so they are escaped.

    Not hypothetical: a dataset label is whatever the uploaded file was called, and the
    findings carry instrument codes straight from the source column.
    """
    assert "<script>" not in builder('</div><script>alert("x")</script>')


# --------------------------------------------------------------------------- #
# the report number
# --------------------------------------------------------------------------- #


def test_the_report_number_is_the_same_for_the_same_data_and_different_for_different_data() -> None:
    """It is a content hash, which is the only honest way to print one of these.

    The number identifies the run it was computed from and claims nothing else: this
    project issues no accreditation.
    """
    one = certificate.report_number(["CLZ25", "ESH26"], 5_325_341)
    assert one == certificate.report_number(["CLZ25", "ESH26"], 5_325_341)
    assert one != certificate.report_number(["CLZ25", "ESH26"], 5_325_342)
    assert len(one) == 9 and one[4] == "-"


# --------------------------------------------------------------------------- #
# the stylesheet and the chart template
# --------------------------------------------------------------------------- #


def test_the_stylesheet_ships_its_own_faces_rather_than_fetching_them() -> None:
    """A reviewer on a locked-down network must still see the real typography."""
    css = theme.stylesheet()
    assert css.count("@font-face") == 3
    assert "src:url(data:font/woff2;base64," in css
    assert "fonts.googleapis.com" not in css
    assert "fonts.gstatic.com" not in css


def test_the_stylesheet_defines_every_token_the_rules_reference() -> None:
    css = theme.stylesheet()
    for token in ("--stock", "--ink", "--reference", "--fail", "--warn", "--expected"):
        assert f"{token}:" in css, f"{token} is used by the rules but never defined"


def test_the_stylesheet_survives_the_percent_signs_and_braces_in_its_own_css() -> None:
    """A regression guard, and it caught a real bug rather than guarding a hypothetical.

    The stylesheet contains a literal `%` (a full-width rule in the narrow block) and 91
    pairs of braces, which is exactly what `%`-formatting and `str.format` consume. The
    palette is therefore concatenated as its own `:root` block rather than interpolated
    into the CSS, and this asserts the CSS came through whole with nothing left to expand.
    """
    css = theme.stylesheet()
    assert "width:100%" in css
    assert "%(" not in css
    assert "{stock}" not in css and "{ink}" not in css


def test_the_chart_template_is_registered_and_registering_twice_is_harmless() -> None:
    import plotly.io as pio

    name = theme.register_template()
    assert theme.register_template() == name
    assert pio.templates[name].layout.paper_bgcolor == theme.STOCK


def test_severity_ink_and_hatches_cover_exactly_the_same_three_tiers() -> None:
    """One word means one colour and one pattern, or the second channel is a lie."""
    assert set(theme.SEVERITY_INK) == set(theme.HATCHES) == set(theme.PATTERN_SHAPES)
    assert len(set(theme.SEVERITY_INK.values())) == 3
    assert len(set(theme.PATTERN_SHAPES.values())) == 3
