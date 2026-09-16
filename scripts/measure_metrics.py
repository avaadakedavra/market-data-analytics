#!/usr/bin/env python
"""Precision, recall and timings — publishing what the test suite already knows.

`tests/support/faults.py` has measured both halves of detection since the engine was
written: `assert_findings_match` asserts that every expected finding appears (recall) and
that no other check fires (precision). The evidence therefore exists, and the *artifact*
does not. A reviewer asking "what is the precision of `stale_bar`?" cannot read a pytest
exit code. This script runs the same faults against the same synthetic bars and publishes
the confusion matrix, per check, as `docs/evaluation/metrics.md`.

It reads `tests/support` deliberately, and the dependency runs one way only: the harness
is the source of truth for what each fault should produce, and duplicating those
expectations here would create a second contract to keep in step with the first. What the
suite asserts and what this file reports cannot diverge, because they are the same
declarations.

**The unit of scoring is one unit of `Expected.count` per check** — "how many bars, or for
a gap how many absent minutes, the check should account for". It is the contract's own
unit, it is the one `assert_findings_match` already sums on, and for two of the fourteen
checks it is the *only* unit available: `missing_session` names no rows at all, and
`intrabar_gap`'s row ids are the two bracketing survivors rather than the missing bars.
Row-level scoring would be undefined for both.

Detection and triage are scored separately. A check that finds the right bars and labels
them WARNING where the regime model should have said INFO has not missed anything — it has
mis-triaged — so that lands in its own `severity misses` column instead of being folded
into a single number where the reader could no longer tell which half broke. `row-id
misses` is the same idea for evidence that names the wrong bars.

Timings go to a **separate** file, and only when asked. `metrics.md` is asserted
byte-equal to this generator in `make test`; a wall clock in it would break that assertion
on every run, and a reviewer could no longer read a `metrics.md` diff as "detection
changed" — which it should never be — rather than "the machine was busy".

    uv run python scripts/measure_metrics.py               # metrics.md only
    uv run python scripts/measure_metrics.py --benchmarks   # and benchmarks.md
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from datetime import date as date_type
from datetime import datetime
from pathlib import Path
from typing import Final

import polars as pl

from mdq.domain.config import QualityConfig
from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.ingest.readers import supported_extensions
from mdq.quality import CheckContext, CheckRegistry, QualityReport, run_checks
from mdq.service.store import Dataset, scan_file

try:
    import resource

    _RESOURCE = True
except ImportError:
    # Windows has no `resource`; the peak-RSS line then says "not measured" rather than
    # offering an estimate. `tracemalloc` is not a substitute — it sees Python
    # allocations only and would understate Polars by an order of magnitude.
    _RESOURCE = False

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR: Final = REPO_ROOT / "data" / "raw"
FIXTURES_DIR: Final = REPO_ROOT / "tests" / "fixtures"
DEFAULT_OUT_DIR: Final = REPO_ROOT / "docs" / "evaluation"
FIXTURE_LABEL: Final = "tests/fixtures"

# `tests/` is on `sys.path` under pytest via `pythonpath`, but not under
# `uv run python scripts/measure_metrics.py`, and the script has to work when run by
# hand. Setting `PYTHONPATH=tests` in the Makefile alone was the alternative, and it
# fails exactly there.
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from support import faults, scenarios  # noqa: E402

METRICS_NAME: Final = "metrics.md"
BENCHMARKS_NAME: Final = "benchmarks.md"

_NONE = "—"
_NOT_APPLICABLE = "n/a"


# --------------------------------------------------------------------------- #
# the catalogue
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Scenario:
    """One clean frame, zero or more faults, and why this combination is in the list.

    Attributes:
        name: Stable identifier; the row label in the artifact.
        frequency: Which frequency's checks run over it.
        frame: Builds the clean bars. Called per evaluation so no scenario can be
            polluted by a frame another one corrupted.
        faults: Applied in order by `inject_with_rejects`, which also collects the rows
            that never became bars.
        note: One line, rendered. What this scenario is *for* — several exist to pin a
            severity or a limitation rather than to add another detection.
    """

    name: str
    frequency: Frequency
    frame: Callable[[], pl.DataFrame]
    faults: tuple[faults.Fault, ...]
    note: str


#: Mirrors `tests/unit/test_quality_checks.py::MINUTE_FAULTS`/`DAILY_FAULTS` and the
#: regime tests, so the artifact publishes what the suite asserts instead of inventing a
#: second set of expectations. A test asserts this covers every type in `faults.ALL_FAULTS`.
SCENARIOS: Final[tuple[Scenario, ...]] = (
    # --- clean: the precision guard ------------------------------------------ #
    Scenario(
        "clean_minute",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (),
        "Three dense ESH26 sessions. Any finding at all here is a false positive.",
    ),
    Scenario(
        "clean_full_sessions",
        Frequency.MINUTE,
        scenarios.full_sessions,
        (),
        "Two complete 1,380-bar CME sessions across the 2026-03-08 spring forward.",
    ),
    Scenario(
        "clean_two_contracts",
        Frequency.MINUTE,
        scenarios.two_contract_frame,
        (),
        "Two contracts, so `missing_session` has siblings to corroborate against.",
    ),
    Scenario(
        "clean_daily",
        Frequency.DAILY,
        scenarios.daily_frame,
        (),
        "Three clean daily bars — the daily half of the precision guard.",
    ),
    # --- duplicates ---------------------------------------------------------- #
    Scenario(
        "exact_duplicate",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.ExactDuplicate(n=2),),
        "A re-sent file. Nothing is contradicted, so INFO is the correct severity.",
    ),
    Scenario(
        "conflicting_duplicate",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.ConflictingDuplicate(n=1),),
        "Same instant, different volume — contradictory, and therefore ERROR.",
    ),
    # --- broken values ------------------------------------------------------- #
    Scenario(
        "swap_high_low",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.SwapHighLow([10]),),
        "`high < low`: the one OHLC clause the real corpus never exhibits.",
    ),
    Scenario(
        "close_outside_range",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.CloseOutsideRange([11]),),
        "`high < max(open, close)` — 24 of the corpus's 43 genuine violations.",
    ),
    Scenario(
        "negative_price",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.NegativePrice([12]),),
        "`low` driven below zero on a non-exempt root (CL is exempt by config).",
    ),
    Scenario(
        "negative_volume",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.NegativeVolume([13]),),
        "Never a market event, always a defect.",
    ),
    Scenario(
        "null_close",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.NullField(C.CLOSE, [14]),),
        "An unparseable cell. Ingestion keeps the row so the user sees it in context.",
    ),
    Scenario(
        "null_volume",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.NullField(C.VOLUME, [15]),),
        "The same defect in a non-price column.",
    ),
    Scenario(
        "zero_volume_with_range",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.ZeroVolumeWithRange([18]),),
        "Price moved with no trades: a contradiction with no benign reading.",
    ),
    Scenario(
        "flat_zero_volume_bar",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.FlatZeroVolumeBar([19]),),
        "The shape of all 14,152 no-range bars in the corpus, inside an ACTIVE session.",
    ),
    Scenario(
        "price_spike",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.PriceSpike(60, factor=1.5),),
        "Two outliers, not one: the move into the spike and the move back out.",
    ),
    # --- broken time --------------------------------------------------------- #
    Scenario(
        "off_grid_seconds",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.OffGridSeconds([16]),),
        "Minute bars nudged off the minute boundary, as a mislabelled tick feed would.",
    ),
    Scenario(
        "future_timestamp",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.FutureTimestamp([17]),),
        "A mis-scaled epoch, landing bars forty years out.",
    ),
    Scenario(
        "ambiguous_local_time",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.AmbiguousLocalTime(bars=2, passes=2),),
        "The repeated fall-back hour, delivered twice — legitimately two checks.",
    ),
    Scenario(
        "nonexistent_local_time",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.NonexistentLocalTime(),),
        "02:30 on 2026-03-08 never happened; it reaches the engine as a reject.",
    ),
    Scenario(
        "malformed_line",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.MalformedLine(row_ids=(900_100, 900_101)),),
        "A CSV line that never parsed into fields, also via the rejects frame.",
    ),
    Scenario(
        "wall_clock_read_as_utc",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.ShiftWallClockToUtc(),),
        "ADR-1's trap. Expects nothing: no row-level check can see it (see Definitions).",
    ),
    # --- absence ------------------------------------------------------------- #
    Scenario(
        "intrabar_gap",
        Frequency.MINUTE,
        scenarios.minute_frame,
        (faults.DropRange(datetime(2026, 3, 3, 17, 30), datetime(2026, 3, 3, 18, 9)),),
        "A 39-minute hole inside a session that is still ACTIVE afterwards.",
    ),
    Scenario(
        "missing_session_one_contract",
        Frequency.MINUTE,
        scenarios.two_contract_frame,
        (faults.DropSession(date_type(2026, 3, 4), contract="ESH26", severity=Severity.WARNING),),
        "One contract absent while its sibling traded: a hole in the feed, WARNING.",
    ),
    Scenario(
        "missing_session_all_contracts",
        Frequency.MINUTE,
        scenarios.two_contract_frame,
        (faults.DropSession(date_type(2026, 3, 4), severity=Severity.INFO),),
        "Every contract absent: a holiday, INFO. Same absence, different reading.",
    ),
    # --- daily --------------------------------------------------------------- #
    Scenario(
        "swap_high_low_daily",
        Frequency.DAILY,
        scenarios.daily_frame,
        (faults.SwapHighLow([0]),),
        "The same OHLC clause at daily frequency.",
    ),
    Scenario(
        "close_outside_range_daily",
        Frequency.DAILY,
        scenarios.daily_frame,
        (faults.CloseOutsideRange([2]),),
        "A settlement outside its own bar's range, in a traded session: ERROR.",
    ),
    Scenario(
        "carried_forward_settlement",
        Frequency.DAILY,
        scenarios.daily_frame,
        (faults.CarriedForwardSettlement([1]),),
        "The corpus's 39-of-43 signature. Zero volume, so DORMANT, so WARNING.",
    ),
    Scenario(
        "negative_volume_daily",
        Frequency.DAILY,
        scenarios.daily_frame,
        (faults.NegativeVolume([1]),),
        "Negative volume on a daily bar.",
    ),
    Scenario(
        "null_open_daily",
        Frequency.DAILY,
        scenarios.daily_frame,
        (faults.NullField(C.OPEN, [1]),),
        "A null price field on a daily bar.",
    ),
)


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #


@dataclass
class Tally:
    """One check's confusion matrix, in units of `Expected.count`.

    `severity_misses` and `row_id_misses` are deliberately *not* folded into the first
    three. A check that found exactly the right bars and labelled one of them wrongly has
    a detection record of TP with no FP and no FN, and a triage record with one miss;
    collapsing the two would leave a reader unable to say which had gone wrong.
    """

    tp: int = 0
    fp: int = 0
    fn: int = 0
    severity_misses: int = 0
    row_id_misses: int = 0

    def __add__(self, other: Tally) -> Tally:
        return Tally(
            tp=self.tp + other.tp,
            fp=self.fp + other.fp,
            fn=self.fn + other.fn,
            severity_misses=self.severity_misses + other.severity_misses,
            row_id_misses=self.row_id_misses + other.row_id_misses,
        )

    @property
    def is_perfect(self) -> bool:
        """True when the check was right on every axis and did detect something."""
        return self.tp > 0 and not (
            self.fp or self.fn or self.severity_misses or self.row_id_misses
        )

    @property
    def precision(self) -> float | None:
        """Of what the check reported, how much was real. `None` when it reported nothing."""
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        """Of what was broken, how much the check found. `None` when nothing was expected."""
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        """Harmonic mean, `None` when either half is undefined or both are zero."""
        precision, recall = self.precision, self.recall
        if precision is None or recall is None or precision + recall == 0:
            return None
        return 2 * precision * recall / (precision + recall)


def score(report: QualityReport, expected: Sequence[faults.Expected]) -> dict[str, Tally]:
    """The per-check confusion matrix for one scenario.

    Every check considered independently against its own `Expected`, which is how a fault
    with two legitimate consequences is handled without a special case: an ambiguous local
    time delivered twice really does trip `ambiguous_local_time` *and*
    `duplicate_timestamp`, `Expected` already declares both, and each is scored against
    its own want. A check with findings and no `Expected` entry is a false positive for
    its whole output — the "unexpected" branch of `assert_findings_match`, not a new rule.
    """
    considered = set(report.checks_run) | set(report.check_ids()) | {e.check_id for e in expected}
    tallies: dict[str, Tally] = {}
    for check_id in sorted(considered):
        wants = [e for e in expected if e.check_id == check_id]
        subset = report.filter(check_id=check_id)
        want = sum(e.count for e in wants)
        got = int(subset.findings.get_column(C.COUNT).sum() or 0)
        tallies[check_id] = Tally(
            tp=min(want, got),
            fp=max(0, got - want),
            fn=max(0, want - got),
            severity_misses=_severity_misses(subset, wants),
            row_id_misses=_row_id_misses(subset, wants),
        )
    return tallies


def _severity_misses(subset: QualityReport, wants: Sequence[faults.Expected]) -> int:
    """Expected severities the check did not actually produce.

    Mirrors the set comparison in `assert_findings_match`. A non-zero count here means the
    regime model mis-triaged a real detection, never that the check missed one.
    """
    produced = set(subset.findings.get_column(C.SEVERITY).to_list())
    return sum(1 for e in wants if e.severity is not None and e.severity.label not in produced)


def _row_id_misses(subset: QualityReport, wants: Sequence[faults.Expected]) -> int:
    """Expected `row_id`s absent from the check's evidence.

    Count-level agreement could in principle hide a check that named the wrong bars. This
    guards that without inventing a row-level unit for the two checks that have none.
    """
    found = set(subset.rows_affected(Severity.INFO))
    wanted = {row_id for e in wants for row_id in e.row_ids}
    return len(wanted - found)


# --------------------------------------------------------------------------- #
# running the catalogue
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScenarioResult:
    """One scenario's report, its declared expectations, and how it scored."""

    scenario: Scenario
    expected: tuple[faults.Expected, ...]
    tallies: dict[str, Tally]
    findings: int

    @property
    def is_exact(self) -> bool:
        """True when no check over- or under-reported, mis-triaged or named a wrong row."""
        return all(
            not (t.fp or t.fn or t.severity_misses or t.row_id_misses)
            for t in self.tallies.values()
        )


@dataclass(frozen=True)
class Evaluation:
    """The whole catalogue, scored.

    Attributes:
        results: One per scenario, in catalogue order.
        per_check: Tallies summed across every scenario in which the check was scored.
        scenarios_per_check: How many scenarios that was.
        clean: `check_id -> scenario name -> findings`, `None` where the check does not
            run at that scenario's frequency. Every cell must be 0.
        clean_sizes: Bars in each clean frame, so the guard names its sample.
    """

    results: tuple[ScenarioResult, ...]
    per_check: dict[str, Tally]
    scenarios_per_check: dict[str, int]
    clean: dict[str, dict[str, int | None]]
    clean_sizes: dict[str, int]


def run_scenario(scenario: Scenario) -> tuple[QualityReport, list[faults.Expected]]:
    """Corrupt a fresh clean frame and run every applicable check over the result."""
    frame, rejects, expected = faults.inject_with_rejects(scenario.frame(), *scenario.faults)
    _, _, report = scenarios.checked(frame, scenario.frequency, rejects=rejects)
    return report, expected


def evaluate(catalogue: Sequence[Scenario] = SCENARIOS) -> Evaluation:
    """Score every scenario and aggregate per check."""
    results: list[ScenarioResult] = []
    per_check: dict[str, Tally] = {}
    counts: dict[str, int] = {}
    clean: dict[str, dict[str, int | None]] = {}
    clean_sizes: dict[str, int] = {}
    clean_names = [s.name for s in catalogue if not s.faults]

    for check_id in CheckRegistry.ids():
        clean[check_id] = dict.fromkeys(clean_names)

    for scenario in catalogue:
        report, expected = run_scenario(scenario)
        tallies = score(report, expected)
        results.append(ScenarioResult(scenario, tuple(expected), tallies, findings=len(report)))
        for check_id, tally in tallies.items():
            per_check[check_id] = per_check.get(check_id, Tally()) + tally
            counts[check_id] = counts.get(check_id, 0) + 1
        if not scenario.faults:
            clean_sizes[scenario.name] = _frame_height(scenario)
            for check_id in report.checks_run:
                if check_id in clean:
                    clean[check_id][scenario.name] = len(report.filter(check_id=check_id))
    return Evaluation(tuple(results), per_check, counts, clean, clean_sizes)


def _frame_height(scenario: Scenario) -> int:
    """Bars in a scenario's clean frame — the sample size the guard is measured on."""
    return scenario.frame().height


# --------------------------------------------------------------------------- #
# rendering the metrics
# --------------------------------------------------------------------------- #

_METRICS_DEFINITIONS = """## Definitions

**The unit is one unit of `Expected.count` per check** — one bar, or for an absence
finding one missing minute or session. It is the unit `Expected` itself is written in and
the one `assert_findings_match` sums on, and it is the only unit two of the fourteen checks
have: `missing_session` names no rows, and `intrabar_gap`'s row ids are the two bracketing
survivors rather than the bars that vanished.

Per scenario and per check, with `want` the summed `Expected.count` and `got` the summed
`count` the check reported: **TP** = `min(want, got)`, **FP** = `max(0, got - want)`,
**FN** = `max(0, want - got)`. A check with findings and no expectation in that scenario is
a false positive for its whole output. A check reporting three bars where two were broken
scores TP 2 and FP 1 — it found the defect and over-reached, and the matrix says both.

**`severity misses`** counts expectations whose severity the check did not produce, and
**`row-id misses`** expected row ids missing from its evidence. Neither changes TP, FP or
FN: detection and triage are different failures, and a single number would hide which one
broke. A non-zero severity miss means the *regime model* mis-triaged a real detection.

**A fault with two legitimate consequences is scored twice, not penalised once.** An
ambiguous wall clock delivered twice arrives under `ambiguous="earliest"` with both copies
on the same instant, so it genuinely trips `ambiguous_local_time` and
`duplicate_timestamp`; `Expected` declares both and each is scored against its own want.

**`wall_clock_read_as_utc` expects nothing, and is here for that reason.** Reading a
Chicago wall clock as UTC leaves the frame internally consistent — coherent bars, unique
instants, dense sessions — so no row-level check can see it, and recall is not defined for
it. Only reconciliation between frequencies could catch it, and
`cross_frequency_mismatch` is a documented non-goal in `docs/ARCHITECTURE.md`. The
scenario is included so that limitation is visible in the numbers rather than absent
from them.

**How to read a precision below 1.000.** It means the check reported `count` no `Expected`
in that scenario declared. There are exactly two causes and they point in opposite
directions: the check over-reached, or the *fault* has a genuine consequence its
declaration omits — in which case the check was correct and the harness is understating
what the fault does. "Where the matrix is not 1.000" above names every instance;
`docs/EVALUATION.md` says which cause applies to each.

**Limitation: these faults are meant to be surgical.** Each is written to violate one
invariant and leave the others intact, which is what makes the precision half of the
assertion mean anything. Where the measurement shows one is not, that is itself reported
above rather than corrected out of the catalogue. Either way these figures say nothing
about compound defects — a feed outage that drops bars, stales the ones it keeps and
duplicates the recovery — and nothing about defect rates in real data. They measure
whether each check does its own job cleanly on a defect aimed at it.
"""


def render_metrics(evaluation: Evaluation) -> str:
    """The whole metrics artifact, as Markdown. No clock, no host, no corpus dependency."""
    faulted = [r for r in evaluation.results if r.scenario.faults]
    clean = [r for r in evaluation.results if not r.scenario.faults]
    lines = [
        "# Check precision and recall",
        "",
        "Generated by `scripts/measure_metrics.py` — do not edit. Regenerate with `make metrics`.",
        "",
        "Data: synthetic clean bars from `tests/support/synth.py`, broken by the faults in",
        "`tests/support/faults.py`. Corpus-independent — nothing under `data/` or",
        "`tests/fixtures/` is read, so these numbers do not move when the sample does.",
        "",
        f"Scenarios: {len(evaluation.results)} "
        f"({len(clean)} clean, {len(faulted)} faulted), covering "
        f"{len(faults.ALL_FAULTS)} fault types and {len(CheckRegistry.ids())} checks.",
        "",
    ]
    lines.extend(_per_check_table(evaluation))
    lines.extend(_imperfect_section(evaluation))
    lines.extend(_clean_table(evaluation))
    lines.extend(_scenario_table(evaluation))
    lines.append(_METRICS_DEFINITIONS)
    return "\n".join(lines)


def _imperfect_section(evaluation: Evaluation) -> list[str]:
    """Locate every departure from 1.000, so a reader never has to hunt for one.

    Derived rather than written: the table above can go from perfect to imperfect on a
    Polars upgrade or a new check, and a hand-written note about which row is which would
    then be stale in exactly the situation where it mattered most.
    """
    lines = ["## Where the matrix is not 1.000", ""]
    rows = [
        (check_id, result.scenario.name, detail)
        for check_id, tally in sorted(evaluation.per_check.items())
        if tally.fp or tally.fn or tally.severity_misses or tally.row_id_misses
        for result in evaluation.results
        if (detail := _tally_detail(result.tallies.get(check_id)))
    ]
    if not rows:
        lines.extend(
            [
                "Nothing: every check matched its declaration on all four axes, in every",
                "scenario it ran in.",
                "",
            ]
        )
        return lines
    lines.extend(
        [
            "| check | scenario | what it did beyond its declaration |",
            "|---|---|---|",
        ]
    )
    lines.extend(f"| `{check}` | `{scenario}` | {detail} |" for check, scenario, detail in rows)
    lines.extend(
        [
            "",
            "A row here is one of two things, and the difference is the whole question: the",
            "check over- or under-reported, **or** the fault has a real consequence its",
            "`Expected` does not declare, in which case the check was right and the",
            "declaration is short. The generator cannot tell them apart — that takes reading",
            "the fault — so each one is discussed in `docs/EVALUATION.md` rather than",
            "adjusted away here. No scenario was dropped and no expectation was edited to",
            "make this table empty.",
            "",
        ]
    )
    return lines


def _tally_detail(tally: Tally | None) -> str:
    """`FP 4`, `FN 1, severity 2` — the non-zero axes of one tally, or `""` if clean."""
    if tally is None:
        return ""
    return ", ".join(
        f"{label} {value}"
        for label, value in (
            ("FP", tally.fp),
            ("FN", tally.fn),
            ("severity misses", tally.severity_misses),
            ("row-id misses", tally.row_id_misses),
        )
        if value
    )


def _per_check_table(evaluation: Evaluation) -> list[str]:
    lines = [
        "## Per check",
        "",
        "Summed over every scenario in which the check ran, clean scenarios included.",
        "",
        "| check | scenarios | TP | FP | FN | precision | recall | F1 "
        "| severity misses | row-id misses |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for check_id in _reported_checks(evaluation):
        tally = evaluation.per_check.get(check_id, Tally())
        lines.append(
            f"| `{check_id}` | {evaluation.scenarios_per_check.get(check_id, 0)} "
            f"| {tally.tp:,} | {tally.fp:,} | {tally.fn:,} "
            f"| {_ratio(tally.precision)} | {_ratio(tally.recall)} | {_ratio(tally.f1)} "
            f"| {tally.severity_misses} | {tally.row_id_misses} |"
        )
    lines.append("")
    return lines


def _reported_checks(evaluation: Evaluation) -> list[str]:
    """Every registered check, plus `check_failed` only if it actually fired.

    Printing the crash row unconditionally would put a permanent zero in the artifact;
    omitting it when it fires would launder a crashed check into a clean table.
    """
    reported = list(CheckRegistry.ids())
    crashed = evaluation.per_check.get("check_failed")
    if crashed is not None:
        reported.append("check_failed")
    return reported


def _clean_table(evaluation: Evaluation) -> list[str]:
    names = list(evaluation.clean_sizes)
    headers = " | ".join(f"{name} ({evaluation.clean_sizes[name]:,} bars)" for name in names)
    lines = [
        "## Clean-data guard",
        "",
        "Findings produced on perfectly clean synthetic bars. Every cell must be 0: a check",
        "that fires here has precision 0 however good its recall, and will bury a real user",
        f"long before it helps them. `{_NONE}` means the check does not run at that frequency.",
        "",
        f"| check | {headers} |",
        "|---|" + "---:|" * len(names),
    ]
    for check_id in CheckRegistry.ids():
        cells = " | ".join(
            _NONE if (value := evaluation.clean[check_id][name]) is None else f"{value:,}"
            for name in names
        )
        lines.append(f"| `{check_id}` | {cells} |")
    lines.append("")
    return lines


def _scenario_table(evaluation: Evaluation) -> list[str]:
    lines = [
        "## Scenarios",
        "",
        "| scenario | frequency | faults | expected | result | why it is here |",
        "|---|---|---|---|---|---|",
    ]
    for result in evaluation.results:
        scenario = result.scenario
        lines.append(
            f"| `{scenario.name}` | {scenario.frequency.value} "
            f"| {_fault_list(scenario.faults)} | {_expected(result.expected)} "
            f"| {_result(result)} | {scenario.note} |"
        )
    lines.append("")
    return lines


def _fault_list(applied: Sequence[faults.Fault]) -> str:
    return ", ".join(f"`{_describe_fault(f)}`" for f in applied) if applied else _NONE


def _describe_fault(fault: faults.Fault) -> str:
    """`CarriedForwardSettlement(rows=[1])` — only the fields set away from their default.

    The full `repr` carries every tuning knob a fault happens to expose and would make the
    table unreadable; what a reader needs is which fault, aimed at what.
    """
    parts = [
        f"{f.name}={_literal(getattr(fault, f.name))}"
        for f in fields(fault)
        if getattr(fault, f.name) != f.default
    ]
    return f"{type(fault).__name__}({', '.join(parts)})"


def _literal(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date_type):
        return value.isoformat()
    if isinstance(value, Severity):
        return value.label
    if isinstance(value, Sequence) and not isinstance(value, str):
        return f"[{', '.join(_literal(v) for v in value)}]"
    return repr(value)


def _expected(expected: Sequence[faults.Expected]) -> str:
    if not expected:
        return "nothing"
    return ", ".join(
        f"`{e.check_id}` {e.count}"
        + (f" @ {e.severity.label}" if e.severity is not None else " @ any")
        for e in sorted(expected, key=lambda e: e.check_id)
    )


def _result(result: ScenarioResult) -> str:
    """ "exact" when every check matched its declaration on all four axes, else the gap.

    Naming the discrepancy rather than scoring it out of sight is the point: a scenario
    that does not come out exact is a real statement about this engine.
    """
    if result.is_exact:
        return "exact"
    return "; ".join(
        f"`{check_id}` {detail}"
        for check_id, tally in sorted(result.tallies.items())
        if (detail := _tally_detail(tally))
    )


def _ratio(value: float | None) -> str:
    return _NOT_APPLICABLE if value is None else f"{value:.3f}"


# --------------------------------------------------------------------------- #
# benchmarks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Timing:
    """One phase, timed `len(seconds)` times, with the sample it ran on named.

    `sample` is not decoration. An unqualified timing is worse than no timing: "5.6 s"
    means nothing without "5,295,239 rows across 40 contracts" beside it.
    """

    name: str
    sample: str
    seconds: tuple[float, ...]

    @property
    def fastest(self) -> float:
        """The minimum — the least contaminated by whatever else the machine was doing."""
        return min(self.seconds)

    @property
    def median(self) -> float:
        """The middle, which is what a user would typically wait."""
        return statistics.median(self.seconds)


@dataclass(frozen=True)
class Benchmarks:
    """Timings, the environment that qualifies them, and the process peak RSS."""

    corpus_label: str
    files: int
    bytes: int
    repeats: int
    timings: tuple[Timing, ...]
    peak_rss_bytes: int | None
    system: str
    machine: str
    cpus: int | None
    python: str
    polars: str


def run_benchmarks(
    data_dir: Path,
    *,
    repeats: int = 3,
    force_fixtures: bool = False,
) -> Benchmarks:
    """Time classification, ingestion and both quality passes, `repeats` times each.

    A **fresh `Dataset` per repeat** is not fussiness: `Source.result()` memoises its
    ingestion, so the second repeat of "ingest minute" would otherwise measure a dict
    lookup. The quality passes then reuse that repeat's already-ingested bars, so they
    time the checks and not the read.

    Phases run in a fixed order and each is timed separately, because the interesting
    question is which one a user waits for. Peak RSS is taken once, at the end, and is
    labelled in the artifact as what it is — a whole-process ceiling attributable to no
    single phase. Per-phase memory would need a subprocess per phase to measure honestly.
    """
    label, candidates = _corpus_files(data_dir, force_fixtures=force_fixtures)
    total_bytes = sum(path.stat().st_size for path in candidates)
    samples: dict[str, str] = {}
    seconds: dict[str, list[float]] = {name: [] for name in _PHASES}

    for _ in range(repeats):
        with _timed(seconds, "classify"):
            sources = tuple(s for path in candidates if (s := _scan(path)) is not None)
        samples["classify"] = (
            f"{len(candidates):,} files, {_mib(total_bytes)} "
            f"({len(sources):,} of them bar data; columns only, no rows)"
        )
        dataset = Dataset(id=label, sources=sources)
        for frequency in Frequency:
            phase = f"ingest-{frequency.value}"
            with _timed(seconds, phase):
                bars = dataset.bars(frequency)
                rows = int(bars.lf.select(pl.len()).collect().item())
            samples[phase] = (
                f"{len(dataset.sources_for(frequency)):,} files, {rows:,} rows "
                "(row count collected from the lazy frame)"
            )
            phase = f"quality-{frequency.value}"
            rejects = dataset.rejects(frequency)
            with _timed(seconds, phase):
                report = run_checks(
                    bars, CheckContext.build(bars, QualityConfig(), rejects=rejects)
                )
            samples[phase] = (
                f"{rows:,} rows, {len(report):,} findings, "
                f"{len(dataset.summary(frequency).contracts):,} contracts"
            )

    return Benchmarks(
        corpus_label=label,
        files=len(candidates),
        bytes=total_bytes,
        repeats=repeats,
        timings=tuple(
            Timing(_PHASES[name], samples[name], tuple(seconds[name]))
            for name in _PHASES
            if seconds[name]
        ),
        peak_rss_bytes=peak_rss_bytes(),
        system=platform.system(),
        machine=platform.machine(),
        cpus=os.cpu_count(),
        python=platform.python_version(),
        polars=pl.__version__,
    )


#: Phase key -> the label the artifact prints, in run order.
_PHASES: Final[dict[str, str]] = {
    "classify": "file classification",
    "ingest-daily": "ingest — daily",
    "quality-daily": "quality pass — daily",
    "ingest-minute": "ingest — minute",
    "quality-minute": "quality pass — minute",
}


class _timed:
    """Context manager appending the elapsed wall clock to `into[key]`."""

    def __init__(self, into: dict[str, list[float]], key: str) -> None:
        self._into = into
        self._key = key
        self._started = 0.0

    def __enter__(self) -> _timed:
        self._started = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        self._into[self._key].append(time.perf_counter() - self._started)


def _scan(path: Path) -> object | None:
    """`scan_file`, tolerating a file that is not readable as bars at all.

    A vendor drop carries checksums and catalogues beside its bars, and
    `MarketDataService.load_directory` already skips rather than aborts on them. Timing
    the classification of a real directory means timing that too.
    """
    try:
        return scan_file(path)
    except Exception:
        return None


def _corpus_files(data_dir: Path, *, force_fixtures: bool) -> tuple[str, list[Path]]:
    """The label and the candidate files a benchmark should run over."""
    if not force_fixtures and data_dir.exists():
        candidates = _candidates(data_dir)
        if candidates:
            return _display(data_dir), candidates
    return FIXTURE_LABEL, sorted(FIXTURES_DIR.glob("*.parquet"))


def _candidates(directory: Path) -> list[Path]:
    """Files under `directory` whose extension some reader claims, in a stable order."""
    extensions = supported_extensions()
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions
    )


def peak_rss_bytes() -> int | None:
    """Process peak resident set size, or `None` when the platform cannot say.

    `ru_maxrss` is bytes on darwin and kibibytes on Linux; the unit is taken from the
    platform rather than guessed from the magnitude.
    """
    if not _RESOURCE:
        return None
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024


_BENCHMARK_NOTES = """Not measured, deliberately:

* **Per-phase memory.** Measuring it honestly needs one subprocess per phase, since
  `ru_maxrss` only ever rises. `tracemalloc` sees Python allocations and would understate
  Polars by an order of magnitude, which is worse than reporting nothing.
* **The dashboard and the HTTP API.** A separate workstream owns the dashboard, and an
  HTTP timing measures the framework more than it measures this code.
* **Analytics and insights.** Daily-bar and VWAP generation are outside the two gaps this
  work exists to close; the insight rules run in microseconds and the number would carry
  no information.

See `docs/EVALUATION.md` for how these compare with the figures quoted in prose.
"""


def render_benchmarks(benchmarks: Benchmarks) -> str:
    """The timings artifact. Regenerated deliberately; its diffs are expected."""
    lines = [
        "# Benchmarks",
        "",
        "Generated by `scripts/measure_metrics.py --benchmarks` — do not edit. Timings vary",
        "run to run, so this file is regenerated deliberately and a diff here is expected.",
        "It carries no timestamp and no hostname: what qualifies a timing is the machine's",
        "shape and the sample, both below.",
        "",
        f"Environment: {benchmarks.system} {benchmarks.machine}, "
        f"{benchmarks.cpus} logical CPUs, Python {benchmarks.python}, "
        f"polars {benchmarks.polars}.",
        f"Corpus: {benchmarks.corpus_label} — {benchmarks.files:,} candidate files, "
        f"{_mib(benchmarks.bytes)}. Repeats: {benchmarks.repeats}.",
        "",
        "| phase | sample | repeats | min (s) | median (s) |",
        "|---|---|---:|---:|---:|",
    ]
    for timing in benchmarks.timings:
        lines.append(
            f"| {timing.name} | {timing.sample} | {len(timing.seconds)} "
            f"| {timing.fastest:.2f} | {timing.median:.2f} |"
        )
    lines.extend(["", _peak_line(benchmarks), "", _BENCHMARK_NOTES])
    return "\n".join(lines)


def _peak_line(benchmarks: Benchmarks) -> str:
    if benchmarks.peak_rss_bytes is None:
        return (
            "Peak RSS: **not measured** — this platform exposes no `resource` module, and "
            "an estimate would be worse than a gap."
        )
    return (
        f"Peak RSS of the whole benchmark process after all repeats: "
        f"**{_gib(benchmarks.peak_rss_bytes)}** (`ru_maxrss`). It is an upper bound on "
        "every phase above and attributable to none of them — the process held both "
        "frequencies' bars and both reports at once, which no single request does."
    )


def _mib(size: int) -> str:
    return f"{size / 1024 / 1024:,.1f} MiB"


def _gib(size: int) -> str:
    return f"{size / 1024 / 1024 / 1024:,.2f} GiB"


def _display(path: Path) -> str:
    """A path relative to the repository when it is inside it, absolute otherwise."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    """Write `metrics.md`, and `benchmarks.md` when asked. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Per-check precision, recall and timings.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--corpus", choices=("auto", "fixtures"), default="auto")
    parser.add_argument(
        "--benchmarks",
        action="store_true",
        help="also time ingestion and the quality passes, into benchmarks.md",
    )
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    evaluation = evaluate()
    metrics = out_dir / METRICS_NAME
    metrics.write_text(render_metrics(evaluation), encoding="utf-8")
    print(f"scenarios         {len(evaluation.results)}")
    print(f"perfect checks    {sum(t.is_perfect for t in evaluation.per_check.values())}")
    print(f"written           {_display(metrics)}")

    if args.benchmarks:
        benchmarks = run_benchmarks(
            args.data_dir,
            repeats=args.repeats,
            force_fixtures=args.corpus == "fixtures",
        )
        path = out_dir / BENCHMARKS_NAME
        path.write_text(render_benchmarks(benchmarks), encoding="utf-8")
        print(f"corpus            {benchmarks.corpus_label} ({benchmarks.files} files)")
        print(f"written           {_display(path)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
