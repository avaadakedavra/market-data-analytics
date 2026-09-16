"""The precision/recall harness.

The artifact this generator writes makes a strong claim — every check finds what it is
supposed to find, names the right bars and triages them correctly — and a claim like that
belongs in CI rather than in a committed table nobody re-runs. So the interesting tests
here are the ones that would go red if the claim stopped being true:

* every check still detects everything declared for it (`fn == 0`, `tp > 0`);
* no check mis-triages or names a wrong row;
* the **one** measured false positive is still the one that has been read and explained,
  and is still the fault's doing rather than the check's.

That last one is the point of scoring rather than asserting. `assert_findings_match` is the
only thing in the suite that forbids an *unexpected* check, and no test applies it to
`AmbiguousLocalTime(bars=2, passes=2)`: `test_faults.py` only inspects the corrupted frame,
and `test_insights_rules.py` runs it through `run_checks` but asserts one insight rule's
evidence rather than the set of checks that fired. A third check has therefore been firing
on it inside a passing test. Scoring found it immediately, and it is pinned here from both
sides so neither the fault nor the check can drift without someone reading this file.

`score()` itself is tested on hand-built reports, because a scoring rule that is only
exercised by data that happens to be perfect is not tested at all.

Everything is synthetic. Nothing here reads `data/`; the benchmark test falls back to the
three committed parquet fixtures.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import polars as pl
import pytest

from mdq.domain.findings import Finding, Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import FINDING_SCHEMA, C, empty_finding_df
from mdq.quality import CheckRegistry, QualityReport
from measure_metrics import (
    BENCHMARKS_NAME,
    DEFAULT_OUT_DIR,
    METRICS_NAME,
    SCENARIOS,
    Evaluation,
    Tally,
    evaluate,
    main,
    render_benchmarks,
    render_metrics,
    run_benchmarks,
    run_scenario,
    score,
)
from support import faults

pytestmark = pytest.mark.unit

_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T")

#: Departures from a perfect confusion matrix, as `(check_id, scenario)`. Empty, and the
#: emptiness is the assertion — see `test_there_are_no_false_positives_anywhere`.
#:
#: It was not empty when the harness was first run. `stale_bar` measured precision 0.200
#: on the `ambiguous_local_time` scenario, and the check turned out to be right:
#: `AmbiguousLocalTime` builds its bars with `open == high == low == close == 100.0` — it
#: cares about the wall clock and picks a price arbitrarily — which satisfies `stale_bar`'s
#: detection predicate exactly. `stale_bar` matches the vendor's own no-range definition,
#: which deliberately does not mention volume (14,152 rows against 12,841 with a volume
#: clause), so it reports them, at INFO because they were traded.
#:
#: The fault was declaring two of its three consequences. `Expected` is documented as
#: "every finding the engine must produce, and nothing more", so a short list is a defect
#: in the declaration, and the fix was to declare the third — not to drop the scenario or
#: soften this table. What let it hide is worth remembering: `assert_findings_match` is the
#: only assertion that forbids an *undeclared* check, and it is never run against this
#: fault, so the finding had been appearing inside a passing test.
KNOWN_FALSE_POSITIVES: frozenset[tuple[str, str]] = frozenset()


# --------------------------------------------------------------------------- #
# the catalogue
# --------------------------------------------------------------------------- #


def test_the_catalogue_covers_every_shipped_fault() -> None:
    """A fault nobody scores is a fault whose precision nobody knows."""
    covered = {type(fault) for scenario in SCENARIOS for fault in scenario.faults}
    assert covered == set(faults.ALL_FAULTS)

    names = [scenario.name for scenario in SCENARIOS]
    assert len(set(names)) == len(names)
    assert {scenario.frequency for scenario in SCENARIOS} == set(Frequency)
    assert [s for s in SCENARIOS if not s.faults], "the clean-data guard must be in the list"
    assert all(scenario.note for scenario in SCENARIOS), "every scenario must say why it exists"


def test_every_check_is_expected_by_at_least_one_scenario() -> None:
    """Otherwise a check would show `n/a` recall and the artifact would say nothing of it."""
    expected_ids = {
        expectation.check_id for scenario in SCENARIOS for expectation in run_scenario(scenario)[1]
    }
    assert set(CheckRegistry.ids()) <= expected_ids


# --------------------------------------------------------------------------- #
# score() — the rule, on reports built to break it
# --------------------------------------------------------------------------- #


def _finding(
    check_id: str,
    count: int,
    severity: Severity = Severity.ERROR,
    row_ids: tuple[int, ...] = (),
) -> Finding:
    return Finding(
        check_id=check_id,
        severity=severity,
        contract="ESH26",
        frequency=Frequency.MINUTE,
        message=f"{count} bar(s)",
        count=count,
        evidence={"row_ids": list(row_ids)},
    )


def _report(*findings: Finding, checks_run: tuple[str, ...] = ("invalid_ohlc",)) -> QualityReport:
    """A report built the way `registry._check_failed` builds one, from `Finding.to_row()`."""
    frame = (
        pl.DataFrame([f.to_row() for f in findings], schema=FINDING_SCHEMA)
        if findings
        else empty_finding_df()
    )
    return QualityReport(frame, Frequency.MINUTE, checks_run=checks_run)


def test_score_counts_an_exact_match_as_true_positives_only() -> None:
    report = _report(_finding("invalid_ohlc", 2, row_ids=(1, 2)))
    expected = [faults.Expected("invalid_ohlc", 2, (1, 2), Severity.ERROR)]
    assert score(report, expected)["invalid_ohlc"] == Tally(tp=2)


def test_score_charges_the_excess_of_an_over_count_as_false_positives() -> None:
    """Three bars reported where two were broken: it found the defect and over-reached."""
    report = _report(_finding("invalid_ohlc", 3, row_ids=(1, 2, 3)))
    expected = [faults.Expected("invalid_ohlc", 2, (1, 2), Severity.ERROR)]
    tally = score(report, expected)["invalid_ohlc"]
    assert (tally.tp, tally.fp, tally.fn) == (2, 1, 0)
    assert tally.precision == pytest.approx(2 / 3)
    assert tally.recall == 1.0


def test_score_charges_the_shortfall_of_an_under_count_as_false_negatives() -> None:
    report = _report(_finding("invalid_ohlc", 1, row_ids=(1,)))
    expected = [faults.Expected("invalid_ohlc", 3, (1,), Severity.ERROR)]
    tally = score(report, expected)["invalid_ohlc"]
    assert (tally.tp, tally.fp, tally.fn) == (1, 0, 2)
    assert tally.precision == 1.0
    assert tally.recall == pytest.approx(1 / 3)


def test_score_charges_a_wholly_unexpected_check_for_its_entire_output() -> None:
    """The `unexpected` branch of `assert_findings_match`, as a number instead of a raise."""
    report = _report(
        _finding("invalid_ohlc", 2, row_ids=(1, 2)),
        _finding("stale_bar", 5, Severity.INFO, row_ids=(7,)),
        checks_run=("invalid_ohlc", "stale_bar"),
    )
    tallies = score(report, [faults.Expected("invalid_ohlc", 2, (1, 2), Severity.ERROR)])
    assert tallies["invalid_ohlc"] == Tally(tp=2)
    assert tallies["stale_bar"] == Tally(fp=5)
    assert tallies["stale_bar"].precision == 0.0
    assert tallies["stale_bar"].recall is None


def test_score_keeps_a_wrong_severity_out_of_the_confusion_matrix() -> None:
    """Detection and triage are different failures; folding them would hide which broke."""
    report = _report(_finding("invalid_ohlc", 2, Severity.INFO, row_ids=(1, 2)))
    expected = [faults.Expected("invalid_ohlc", 2, (1, 2), Severity.WARNING)]
    tally = score(report, expected)["invalid_ohlc"]
    assert (tally.tp, tally.fp, tally.fn) == (2, 0, 0)
    assert tally.severity_misses == 1
    assert tally.precision == 1.0


def test_score_notices_the_right_count_on_the_wrong_rows() -> None:
    report = _report(_finding("invalid_ohlc", 2, row_ids=(8, 9)))
    expected = [faults.Expected("invalid_ohlc", 2, (1, 2), Severity.ERROR)]
    tally = score(report, expected)["invalid_ohlc"]
    assert (tally.tp, tally.fp, tally.fn) == (2, 0, 0)
    assert tally.row_id_misses == 2


def test_score_makes_every_finding_a_false_positive_when_nothing_was_expected() -> None:
    """`ShiftWallClockToUtc` is this case: a precision-only scenario."""
    report = _report(_finding("invalid_ohlc", 4, row_ids=(1,)))
    tally = score(report, [])["invalid_ohlc"]
    assert tally == Tally(fp=4)
    assert tally.recall is None
    assert tally.f1 is None


def test_a_silent_check_that_was_expected_to_speak_is_all_false_negatives() -> None:
    tally = score(_report(), [faults.Expected("invalid_ohlc", 3)])["invalid_ohlc"]
    assert tally == Tally(fn=3)
    assert tally.precision is None
    assert tally.recall == 0.0
    assert tally.f1 is None


def test_tallies_add_componentwise() -> None:
    total = Tally(tp=1, fp=2, fn=3, severity_misses=4, row_id_misses=5) + Tally(tp=10)
    assert total == Tally(tp=11, fp=2, fn=3, severity_misses=4, row_id_misses=5)


# --------------------------------------------------------------------------- #
# the whole catalogue
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def evaluation() -> Evaluation:
    """One pass over the catalogue, shared — it runs 29 scenarios through `run_checks`."""
    return evaluate()


def test_no_check_ever_misses_a_declared_finding(evaluation: Evaluation) -> None:
    """Recall is 1.000 everywhere. This is the half of the claim that holds universally."""
    per_check = evaluation.per_check
    for check_id in CheckRegistry.ids():
        tally = per_check[check_id]
        assert tally.tp > 0, f"{check_id} detected nothing in any scenario"
        assert tally.fn == 0, f"{check_id} missed {tally.fn} declared units"
        assert tally.recall == 1.0, check_id


def test_no_check_mis_triages_or_names_the_wrong_rows(evaluation: Evaluation) -> None:
    """Severity and evidence are scored separately, and both are clean everywhere."""
    per_check = evaluation.per_check
    for check_id in CheckRegistry.ids():
        tally = per_check[check_id]
        assert tally.severity_misses == 0, check_id
        assert tally.row_id_misses == 0, check_id


def test_no_check_ever_crashed_on_the_catalogue(evaluation: Evaluation) -> None:
    """A `check_failed` finding would mean a check raised rather than reported."""
    assert "check_failed" not in evaluation.per_check


def test_there_are_no_false_positives_anywhere(evaluation: Evaluation) -> None:
    """Every check reports exactly what its scenario declares, and nothing else.

    A new false positive anywhere fails this. That is the whole point: a check firing
    somewhere no fault declared it is either the check over-reaching or a declaration
    gone short, and both want a human reading the diff rather than watching a green
    build. This assertion is the thing that was missing — `assert_findings_match` forbids
    an undeclared check but is not run against every fault, so one had been firing inside
    a passing test until the harness measured it.
    """
    observed = {
        (check_id, result.scenario.name)
        for result in evaluation.results
        for check_id, tally in result.tallies.items()
        if tally.fp
    }
    assert observed == set(KNOWN_FALSE_POSITIVES)


def test_the_flat_bars_ambiguous_local_time_appends_really_are_stale() -> None:
    """The `stale_bar` declaration on that fault is earned, not asserted into place.

    It would be easy to silence a false positive by adding an `Expected` entry for
    whatever the check happened to emit, which is laundering with extra steps. So this
    proves the declaration from the data: the appended bars carry
    `open == high == low == close` and a non-zero volume, which is precisely a
    flat-but-traded bar — INFO under `stale_bar`'s vendor-matching definition, whose
    no-range predicate deliberately does not mention volume. The check reports those rows
    and no others, at that severity.
    """
    scenario = next(s for s in SCENARIOS if s.name == "ambiguous_local_time")
    frame, _, expected = faults.inject_with_rejects(scenario.frame(), *scenario.faults)
    appended = frame.filter(pl.col(C.ROW_ID) >= scenario.frame().height)

    assert appended.height == 4
    flat = appended.filter(
        (pl.col(C.OPEN) == pl.col(C.HIGH))
        & (pl.col(C.HIGH) == pl.col(C.LOW))
        & (pl.col(C.LOW) == pl.col(C.CLOSE))
    )
    assert flat.height == appended.height, "the appended bars are flat by construction"
    assert (appended.get_column(C.VOLUME) > 0).all(), "and traded, so INFO is correct"

    declared = next(e for e in expected if e.check_id == "stale_bar")
    assert declared.count == appended.height, "the declaration covers every flat bar"
    assert declared.severity is Severity.INFO
    assert set(declared.row_ids) == set(appended.get_column(C.ROW_ID).to_list())

    report = run_scenario(scenario)[0]
    stale = report.filter(check_id="stale_bar")
    assert stale.findings.get_column(C.SEVERITY).to_list() == [Severity.INFO.label]
    assert set(stale.rows_affected(Severity.INFO)) == set(appended.get_column(C.ROW_ID).to_list())


def test_clean_guard_is_all_zero(evaluation: Evaluation) -> None:
    """A check that fires on clean data has precision 0 however good its recall."""
    clean = evaluation.clean
    assert set(clean) == set(CheckRegistry.ids())
    for check_id, row in clean.items():
        for name, value in row.items():
            assert value in (0, None), f"{check_id} produced {value} findings on {name}"
    assert evaluation.clean_sizes, "the guard must name its sample sizes"


# --------------------------------------------------------------------------- #
# the artifacts
# --------------------------------------------------------------------------- #


def test_render_is_deterministic(evaluation: Evaluation) -> None:
    first = render_metrics(evaluation)
    assert first == render_metrics(evaluation)
    assert not _CLOCK.search(first)
    assert socket.gethostname() not in first
    assert "## Where the matrix is not 1.000" in first
    for check_id in CheckRegistry.ids():
        assert f"`{check_id}`" in first


def test_benchmarks_run_on_fixtures_and_name_their_samples(tmp_path: Path) -> None:
    """A timing without its sample beside it is worse than no timing at all."""
    benchmarks = run_benchmarks(tmp_path / "none", repeats=1)
    assert len(benchmarks.timings) == 5
    for timing in benchmarks.timings:
        assert timing.seconds and all(s > 0 for s in timing.seconds)
        assert timing.fastest <= timing.median
        assert re.search(r"\d", timing.sample), f"{timing.name} does not name its sample"

    rendered = render_benchmarks(benchmarks)
    assert "Repeats: 1" in rendered
    assert "polars" in rendered
    assert "Not measured, deliberately" in rendered
    assert not _CLOCK.search(rendered)
    assert socket.gethostname() not in rendered


def test_main_writes_metrics_only_by_default(tmp_path: Path) -> None:
    """Timings are a deliberate act: `metrics.md` is byte-asserted and must not carry one."""
    assert main(["--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / METRICS_NAME).exists()
    assert not (tmp_path / BENCHMARKS_NAME).exists()


def test_main_writes_benchmarks_when_asked(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert (
        main(
            [
                "--out-dir",
                str(out),
                "--data-dir",
                str(tmp_path / "none"),
                "--benchmarks",
                "--repeats",
                "1",
            ]
        )
        == 0
    )
    assert (out / METRICS_NAME).exists()
    assert "tests/fixtures" in (out / BENCHMARKS_NAME).read_text(encoding="utf-8")


def test_the_committed_metrics_are_what_the_generator_produces(evaluation: Evaluation) -> None:
    """The committed table and the generator must not be allowed to drift apart.

    Same contract as `test_the_committed_csvs_are_what_the_generator_produces`: the file
    in the repository is evidence, and evidence that no longer matches the code that
    produced it is worse than none. A Polars upgrade that moved a count would fail here,
    and the fix is to regenerate and read the diff — never to loosen this assertion.
    """
    committed = (DEFAULT_OUT_DIR / METRICS_NAME).read_text(encoding="utf-8")
    assert committed == render_metrics(evaluation)


def test_the_committed_benchmarks_name_their_sample_but_are_not_asserted() -> None:
    """Timings are checked for the things that must be true, not for their values.

    They vary run to run, so the file is deliberately outside the byte-equality contract.
    What it must still carry is what qualifies the numbers — environment, corpus, sample
    sizes — and nothing that would make a diff noisy for no reason.
    """
    benchmarks = (DEFAULT_OUT_DIR / BENCHMARKS_NAME).read_text(encoding="utf-8")
    assert "Corpus: data/raw" in benchmarks, "the committed timings must be full-corpus"
    assert "polars" in benchmarks
    assert "Repeats:" in benchmarks
    assert "Not measured, deliberately" in benchmarks
    assert not _CLOCK.search(benchmarks)
    assert socket.gethostname() not in benchmarks
