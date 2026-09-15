"""Cleansing — turning a report into a frame the analytics can safely run on.

Reporting a defect is half the job; the exercise asks for the data to be *handled*. The
policy is deliberately conservative and entirely derived from the report, so nothing is
removed that the user was not also told about:

* **Exact duplicates** — identical values at the same `(contract, ts_utc)` — collapse to
  the first `row_id`. Nothing is lost, so this is safe without a severity.
* **Rows named by an ERROR finding** are dropped. A *conflicting* duplicate lands here:
  both copies go, because there is no principled way to choose between two contradictory
  prices, and silently keeping one would be a fabrication.
* **Everything else is kept.** A WARNING or INFO never removes a row — the whole point of
  the regime model is that most of this data is unusual but genuine.

`CleanseLog` records what happened per check, and — honestly — which checks had more
affected rows than their evidence could enumerate, so nobody mistakes the cleansed frame
for a guarantee.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.schema import BarFrame, C
from mdq.quality.report import QualityReport

__all__ = ["DEFAULT_POLICY", "CleanseLog", "CleansePolicy", "cleanse"]

#: Columns that must all agree for two bars at one instant to be "the same bar".
_VALUES = (*C.PRICES, C.VOLUME, C.OPEN_INTEREST)
_DUP_RANK = "_duplicate_rank"


@dataclass(frozen=True)
class CleansePolicy:
    """What `cleanse` is allowed to remove.

    Attributes:
        drop_exact_duplicates: Collapse identical bars at one `(contract, ts_utc)` to the
            first by `row_id`.
        drop_error_rows: Remove rows named by findings at or above `min_severity`.
        min_severity: The severity at which a finding becomes grounds for removal.
        exempt_checks: Check ids whose findings never remove a row, however severe. Lets
            a desk keep rows from a check it disagrees with without editing the check.
    """

    drop_exact_duplicates: bool = True
    drop_error_rows: bool = True
    min_severity: Severity = Severity.ERROR
    exempt_checks: frozenset[str] = frozenset()


#: The policy analytics use unless asked for the raw view.
DEFAULT_POLICY = CleansePolicy()


@dataclass(frozen=True)
class CleanseLog:
    """An audit trail of exactly what `cleanse` removed and why."""

    rows_in: int
    rows_out: int
    duplicates_dropped: int
    error_rows_dropped: int
    dropped_by_check: Mapping[str, int] = field(default_factory=dict)
    incomplete_checks: tuple[str, ...] = ()
    policy: CleansePolicy = DEFAULT_POLICY

    @property
    def rows_dropped(self) -> int:
        """Total rows removed."""
        return self.rows_in - self.rows_out

    def to_dict(self) -> dict[str, Any]:
        """Plain-python view for the API and the dashboard."""
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "rows_dropped": self.rows_dropped,
            "duplicates_dropped": self.duplicates_dropped,
            "error_rows_dropped": self.error_rows_dropped,
            "dropped_by_check": dict(self.dropped_by_check),
            "incomplete_checks": list(self.incomplete_checks),
            "policy": {
                "drop_exact_duplicates": self.policy.drop_exact_duplicates,
                "drop_error_rows": self.policy.drop_error_rows,
                "min_severity": self.policy.min_severity.label,
                "exempt_checks": sorted(self.policy.exempt_checks),
            },
        }


def cleanse(
    bars: BarFrame,
    report: QualityReport,
    policy: CleansePolicy = DEFAULT_POLICY,
) -> tuple[BarFrame, CleanseLog]:
    """Apply `policy` to `bars`, using `report` as the only source of what to remove.

    Args:
        bars: The frame the report was produced from.
        report: Findings for that frame. Only `row_id`s the report actually names are
            removed — `cleanse` never re-derives a rule of its own.
        policy: What is allowed to be removed.

    Returns:
        `(cleansed, log)`. The cleansed frame keeps `BAR_SCHEMA` and the original
        `row_id`s, so a finding can still be traced to the input file after cleansing.
    """
    if report.frequency is not bars.frequency:
        raise ValueError(
            f"report frequency {report.frequency} does not match bars frequency {bars.frequency}"
        )
    frame = bars.collect()
    rows_in = frame.height

    duplicates_dropped = 0
    if policy.drop_exact_duplicates and rows_in:
        deduplicated = _drop_exact_duplicates(frame)
        duplicates_dropped = rows_in - deduplicated.height
        frame = deduplicated

    error_rows: list[int] = []
    dropped_by_check: dict[str, int] = {}
    if policy.drop_error_rows:
        selected = report.filter(min_severity=policy.min_severity)
        if policy.exempt_checks:
            keep = [c for c in selected.check_ids() if c not in policy.exempt_checks]
            selected = selected.filter(check_id=keep) if keep else selected.filter(check_id=[])
        error_rows = selected.rows_affected(policy.min_severity)
        dropped_by_check = _dropped_by_check(selected, frame, policy)

    error_rows_dropped = 0
    if error_rows:
        before = frame.height
        frame = frame.filter(~pl.col(C.ROW_ID).is_in(error_rows))
        error_rows_dropped = before - frame.height

    log = CleanseLog(
        rows_in=rows_in,
        rows_out=frame.height,
        duplicates_dropped=duplicates_dropped,
        error_rows_dropped=error_rows_dropped,
        dropped_by_check=dropped_by_check,
        incomplete_checks=tuple(report.truncated_checks(policy.min_severity)),
        policy=policy,
    )
    return bars.with_lf(frame.lazy()), log


def _drop_exact_duplicates(frame: pl.DataFrame) -> pl.DataFrame:
    """Keep the first `row_id` of each set of byte-identical bars at one instant.

    Ranking over the *value* columns as well as the key is what makes this safe: two bars
    that disagree are not in the same group, so neither is silently discarded here. They
    are removed only if a check called them an ERROR.
    """
    key = [C.CONTRACT, C.TS_UTC, *_VALUES]
    ranked = frame.sort(C.ROW_ID).with_columns(pl.int_range(pl.len()).over(key).alias(_DUP_RANK))
    return ranked.filter(pl.col(_DUP_RANK) == 0).drop(_DUP_RANK).sort(C.ROW_ID)


def _dropped_by_check(
    report: QualityReport, frame: pl.DataFrame, policy: CleansePolicy
) -> dict[str, int]:
    """How many surviving rows each check is responsible for removing.

    Counted against the frame as it stands, so a row an exact-duplicate collapse already
    removed is not double-counted, and a row two checks both condemn is counted for both
    — which is what an auditor asking "why did this row go?" needs to see.
    """
    present = set(frame.get_column(C.ROW_ID).to_list())
    counts: dict[str, int] = {}
    for check_id in report.check_ids():
        rows = report.filter(check_id=check_id).rows_affected(policy.min_severity)
        hits = sum(1 for row_id in rows if row_id in present)
        if hits:
            counts[check_id] = hits
    return counts
