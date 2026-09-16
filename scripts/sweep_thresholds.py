#!/usr/bin/env python
"""Threshold sensitivity — how far the triage split moves when the thresholds move.

The activity-regime model is this platform's central claim: on the full corpus it
separates 44 daily findings that need a human from 14,974 that are correct settlement
prints and exchange holidays. Every number in that sentence is conditioned on six
thresholds in `QualityConfig`, and nothing in the repository showed what became of the
separation if one of them were wrong. For a model-risk reader that is the conspicuous
hole: a headline whose parameters have never been shown to be uncritical.

So this script sweeps each threshold around its default on whichever corpus is present
and writes what it measures into `docs/evaluation/sensitivity.md`. It **reports**; it
never retunes. If a swept value looks like a better default than the one shipped, that
belongs in `docs/EVALUATION.md` as a sentence — the defaults stay where they are, because
a sweep that quietly moved them would be tuning on the only corpus we have.

**One-at-a-time around the default, not a full grid.** The six parameters partition by the
frame they act on: `activity_percentile`, `active_bar_ratio` and `dormant_bar_ratio` are
read only by `_minute_regimes`, `daily_thin_volume_ratio` only by `_daily_regimes`
(`mdq/quality/context.py`). A six-way grid at 5–7 levels would therefore be 15k–117k runs
— days of wall clock at ~5.5 s a minute pass — laying independent axes out as a hypercube.
OAT also answers the question a reviewer actually asks, which is "does the 44 move if
*this* number is wrong?", one number at a time. The one pair that genuinely interacts,
`active_bar_ratio x dormant_bar_ratio`, gets a 5x5 grid at minute frequency, because those
two are the lower and upper cutoffs of the same three-way split.

A frequency that does not read a parameter still gets a **control pair** — the two extremes
of the swept list. Two runs are enough to demonstrate a no-op empirically, where sweeping
all seven would spend minutes confirming a result the code already implies; and if a
control row ever *did* move, that would be a finding worth failing loudly over.

    uv run python scripts/sweep_thresholds.py                   # data/raw when present
    uv run python scripts/sweep_thresholds.py --corpus fixtures  # offline, committed slices
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import polars as pl

from mdq.domain.config import AppSettings, QualityConfig
from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame, C
from mdq.quality import CheckContext, run_checks
from mdq.quality.context import A, Regime
from mdq.service import MarketDataService
from mdq.service.store import Dataset, scan_file

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR: Final = REPO_ROOT / "data" / "raw"
FIXTURES_DIR: Final = REPO_ROOT / "tests" / "fixtures"
DEFAULT_OUT: Final = REPO_ROOT / "docs" / "evaluation" / "sensitivity.md"

#: Label the fixture corpus reports itself under, and the id of its `Dataset`.
FIXTURE_LABEL: Final = "tests/fixtures"

#: A finding a person has to look at. Everything else the regime model has explained.
NEEDS_HUMAN: Final[tuple[Severity, ...]] = (Severity.ERROR, Severity.WARNING)

#: The swept values, default included, sorted. Every one is inside the pydantic bounds
#: declared on `QualityConfig` — `tests/unit/test_sweep_thresholds.py` constructs each of
#: them through the validating constructor so an out-of-range value fails the suite rather
#: than silently surviving `model_copy`, which does not validate.
SWEEPS: Final[dict[str, tuple[float | int, ...]]] = {
    "activity_percentile": (0.5, 0.75, 0.9, 0.95, 1.0),
    "active_bar_ratio": (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8),
    "dormant_bar_ratio": (0.0, 0.05, 0.1, 0.15, 0.2, 0.3),
    "daily_thin_volume_ratio": (0.0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.1),
    "outlier_mad_k": (3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0),
    "max_gap_minutes": (1, 2, 3, 5, 10, 15, 30),
}

#: Which frequency's code actually consults each parameter. This is not a convenience
#: table — it is the reason the control-pair rule is honest, so it is pinned by a test
#: that fails if the engine starts reading a threshold somewhere new.
READ_BY: Final[dict[str, frozenset[Frequency]]] = {
    # `_minute_regimes` alone: the bar-count baseline and its two cutoffs.
    "activity_percentile": frozenset({Frequency.MINUTE}),
    "active_bar_ratio": frozenset({Frequency.MINUTE}),
    "dormant_bar_ratio": frozenset({Frequency.MINUTE}),
    # `_daily_regimes` alone; on a daily frame the DORMANT cutoff is a literal 0.0.
    "daily_thin_volume_ratio": frozenset({Frequency.DAILY}),
    # `outlier_return` is registered for both frequencies.
    "outlier_mad_k": frozenset(Frequency),
    # `intrabar_gap` is minute-only — a daily frame has no within-session spacing.
    "max_gap_minutes": frozenset({Frequency.MINUTE}),
}

#: Where each parameter is read, for the artifact's own subsection headings.
READ_WHERE: Final[dict[str, str]] = {
    "activity_percentile": "`_minute_regimes`, as the per-contract bar-count baseline",
    "active_bar_ratio": "`_minute_regimes`, as the ACTIVE cutoff",
    "dormant_bar_ratio": "`_minute_regimes`, as the DORMANT cutoff",
    "daily_thin_volume_ratio": "`_daily_regimes`, as the THIN cutoff",
    "outlier_mad_k": "`outlier_return`, at both frequencies",
    "max_gap_minutes": "`intrabar_gap`, minute only",
}

#: The only pair of thresholds that acts on one frame together: the two cutoffs of the
#: minute three-way split. Default at the centre of both axes.
GRID_AXES: Final[tuple[str, str]] = ("active_bar_ratio", "dormant_bar_ratio")
GRID_VALUES: Final[dict[str, tuple[float, ...]]] = {
    "active_bar_ratio": (0.3, 0.4, 0.5, 0.6, 0.7),
    "dormant_bar_ratio": (0.0, 0.05, 0.1, 0.15, 0.2),
}

#: Regime buckets, in the order the tables print them.
_REGIME_ORDER: Final[tuple[Regime, ...]] = (Regime.DORMANT, Regime.THIN, Regime.ACTIVE)

#: Severity labels, worst first — the column order of every per-check table.
_SEVERITY_ORDER: Final[tuple[str, ...]] = tuple(s.label for s in sorted(Severity, reverse=True))

_NONE = "—"


# --------------------------------------------------------------------------- #
# what was measured
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckCounts:
    """One check's contribution to a report, on all three axes that can move separately.

    `findings` counts report rows — the unit the headline 15,018 is in. `bars` sums their
    `count`, i.e. how many bars are implicated. The severity split is the third axis. All
    three are part of the identity because a threshold can move any one without the
    others, and a comparison on a single axis would call the other two stable:

    * `stale_bar` and `intrabar_gap` aggregate per session, so lowering `outlier_mad_k`
      can add hundreds of bars to findings that already exist without producing one new
      row — measured on the fixture minute slice, `outlier_mad_k` 3 through 6 all report
      ten rows;
    * a regime downgrade moves 38 `invalid_ohlc` findings from WARNING to INFO while
      leaving both counts untouched — and that *is* the answer to "what needs a human".

    Comparing only rows would let the sweep report a parameter as insensitive when it had
    changed the size of the problem by an order of magnitude.
    """

    findings: int
    bars: int
    error: int
    warning: int
    info: int


@dataclass(frozen=True)
class Outcome:
    """Everything one configuration produced on one frequency.

    Attributes:
        total: Finding rows in the report.
        error, warning, info: That total split by severity.
        needs_human: Findings at ERROR or WARNING — the number the headline is about.
        regimes: Sessions per regime. Carried because a parameter can move the regime
            classification without moving a single finding, and that contrast is itself
            the most interesting thing the sweep has to say.
        per_check: `CheckCounts` per check id, for checks that produced anything.
    """

    total: int
    error: int
    warning: int
    info: int
    needs_human: int
    regimes: dict[str, int]
    per_check: dict[str, CheckCounts]

    @property
    def share(self) -> float:
        """Needs-a-human as a share of all findings; 0.0 when there are no findings."""
        return self.needs_human / self.total if self.total else 0.0


@dataclass(frozen=True)
class SweepRow:
    """One OAT run: a parameter at a value, and what came out."""

    parameter: str
    value: float | int
    is_default: bool
    is_control: bool
    outcome: Outcome


@dataclass(frozen=True)
class GridCell:
    """One cell of the `active_bar_ratio x dormant_bar_ratio` grid."""

    active: float
    dormant: float
    is_default: bool
    outcome: Outcome


# --------------------------------------------------------------------------- #
# the corpus
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Corpus:
    """The data a sweep ran on, and the label the artifact must name it by.

    A sensitivity table is meaningless without its sample: `data/raw` has forty daily
    contracts and the 44-of-15,018 separation, the committed fixtures have two and cannot
    exhibit it. The label travels into the rendered header for exactly that reason.
    """

    label: str
    dataset: Dataset
    files: int
    _described: dict[Frequency, tuple[int, int]] = field(
        default_factory=dict, init=False, repr=False
    )

    def has(self, frequency: Frequency) -> bool:
        """True when the corpus holds bars of `frequency`."""
        return frequency in self.dataset.frequencies()

    def describe(self, frequency: Frequency) -> tuple[int, int]:
        """Rows and distinct contracts at `frequency`, collected once."""
        cached = self._described.get(frequency)
        if cached is None:
            row = (
                self.dataset.bars(frequency)
                .lf.select(
                    pl.len().alias("rows"),
                    pl.col(C.CONTRACT).n_unique().alias("contracts"),
                )
                .collect()
                .row(0)
            )
            cached = (int(row[0]), int(row[1]))
            self._described[frequency] = cached
        return cached


def load_corpus(data_dir: Path, *, force_fixtures: bool = False) -> Corpus:
    """The full corpus when `data_dir` holds bars, the committed fixtures otherwise.

    Never raises for a missing or empty `data/`: the sweep has to be runnable on a clean
    clone, and the artifact says which corpus it got. Only the parquet fixtures are taken
    — the CSV fixtures carry hand-made ESH26 minute bars at invented prices plus a
    deliberately malformed file, which `tests/support/api.py` already explains would merge
    fiction into ESH26's real series. In a sensitivity table that fiction would be
    indistinguishable from signal.
    """
    if not force_fixtures and data_dir.exists():
        service = MarketDataService(AppSettings(data_dir=data_dir))
        report = service.autoload()
        if report is not None and report.loaded:
            return Corpus(_display(data_dir), service.data, report.file_count)
    sources = tuple(
        source
        for path in sorted(FIXTURES_DIR.glob("*.parquet"))
        if (source := scan_file(path)) is not None
    )
    return Corpus(FIXTURE_LABEL, Dataset(id=FIXTURE_LABEL, sources=sources), len(sources))


def _display(path: Path) -> str:
    """A path relative to the repository when it is inside it, absolute otherwise."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- #
# running one configuration
# --------------------------------------------------------------------------- #


def run_once(bars: BarFrame, rejects: pl.DataFrame, config: QualityConfig) -> Outcome:
    """Build a context under `config` and run every applicable check over `bars`.

    Deliberately bypasses `Dataset.context()` and `Dataset.report()`: both memoise one
    result per dataset, so the second configuration of a sweep would silently get the
    first one's answer. The bars and rejects are passed in already ingested, which is what
    keeps a 5.3M-row minute run at ~5.5 s instead of re-reading forty files each time.
    """
    ctx = CheckContext.build(bars, config, rejects=rejects)
    report = run_checks(bars, ctx)
    counts = report.counts_by_severity()
    needs_human = sum(counts.get(severity.label, 0) for severity in NEEDS_HUMAN)
    return Outcome(
        total=len(report),
        error=counts.get(Severity.ERROR.label, 0),
        warning=counts.get(Severity.WARNING.label, 0),
        info=counts.get(Severity.INFO.label, 0),
        needs_human=needs_human,
        regimes=_regime_counts(ctx),
        per_check=_per_check(report.summary()),
    )


def _regime_counts(ctx: CheckContext) -> dict[str, int]:
    """Sessions per regime, every bucket present so a table column never disappears."""
    counts = dict.fromkeys((regime.value for regime in _REGIME_ORDER), 0)
    grouped = ctx.profile.regime_counts().group_by(A.REGIME).agg(pl.col("sessions").sum())
    for regime, sessions in grouped.iter_rows():
        counts[regime] = counts.get(regime, 0) + int(sessions)
    return counts


def _per_check(summary: pl.DataFrame) -> dict[str, CheckCounts]:
    """Collapse the check x severity x contract grid to one `CheckCounts` per check."""
    rows: dict[str, dict[str, int]] = {}
    bars: dict[str, int] = {}
    for row in summary.to_dicts():
        check_id = row[C.CHECK_ID]
        bucket = rows.setdefault(check_id, dict.fromkeys(_SEVERITY_ORDER, 0))
        bucket[row[C.SEVERITY]] = bucket.get(row[C.SEVERITY], 0) + int(row["findings"])
        bars[check_id] = bars.get(check_id, 0) + int(row["rows"])
    return {
        check_id: CheckCounts(
            findings=sum(bucket.values()),
            bars=bars[check_id],
            error=bucket[Severity.ERROR.label],
            warning=bucket[Severity.WARNING.label],
            info=bucket[Severity.INFO.label],
        )
        for check_id, bucket in rows.items()
    }


# --------------------------------------------------------------------------- #
# planning and running the sweep
# --------------------------------------------------------------------------- #


def plan_runs(frequency: Frequency) -> list[tuple[str, float | int, bool]]:
    """`(parameter, value, is_control)` for every OAT run at `frequency`.

    The whole list for a parameter this frequency's code reads; the two extremes, flagged
    as controls, for one it does not. A control pair is the empirical half of the claim in
    `READ_BY`: the table shows the no-op rather than asserting it.
    """
    runs: list[tuple[str, float | int, bool]] = []
    for parameter, values in SWEEPS.items():
        if frequency in READ_BY[parameter]:
            runs.extend((parameter, value, False) for value in values)
        else:
            runs.extend((parameter, value, True) for value in (values[0], values[-1]))
    return runs


def default_outcome(corpus: Corpus, frequency: Frequency, memo: dict[str, Outcome]) -> Outcome:
    """The shipped configuration's result at `frequency` — every Δ is measured against it."""
    return _memoised(corpus, frequency, QualityConfig(), memo)


def sweep(
    corpus: Corpus,
    frequency: Frequency,
    *,
    memo: dict[str, Outcome],
    runs: Sequence[tuple[str, float | int, bool]] | None = None,
) -> list[SweepRow]:
    """Every OAT run at `frequency`, in `plan_runs` order.

    `memo` is keyed by the serialised config, so the default row, and every grid cell that
    lands on an OAT point, are computed once however many tables they appear in.
    """
    defaults = QualityConfig()
    return [
        SweepRow(
            parameter=parameter,
            value=value,
            is_default=getattr(defaults, parameter) == value,
            is_control=is_control,
            outcome=_memoised(
                corpus, frequency, defaults.model_copy(update={parameter: value}), memo
            ),
        )
        for parameter, value, is_control in (plan_runs(frequency) if runs is None else runs)
    ]


def sweep_grid(corpus: Corpus, memo: dict[str, Outcome]) -> list[GridCell]:
    """The `active_bar_ratio x dormant_bar_ratio` grid, minute frequency only.

    Minute only because both axes are read by `_minute_regimes` and by nothing else; a
    daily grid over them would be twenty-five copies of the daily default.
    """
    defaults = QualityConfig()
    active_axis, dormant_axis = GRID_AXES
    return [
        GridCell(
            active=active,
            dormant=dormant,
            is_default=(
                getattr(defaults, active_axis) == active
                and getattr(defaults, dormant_axis) == dormant
            ),
            outcome=_memoised(
                corpus,
                Frequency.MINUTE,
                defaults.model_copy(update={active_axis: active, dormant_axis: dormant}),
                memo,
            ),
        )
        for active in GRID_VALUES[active_axis]
        for dormant in GRID_VALUES[dormant_axis]
    ]


def _memoised(
    corpus: Corpus,
    frequency: Frequency,
    config: QualityConfig,
    memo: dict[str, Outcome],
) -> Outcome:
    key = config.model_dump_json()
    cached = memo.get(key)
    if cached is None:
        cached = run_once(corpus.dataset.bars(frequency), corpus.dataset.rejects(frequency), config)
        memo[key] = cached
    return cached


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

_PREAMBLE = """# Threshold sensitivity

Generated by `scripts/sweep_thresholds.py` — do not edit. Regenerate with `make sensitivity`.
"""

_DEFINITIONS = """## Definitions and limitations

**Needs a human** is a finding at ERROR or WARNING. Everything at INFO is a finding the
activity-regime model explained as expected behaviour — a settlement-only print, an
exchange holiday, a dormant contract — and the whole purpose of the model is to keep those
out of a person's queue.

**Control rows** are the two extremes of a parameter's swept range, run at a frequency
whose code never reads that parameter (`READ_BY` in the generator says which, and names
the function). They exist so the table shows the no-op instead of asserting it. A control
row that moved anything would mean the engine had started reading a threshold somewhere
the generator does not know about.

**THIN and DORMANT are indistinguishable to every check.** Three checks consult the
regime — `intrabar_gap`, `stale_bar` and `outlier_return` — and all three test
`== Regime.ACTIVE`; `ActivityProfile.active_span()`, which bounds `missing_session`'s
expected coverage, is ACTIVE-only as well. A threshold that only moves sessions between
the THIN and DORMANT buckets therefore cannot move a count or a severity, however far it
travels. The sessions column above is what makes that visible: where it moves and the
findings columns do not, the regime classification really did change and no consumer
cared.

**One corpus.** Every number here was measured on the sample named in the header, and the
defaults were chosen on that same sample. A sweep cannot tell you a threshold generalises;
it can only tell you the answer is not balanced on a knife edge within the range swept.

**The sweep reports. It does not retune.** No default was changed to produce this file.
"""


def render(
    corpus: Corpus,
    oat: dict[Frequency, list[SweepRow]],
    grid: list[GridCell],
    defaults: dict[Frequency, Outcome],
    configurations: int,
) -> str:
    """The whole artifact, as Markdown. Deterministic for a given set of outcomes."""
    lines = [_PREAMBLE, _header(corpus, configurations), ""]
    for frequency in Frequency:
        lines.append(f"## {frequency.value.capitalize()}")
        lines.append("")
        default = defaults.get(frequency)
        if default is None:
            lines.append("No bars of this frequency in the corpus; nothing was swept.")
            lines.append("")
            continue
        lines.extend(_default_section(default))
        lines.extend(_parameter_sections(frequency, oat.get(frequency, []), default))
        if frequency is Frequency.MINUTE and grid:
            lines.extend(_grid_section(grid))
        lines.extend(_insensitive_section(frequency, oat.get(frequency, []), default))
    lines.append(_DEFINITIONS)
    return "\n".join(lines)


def _header(corpus: Corpus, configurations: int) -> str:
    samples = " ".join(_sample(corpus, frequency) for frequency in Frequency)
    return (
        f"Corpus: {corpus.label} ({corpus.files} files). {samples}\n"
        "Needs a human = findings at ERROR or WARNING. "
        "Share = needs a human / total findings.\n"
        f"Distinct configurations run: {configurations}, one quality pass each; "
        "a configuration shared by two tables is run once.\n"
    )


def _sample(corpus: Corpus, frequency: Frequency) -> str:
    name = frequency.value.capitalize()
    if not corpus.has(frequency):
        return f"{name}: absent."
    rows, contracts = corpus.describe(frequency)
    return f"{name}: {rows:,} rows, {contracts:,} {_plural(contracts, 'contract')}."


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


def _default_section(default: Outcome) -> list[str]:
    lines = [
        "### Default (`QualityConfig()`)",
        "",
        f"Sessions {_regimes(default)}.",
        "",
        "| check | findings | bars | ERROR | WARNING | INFO |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for check_id in sorted(default.per_check):
        counts = default.per_check[check_id]
        lines.append(
            f"| `{check_id}` | {counts.findings:,} | {counts.bars:,} | {counts.error:,} "
            f"| {counts.warning:,} | {counts.info:,} |"
        )
    bars = sum(counts.bars for counts in default.per_check.values())
    lines.extend(
        [
            f"| **total** | **{default.total:,}** | **{bars:,}** | **{default.error:,}** "
            f"| **{default.warning:,}** | **{default.info:,}** |",
            "",
            f"**Needs a human: {default.needs_human:,} of {default.total:,} "
            f"({default.share:.1%}).**",
            "",
            "`findings` counts report rows — the unit the headline is in. `bars` sums their",
            "`count`: how many bars each check implicates. A session-aggregated check moves",
            "the second without moving the first, so both are compared when deciding whether",
            "a threshold moved anything.",
            "",
        ]
    )
    return lines


def _parameter_sections(
    frequency: Frequency, rows: Sequence[SweepRow], default: Outcome
) -> list[str]:
    lines: list[str] = []
    for parameter in SWEEPS:
        selected = [row for row in rows if row.parameter == parameter]
        if not selected:
            continue
        lines.extend(_parameter_table(frequency, parameter, selected, default))
    return lines


def _parameter_table(
    frequency: Frequency, parameter: str, rows: Sequence[SweepRow], default: Outcome
) -> list[str]:
    shipped = _number(getattr(QualityConfig(), parameter))
    if frequency in READ_BY[parameter]:
        note = f"read by {READ_WHERE[parameter]}"
    else:
        note = f"**control** — no {frequency.value} code reads it; only {READ_WHERE[parameter]}"
    lines = [
        f"#### `{parameter}` — default {shipped}; {note}",
        "",
        "| value | sessions D / T / A | findings | ERROR | WARNING | INFO "
        "| needs a human | share | Δ vs default | checks that moved |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        outcome = row.outcome
        label = f"**{_number(row.value)} \\***" if row.is_default else _number(row.value)
        lines.append(
            f"| {label} | {_regimes(outcome)} | {outcome.total:,} | {outcome.error:,} "
            f"| {outcome.warning:,} | {outcome.info:,} | {outcome.needs_human:,} "
            f"| {outcome.share:.1%} | {_delta(outcome.needs_human - default.needs_human)} "
            f"| {_moved(outcome, default)} |"
        )
    lines.append("")
    return lines


def _grid_section(grid: Sequence[GridCell]) -> list[str]:
    active_axis, dormant_axis = GRID_AXES
    dormants = GRID_VALUES[dormant_axis]
    cells = {(cell.active, cell.dormant): cell for cell in grid}
    headers = " | ".join(
        f"**{_number(value)} \\***"
        if value == QualityConfig().dormant_bar_ratio
        else _number(value)
        for value in dormants
    )
    lines = [
        f"#### Grid — `{active_axis}` (rows) x `{dormant_axis}` (columns)",
        "",
        "Each cell is needs-a-human, with its share of all findings. These are the only "
        "two thresholds that",
        "act on one frame together, so this is the only interaction a grid can show.",
        "",
        f"| {active_axis} \\ {dormant_axis} | {headers} |",
        "|---:|" + "---:|" * len(dormants),
    ]
    for active in GRID_VALUES[active_axis]:
        row_label = (
            f"**{_number(active)} \\***"
            if active == QualityConfig().active_bar_ratio
            else _number(active)
        )
        rendered = []
        for dormant in dormants:
            cell = cells[(active, dormant)]
            text = f"{cell.outcome.needs_human:,} ({cell.outcome.share:.1%})"
            rendered.append(f"**{text}**" if cell.is_default else text)
        lines.append(f"| {row_label} | {' | '.join(rendered)} |")
    lines.append("")
    return lines


def _insensitive_section(
    frequency: Frequency, rows: Sequence[SweepRow], default: Outcome
) -> list[str]:
    insensitive = insensitive_parameters(rows, default)
    sensitive = [p for p in SWEEPS if any(r.parameter == p for r in rows) and p not in insensitive]
    lines = [f"### Insensitive at {frequency.value} frequency", ""]
    if insensitive:
        listed = ", ".join(f"`{p}`" for p in insensitive)
        lines.append(
            f"{listed} — every swept value reproduces the default's findings, bars *and* "
            "severity split, for every check."
        )
        lines.append("")
        lines.append(
            "A flat table here is a result, not a broken sweep. Read it against the "
            "sessions column:"
        )
        lines.append(
            "where the sessions move and the findings do not, the parameter really did "
            "reclassify the corpus"
        )
        lines.append(
            "and no check could tell, because every regime consumer tests "
            "`== Regime.ACTIVE` and nothing else."
        )
    else:
        lines.append("None: every parameter swept moved at least one check.")
    if sensitive:
        lines.append("")
        lines.append(f"Moved something: {', '.join(f'`{p}`' for p in sensitive)}.")
    lines.append("")
    return lines


def insensitive_parameters(rows: Sequence[SweepRow], default: Outcome) -> list[str]:
    """Parameters whose every non-default row reproduced the default's `per_check` exactly.

    Control rows are included in the test rather than excluded from it. Excluding them
    would make the claim vacuous for a parameter the frequency does not read — there would
    be no rows left to check — and would also hide the one outcome worth shouting about,
    a control row that moved.
    """
    swept = [parameter for parameter in SWEEPS if any(r.parameter == parameter for r in rows)]
    return [
        parameter
        for parameter in swept
        if all(
            row.outcome.per_check == default.per_check
            for row in rows
            if row.parameter == parameter and not row.is_default
        )
    ]


def _moved(outcome: Outcome, default: Outcome) -> str:
    """Sorted ids of the checks whose counts or severities differ from the default's."""
    ids = set(outcome.per_check) | set(default.per_check)
    moved = sorted(
        check_id
        for check_id in ids
        if outcome.per_check.get(check_id) != default.per_check.get(check_id)
    )
    return ", ".join(f"`{check_id}`" for check_id in moved) if moved else _NONE


def _regimes(outcome: Outcome) -> str:
    return " / ".join(f"{outcome.regimes.get(regime.value, 0):,}" for regime in _REGIME_ORDER)


def _delta(difference: int) -> str:
    return "0" if difference == 0 else f"{difference:+,}"


def _number(value: float | int) -> str:
    """`0.05` -> `0.05`, `1.0` -> `1`, `30` -> `30` — stable across runs and platforms."""
    return f"{value:g}"


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sweep and write the report. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Threshold sensitivity sweep.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--corpus",
        choices=("auto", "fixtures"),
        default="auto",
        help="'auto' uses --data-dir when it holds bars and falls back to tests/fixtures",
    )
    args = parser.parse_args(argv)

    corpus = load_corpus(args.data_dir, force_fixtures=args.corpus == "fixtures")
    oat: dict[Frequency, list[SweepRow]] = {}
    defaults: dict[Frequency, Outcome] = {}
    grid: list[GridCell] = []
    configurations = 0
    for frequency in Frequency:
        if not corpus.has(frequency):
            continue
        memo: dict[str, Outcome] = {}
        defaults[frequency] = default_outcome(corpus, frequency, memo)
        oat[frequency] = sweep(corpus, frequency, memo=memo)
        if frequency is Frequency.MINUTE:
            grid = sweep_grid(corpus, memo)
        configurations += len(memo)

    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(corpus, oat, grid, defaults, configurations), encoding="utf-8")
    print(f"corpus            {corpus.label} ({corpus.files} files)")
    print(f"configurations    {configurations}")
    print(f"written           {_display(out)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
