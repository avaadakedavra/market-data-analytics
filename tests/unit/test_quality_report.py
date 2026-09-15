"""`QualityReport` — the three read paths and the schema promise."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import polars as pl
import pytest

from mdq.domain.findings import Finding, Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import FINDING_SCHEMA, C
from mdq.quality.report import QualityReport

pytestmark = pytest.mark.unit


def _finding(
    check_id: str,
    severity: Severity,
    contract: str = "ESH26",
    count: int = 1,
    row_ids: list[int] | None = None,
    truncated: bool = False,
) -> Finding:
    return Finding(
        check_id=check_id,
        severity=severity,
        contract=contract,
        frequency=Frequency.MINUTE,
        message=f"{check_id} on {contract}",
        count=count,
        start_utc=datetime(2026, 3, 3, 23, 0, tzinfo=UTC),
        session_date=date(2026, 3, 4),
        evidence={"row_ids": row_ids or [], "row_ids_truncated": truncated},
        suggested_rule_id="a_rule",
    )


def _report(*findings: Finding) -> QualityReport:
    frame = pl.DataFrame([f.to_row() for f in findings], schema=FINDING_SCHEMA)
    return QualityReport(frame, Frequency.MINUTE, checks_run=tuple({f.check_id for f in findings}))


def test_construction_projects_onto_the_finding_schema() -> None:
    report = _report(_finding("stale_bar", Severity.INFO))
    assert report.findings.schema == FINDING_SCHEMA


def test_construction_rejects_a_frame_that_is_missing_columns() -> None:
    with pytest.raises(ValueError, match="missing FINDING_SCHEMA columns"):
        QualityReport(pl.DataFrame({"check_id": ["x"]}), Frequency.MINUTE)


def test_empty_report_is_the_neutral_element() -> None:
    report = QualityReport.empty(Frequency.DAILY, ["stale_bar"])
    assert report.is_empty()
    assert len(report) == 0
    assert report.check_ids() == []
    assert report.summary().height == 0
    assert list(report.summary().columns) == [
        C.CHECK_ID,
        C.SEVERITY,
        C.CONTRACT,
        "findings",
        "rows",
    ]
    assert report.counts_by_severity() == {"ERROR": 0, "WARNING": 0, "INFO": 0}
    assert report.rows_affected() == []
    assert report.truncated_checks() == []
    assert report.to_findings() == []


def test_summary_counts_findings_and_the_rows_behind_them_separately() -> None:
    report = _report(
        _finding("stale_bar", Severity.INFO, count=5_000),
        _finding("stale_bar", Severity.INFO, count=9_152),
        _finding("invalid_ohlc", Severity.ERROR, count=1),
    )
    summary = summary_rows(report)
    assert summary[("stale_bar", "INFO", "ESH26")] == (2, 14_152)
    assert summary[("invalid_ohlc", "ERROR", "ESH26")] == (1, 1)


def test_summary_sorts_the_worst_severity_first() -> None:
    report = _report(
        _finding("stale_bar", Severity.INFO),
        _finding("intrabar_gap", Severity.WARNING),
        _finding("invalid_ohlc", Severity.ERROR),
    )
    assert report.summary().get_column(C.SEVERITY).to_list() == ["ERROR", "WARNING", "INFO"]


def test_counts_by_severity_always_reports_every_severity() -> None:
    report = _report(_finding("stale_bar", Severity.INFO), _finding("stale_bar", Severity.INFO))
    assert report.counts_by_severity() == {"ERROR": 0, "WARNING": 0, "INFO": 2}


def test_to_findings_round_trips_through_the_frame() -> None:
    original = _finding("invalid_ohlc", Severity.ERROR, row_ids=[1, 2])
    restored = _report(original).to_findings()[0]
    assert restored == original


def test_rows_affected_returns_only_rows_at_or_above_the_severity() -> None:
    report = _report(
        _finding("invalid_ohlc", Severity.ERROR, row_ids=[3, 1]),
        _finding("negative_volume", Severity.ERROR, row_ids=[1, 9]),
        _finding("stale_bar", Severity.INFO, row_ids=[42]),
    )
    assert report.rows_affected() == [1, 3, 9]
    assert report.rows_affected(Severity.INFO) == [1, 3, 9, 42]


def test_rows_affected_ignores_findings_that_point_at_no_row() -> None:
    """A missing session has nothing to drop — cleanse must not be handed a phantom."""
    report = _report(_finding("missing_session", Severity.ERROR, row_ids=[]))
    assert report.rows_affected() == []


def test_truncated_checks_admits_when_evidence_is_incomplete() -> None:
    report = _report(
        _finding("missing_value", Severity.ERROR, row_ids=[1], truncated=True),
        _finding("negative_volume", Severity.ERROR, row_ids=[2]),
    )
    assert report.truncated_checks() == ["missing_value"]


def test_filter_narrows_but_keeps_the_context() -> None:
    report = _report(
        _finding("stale_bar", Severity.INFO, contract="ESH26"),
        _finding("stale_bar", Severity.WARNING, contract="ESM26"),
        _finding("invalid_ohlc", Severity.ERROR, contract="ESM26"),
    )
    assert len(report.filter(contract="ESM26")) == 2
    assert len(report.filter(check_id=["stale_bar"])) == 2
    assert len(report.filter(min_severity=Severity.WARNING)) == 2
    assert report.filter(contract="ESM26").frequency is Frequency.MINUTE
    assert report.filter(contract="nope").is_empty()


def test_merge_concatenates_and_unions_the_checks_that_ran() -> None:
    left = _report(_finding("stale_bar", Severity.INFO))
    right = _report(_finding("invalid_ohlc", Severity.ERROR))
    merged = left.merge(right)
    assert len(merged) == 2
    assert set(merged.checks_run) == {"stale_bar", "invalid_ohlc"}


def test_merging_across_frequencies_is_refused() -> None:
    left = _report(_finding("stale_bar", Severity.INFO))
    right = QualityReport.empty(Frequency.DAILY)
    with pytest.raises(ValueError, match="cannot merge"):
        left.merge(right)


def test_to_dicts_is_json_serialisable_for_the_api() -> None:
    report = _report(_finding("stale_bar", Severity.INFO, row_ids=[1]))
    row = report.to_dicts()[0]
    assert json.loads(row[C.EVIDENCE])["row_ids"] == [1]


def summary_rows(report: QualityReport) -> dict[tuple[str, str, str], tuple[int, int]]:
    return {
        (r[C.CHECK_ID], r[C.SEVERITY], r[C.CONTRACT]): (r["findings"], r["rows"])
        for r in report.summary().to_dicts()
    }
