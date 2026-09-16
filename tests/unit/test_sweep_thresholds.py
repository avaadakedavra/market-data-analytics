"""The sensitivity sweep.

Two different things need guarding, and they fail for different reasons.

The **sweep's premises** are claims about the engine written down in the generator:
that every swept value is inside `QualityConfig`'s bounds, and that `READ_BY` names the
frequency whose code actually reads each threshold. `model_copy(update=...)` does not
validate, so an out-of-range value would sweep silently to a nonsense result; and the
control-pair rule — two runs instead of seven, for a frequency that ignores a parameter —
is only honest while `READ_BY` is true. Both are pinned here so that a change to
`mdq/quality/context.py` breaks this file rather than quietly producing a table of
confident lies.

The **artifact** has to be reproducible: same corpus, byte-identical Markdown, no clock
and no hostname, and a header naming which corpus it came from. A sensitivity table
carrying the fixture numbers under a `data/raw` heading would be worse than no table.

Everything runs on the three committed parquet fixtures. Nothing here reads `data/`.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

from mdq.domain.config import QualityConfig
from mdq.domain.frequency import Frequency
from mdq.quality.context import Regime
from support.api import PARQUET_FIXTURES, fixture_dir
from sweep_thresholds import (
    DEFAULT_OUT,
    FIXTURE_LABEL,
    GRID_AXES,
    GRID_VALUES,
    READ_BY,
    SWEEPS,
    Corpus,
    Outcome,
    default_outcome,
    load_corpus,
    main,
    plan_runs,
    render,
    run_once,
    sweep,
    sweep_grid,
)

pytestmark = pytest.mark.unit

#: An ISO-8601 instant, the shape of an accidentally embedded timestamp.
_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T")


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    """The fixture corpus, reached through the same fallback a clean clone takes."""
    return load_corpus(tmp_path / "no-such-data-dir")


# --------------------------------------------------------------------------- #
# the sweep's premises about the engine
# --------------------------------------------------------------------------- #


def test_every_swept_value_is_a_valid_config() -> None:
    """`model_copy` skips validation, so the bounds are asserted here instead.

    Were they not, `activity_percentile = 0.4` (below the 0.5 floor) would sweep happily
    and produce a row of numbers no `QualityConfig` could ever hold.
    """
    defaults = QualityConfig()
    for parameter, values in SWEEPS.items():
        assert list(values) == sorted(values), f"{parameter} values are not sorted"
        assert len(set(values)) == len(values), f"{parameter} values are not unique"
        assert getattr(defaults, parameter) in values, f"{parameter} omits its own default"
        for value in values:
            assert QualityConfig(**{parameter: value})

    for parameter, values in GRID_VALUES.items():
        assert parameter in SWEEPS
        assert set(values) <= set(SWEEPS[parameter]), f"grid {parameter} leaves the swept range"
        for value in values:
            assert QualityConfig(**{parameter: value})


def test_the_grid_puts_the_default_at_its_centre() -> None:
    """A grid read for a defect near the middle needs the shipped point in the middle."""
    defaults = QualityConfig()
    for axis in GRID_AXES:
        values = GRID_VALUES[axis]
        assert values[len(values) // 2] == getattr(defaults, axis)


def test_read_by_matches_the_context_module() -> None:
    """`READ_BY` is the reason the control-pair rule is not a guess.

    `_minute_regimes` reads `activity_percentile`, `active_bar_ratio` and
    `dormant_bar_ratio`; `_daily_regimes` reads `daily_thin_volume_ratio` and pins its own
    DORMANT cutoff to a literal 0.0. `outlier_return` is registered for both frequencies,
    `intrabar_gap` for minute alone. If any of that changes, this test tells the sweep
    before the sweep tells a reviewer something false.
    """
    assert set(READ_BY) == set(SWEEPS)
    assert READ_BY["daily_thin_volume_ratio"] == frozenset({Frequency.DAILY})
    assert READ_BY["outlier_mad_k"] == frozenset(Frequency)
    for parameter in ("activity_percentile", "active_bar_ratio", "dormant_bar_ratio"):
        assert READ_BY[parameter] == frozenset({Frequency.MINUTE})
    assert READ_BY["max_gap_minutes"] == frozenset({Frequency.MINUTE})


def test_plan_runs_uses_control_pairs_for_unread_parameters() -> None:
    daily = plan_runs(Frequency.DAILY)
    controls = [(value, flag) for name, value, flag in daily if name == "active_bar_ratio"]
    assert controls == [
        (SWEEPS["active_bar_ratio"][0], True),
        (SWEEPS["active_bar_ratio"][-1], True),
    ]

    swept = [value for name, value, flag in daily if name == "daily_thin_volume_ratio" and not flag]
    assert swept == list(SWEEPS["daily_thin_volume_ratio"])

    minute = plan_runs(Frequency.MINUTE)
    assert [v for n, v, f in minute if n == "daily_thin_volume_ratio" and f] == [
        SWEEPS["daily_thin_volume_ratio"][0],
        SWEEPS["daily_thin_volume_ratio"][-1],
    ]
    # `outlier_mad_k` is read at both frequencies, so it is never a control.
    assert not [n for n, _, f in [*daily, *minute] if n == "outlier_mad_k" and f]


# --------------------------------------------------------------------------- #
# the corpus
# --------------------------------------------------------------------------- #


def test_fixture_fallback_when_data_is_absent(corpus: Corpus) -> None:
    """A clean clone has no `data/`; the sweep must still run and say what it ran on."""
    assert corpus.label == FIXTURE_LABEL
    assert corpus.files == len(PARQUET_FIXTURES)
    assert corpus.has(Frequency.DAILY)
    assert corpus.has(Frequency.MINUTE)
    for frequency in Frequency:
        rows, contracts = corpus.describe(frequency)
        assert rows > 0
        assert contracts > 0


def test_the_fixture_corpus_takes_no_csv_fixtures(corpus: Corpus) -> None:
    """The CSVs carry invented ESH26 prices and a malformed file; both would be noise."""
    assert all(source.name.endswith(".parquet") for source in corpus.dataset.sources)


# --------------------------------------------------------------------------- #
# one run, and the default every delta is measured against
# --------------------------------------------------------------------------- #


def test_default_row_matches_an_independent_run(corpus: Corpus) -> None:
    """`run_once` bypasses the dataset's memoised report; it must still agree with it.

    The bypass is what makes a sweep possible — `Dataset.report()` caches one config per
    dataset — so this is the test that the shortcut did not change the answer.
    """
    frequency = Frequency.DAILY
    outcome = run_once(
        corpus.dataset.bars(frequency), corpus.dataset.rejects(frequency), QualityConfig()
    )
    report = corpus.dataset.report(frequency)
    counts = report.counts_by_severity()

    assert outcome.total == len(report)
    assert outcome.error == counts["ERROR"]
    assert outcome.warning == counts["WARNING"]
    assert outcome.info == counts["INFO"]
    assert outcome.needs_human == counts["ERROR"] + counts["WARNING"]
    assert sum(c.findings for c in outcome.per_check.values()) == outcome.total
    assert set(outcome.regimes) == {r.value for r in Regime}


def test_control_rows_reproduce_the_default(corpus: Corpus) -> None:
    memo: dict[str, Outcome] = {}
    default = default_outcome(corpus, Frequency.DAILY, memo)
    rows = sweep(
        corpus,
        Frequency.DAILY,
        memo=memo,
        runs=[(name, value, flag) for name, value, flag in plan_runs(Frequency.DAILY) if flag],
    )
    assert rows, "the daily plan should contain control rows"
    for row in rows:
        assert row.is_control
        assert row.outcome.per_check == default.per_check, row.parameter
        assert row.outcome.regimes == default.regimes, row.parameter


def test_the_daily_thin_ratio_cannot_move_the_invalid_ohlc_downgrade(corpus: Corpus) -> None:
    """A correction to the plan, measured: this threshold cannot reach that severity.

    `invalid_ohlc` downgrades to WARNING in a DORMANT session, and on a daily frame
    `_daily_regimes` makes a session DORMANT when `session_volume <= 0` against a
    **literal** 0.0 cutoff — `daily_thin_volume_ratio` is the THIN cutoff, which
    `regime_severity` treats exactly like ACTIVE. So the corpus's ERROR/WARNING split on
    `invalid_ohlc` rests on an observation (was anything traded?) and not on a parameter
    at all, at every value in the swept range. CLG26's six violations in the fixtures are
    all zero-volume carried-forward settlements and stay WARNING throughout.

    What the threshold *does* move is `missing_session`: it decides which sessions are
    ACTIVE, and `ActivityProfile.active_span()` bounds expected coverage by the first and
    last ACTIVE session of each contract.
    """
    memo: dict[str, Outcome] = {}
    default = default_outcome(corpus, Frequency.DAILY, memo)
    rows = sweep(
        corpus,
        Frequency.DAILY,
        memo=memo,
        runs=[
            (name, value, flag)
            for name, value, flag in plan_runs(Frequency.DAILY)
            if name == "daily_thin_volume_ratio"
        ],
    )
    assert len(rows) == len(SWEEPS["daily_thin_volume_ratio"])
    assert default.per_check["invalid_ohlc"].warning == 6
    assert default.per_check["invalid_ohlc"].error == 0
    for row in rows:
        counts = row.outcome.per_check["invalid_ohlc"]
        assert (counts.warning, counts.error) == (6, 0), row.value

    moved = {
        row.value: row.outcome.per_check["missing_session"] != default.per_check["missing_session"]
        for row in rows
        if not row.is_default
    }
    assert any(moved.values()), "the THIN cutoff should move the expected-coverage window"


def test_the_grid_reuses_the_points_the_oat_sweep_already_ran(corpus: Corpus) -> None:
    """Nine of the twenty-five cells lie on an OAT axis; a shared memo runs each once."""
    memo: dict[str, Outcome] = {}
    default_outcome(corpus, Frequency.MINUTE, memo)
    sweep(corpus, Frequency.MINUTE, memo=memo)
    before = len(memo)
    cells = sweep_grid(corpus, memo)
    assert len(cells) == 25
    assert sum(cell.is_default for cell in cells) == 1
    assert len(memo) - before == 16


# --------------------------------------------------------------------------- #
# the artifact
# --------------------------------------------------------------------------- #


def test_render_is_deterministic_and_carries_no_clock(corpus: Corpus) -> None:
    memo: dict[str, Outcome] = {}
    default = default_outcome(corpus, Frequency.DAILY, memo)
    rows = sweep(
        corpus,
        Frequency.DAILY,
        memo=memo,
        runs=[("daily_thin_volume_ratio", 0.05, False)],
    )
    arguments = (corpus, {Frequency.DAILY: rows}, [], {Frequency.DAILY: default}, len(memo))
    first = render(*arguments)
    assert first == render(*arguments)

    assert not _CLOCK.search(first)
    assert socket.gethostname() not in first
    assert f"Corpus: {FIXTURE_LABEL}" in first
    # A frequency the corpus does not cover must say so rather than render empty tables.
    assert "No bars of this frequency" in first


def test_main_runs_offline_on_fixtures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The whole catalogue, end to end, on the committed slices — ~2.5 s on this machine."""
    out = tmp_path / "sensitivity.md"
    assert main(["--data-dir", str(tmp_path / "none"), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert FIXTURE_LABEL in printed

    written = out.read_text(encoding="utf-8")
    assert f"Corpus: {FIXTURE_LABEL}" in written
    assert "scripts/sweep_thresholds.py" in written
    assert not _CLOCK.search(written)
    for parameter in SWEEPS:
        assert f"`{parameter}`" in written
    assert "### Insensitive at daily frequency" in written
    assert "### Insensitive at minute frequency" in written


def test_forcing_fixtures_ignores_a_populated_data_dir(tmp_path: Path) -> None:
    """`--corpus fixtures` must be honoured even on a machine that has the real corpus."""
    populated = fixture_dir(tmp_path, *PARQUET_FIXTURES)
    assert load_corpus(populated, force_fixtures=True).label == FIXTURE_LABEL
    assert load_corpus(populated).label != FIXTURE_LABEL


def test_the_committed_report_names_its_corpus() -> None:
    """Guards against committing a fixture run in place of the full-corpus artifact.

    The separation the report exists to describe — 44 findings of 15,018 — needs forty
    daily contracts. The fixtures have two, so a fixture run under this filename would
    misrepresent the sample rather than merely understate it, and the corpus label is the
    only thing in the file that says which happened.
    """
    report = DEFAULT_OUT.read_text(encoding="utf-8")
    assert "Generated by `scripts/sweep_thresholds.py`" in report
    assert "Corpus: data/raw" in report
    assert not _CLOCK.search(report)
    assert socket.gethostname() not in report
