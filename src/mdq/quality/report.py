"""`QualityReport` — everything the engine observed about one dataset.

The report is a single `FINDING_SCHEMA` frame plus the context needed to interpret it:
which checks ran, the activity profile that decided the severities, and the thresholds
that produced that profile. Keeping the thresholds *inside* the report is deliberate —
a business user looking at an INFO where they expected an ERROR can see the number that
downgraded it without reading the source.

Three read paths, each for a different consumer:

* `summary()`      — counts by check x severity x contract, for the API and dashboard.
* `to_findings()`  — typed `Finding` objects, for the insights layer.
* `rows_affected()`— the row ids `cleanse` removes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

import polars as pl

from mdq.domain.findings import Finding, Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import FINDING_SCHEMA, C, empty_finding_df
from mdq.quality.context import ActivityProfile

__all__ = ["QualityReport"]

#: Severity ordering as a column, so `summary()` can sort worst-first in Polars.
_SEVERITY_RANK: Final[dict[str, int]] = {s.label: int(s) for s in Severity}

#: Only this field of `evidence` is read back out of the frame by the engine itself.
_ROW_IDS_DTYPE: Final = pl.Struct({"row_ids": pl.List(pl.UInt32), "row_ids_truncated": pl.Boolean})


class _SummaryColumns:
    FINDINGS: Final = "findings"
    ROWS: Final = "rows"
    SEVERITY_RANK: Final = "severity_rank"


def _conform(findings: pl.DataFrame) -> pl.DataFrame:
    """Select and cast to `FINDING_SCHEMA`, raising if a column is missing."""
    missing = [name for name in FINDING_SCHEMA.names() if name not in findings.columns]
    if missing:
        raise ValueError(f"findings frame is missing FINDING_SCHEMA columns {missing}")
    return findings.select(FINDING_SCHEMA.names()).cast(dict(FINDING_SCHEMA))  # type: ignore[arg-type]


@dataclass(frozen=True)
class QualityReport:
    """The result of running the check registry over one `BarFrame`.

    Attributes:
        findings: One row per finding, conforming to `FINDING_SCHEMA`. A finding may
            cover many rows; `count` says how many.
        frequency: The frequency the checks ran against.
        checks_run: Ids of every check that executed, including ones that failed.
        activity: The regime classification used for severity downgrades, if any. Its
            `.lf` is the `(contract, session_date, regime, thresholds)` frame PLAN §4.1
            refers to; the wrapper carries the thresholds that produced it.
        thresholds: The `QualityConfig` values in force, surfaced for the user.
    """

    findings: pl.DataFrame
    frequency: Frequency
    checks_run: tuple[str, ...] = ()
    activity: ActivityProfile | None = None
    thresholds: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", _conform(self.findings))

    # --- construction ----------------------------------------------------- #

    @classmethod
    def empty(cls, frequency: Frequency, checks_run: Sequence[str] = ()) -> QualityReport:
        """A report with no findings — what clean data produces."""
        return cls(empty_finding_df(), frequency, tuple(checks_run))

    def merge(self, other: QualityReport) -> QualityReport:
        """Concatenate two reports of the same frequency."""
        if other.frequency is not self.frequency:
            raise ValueError(
                f"cannot merge a {other.frequency} report into a {self.frequency} report"
            )
        return replace(
            self,
            findings=pl.concat([self.findings, other.findings], how="vertical"),
            checks_run=tuple(dict.fromkeys([*self.checks_run, *other.checks_run])),
        )

    # --- basic shape ------------------------------------------------------ #

    def __len__(self) -> int:
        return self.findings.height

    def is_empty(self) -> bool:
        """True when nothing was found."""
        return self.findings.height == 0

    def check_ids(self) -> list[str]:
        """Sorted distinct check ids that actually produced a finding."""
        return sorted(set(self.findings.get_column(C.CHECK_ID).to_list()))

    # --- read paths ------------------------------------------------------- #

    def summary(self) -> pl.DataFrame:
        """Counts by `check_id` x `severity` x `contract`, worst severity first.

        `findings` counts report rows; `rows` sums their `count`, i.e. how many bars
        are implicated. The two differ because row-level checks aggregate per session —
        14,152 stale bars on the real corpus arrive as 14,152 `rows`.
        """
        if self.is_empty():
            return pl.DataFrame(
                schema=pl.Schema(
                    [
                        (C.CHECK_ID, pl.String),
                        (C.SEVERITY, pl.String),
                        (C.CONTRACT, pl.String),
                        (_SummaryColumns.FINDINGS, pl.UInt32),
                        (_SummaryColumns.ROWS, pl.UInt32),
                    ]
                )
            )
        return (
            self.findings.group_by(C.CHECK_ID, C.SEVERITY, C.CONTRACT)
            .agg(
                pl.len().cast(pl.UInt32).alias(_SummaryColumns.FINDINGS),
                pl.col(C.COUNT).sum().cast(pl.UInt32).alias(_SummaryColumns.ROWS),
            )
            .with_columns(
                pl.col(C.SEVERITY)
                .replace_strict(_SEVERITY_RANK, default=0, return_dtype=pl.Int32)
                .alias(_SummaryColumns.SEVERITY_RANK)
            )
            .sort(
                [_SummaryColumns.SEVERITY_RANK, C.CHECK_ID, C.CONTRACT],
                descending=[True, False, False],
                nulls_last=True,
            )
            .drop(_SummaryColumns.SEVERITY_RANK)
        )

    def counts_by_severity(self) -> dict[str, int]:
        """`{"ERROR": 3, "WARNING": 0, "INFO": 12}` — every severity always present."""
        counts = dict.fromkeys((s.label for s in sorted(Severity, reverse=True)), 0)
        if self.is_empty():
            return counts
        observed = self.findings.group_by(C.SEVERITY).agg(pl.len().alias("n"))
        for severity, n in observed.iter_rows():
            counts[severity] = counts.get(severity, 0) + int(n)
        return counts

    def to_findings(self) -> list[Finding]:
        """Typed `Finding` objects — the form the insights layer consumes."""
        return [Finding.from_row(row) for row in self.findings.to_dicts()]

    def to_dicts(self) -> list[dict[str, Any]]:
        """Plain rows, for JSON serialisation at the API boundary."""
        return self.findings.to_dicts()

    def rows_affected(self, min_severity: Severity = Severity.ERROR) -> list[int]:
        """Sorted distinct `row_id`s implicated by findings at or above `min_severity`.

        This is the input to `cleanse`. Findings that point at no specific row (a gap, a
        missing session) contribute nothing, which is correct: there is no row to drop.
        """
        selected = self._at_least(min_severity)
        if selected.height == 0:
            return []
        ids = (
            selected.select(
                pl.col(C.EVIDENCE)
                .str.json_decode(dtype=_ROW_IDS_DTYPE)
                .struct.field("row_ids")
                .alias("row_ids")
            )
            # A finding that names no row (a gap, a missing session) must contribute
            # nothing, not a null row — hence the explicit `empty_as_null`.
            .explode("row_ids", empty_as_null=True)
            .drop_nulls()
            .get_column("row_ids")
            .unique()
            .sort()
        )
        return [int(i) for i in ids.to_list()]

    def truncated_checks(self, min_severity: Severity = Severity.ERROR) -> list[str]:
        """Checks whose evidence row-id list was truncated at or above `min_severity`.

        `cleanse` cannot remove rows it was never told about, so it reports this rather
        than pretending the cleansed frame is complete.
        """
        selected = self._at_least(min_severity)
        if selected.height == 0:
            return []
        flagged = selected.with_columns(
            pl.col(C.EVIDENCE)
            .str.json_decode(dtype=_ROW_IDS_DTYPE)
            .struct.field("row_ids_truncated")
            .fill_null(value=False)
            .alias("truncated")
        ).filter(pl.col("truncated"))
        return sorted(set(flagged.get_column(C.CHECK_ID).to_list()))

    # --- filtering -------------------------------------------------------- #

    def filter(
        self,
        *,
        check_id: str | Iterable[str] | None = None,
        contract: str | Iterable[str] | None = None,
        min_severity: Severity | None = None,
    ) -> QualityReport:
        """A narrowed report; the context (thresholds, activity) travels with it."""
        frame = self.findings
        if check_id is not None:
            frame = frame.filter(pl.col(C.CHECK_ID).is_in(_as_list(check_id)))
        if contract is not None:
            frame = frame.filter(pl.col(C.CONTRACT).is_in(_as_list(contract)))
        if min_severity is not None:
            frame = frame.filter(
                pl.col(C.SEVERITY).replace_strict(_SEVERITY_RANK, default=0, return_dtype=pl.Int32)
                >= int(min_severity)
            )
        return replace(self, findings=frame)

    def _at_least(self, min_severity: Severity) -> pl.DataFrame:
        if self.is_empty():
            return self.findings
        return self.findings.filter(
            pl.col(C.SEVERITY).replace_strict(_SEVERITY_RANK, default=0, return_dtype=pl.Int32)
            >= int(min_severity)
        )


def _as_list(value: str | Iterable[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)
