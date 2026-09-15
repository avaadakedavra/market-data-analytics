"""Severity ordering, Finding round-trips and reject reasons."""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from mdq.domain.findings import Finding, RejectReason, Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import FINDING_SCHEMA

pytestmark = pytest.mark.unit


def test_severity_is_ordered_info_warning_error() -> None:
    assert Severity.INFO < Severity.WARNING < Severity.ERROR
    assert Severity.ERROR > Severity.INFO
    assert max(Severity) is Severity.ERROR
    assert sorted([Severity.ERROR, Severity.INFO, Severity.WARNING]) == [
        Severity.INFO,
        Severity.WARNING,
        Severity.ERROR,
    ]


def test_severity_filtering_reads_naturally() -> None:
    severities = [Severity.INFO, Severity.WARNING, Severity.ERROR]
    assert [s for s in severities if s >= Severity.WARNING] == [
        Severity.WARNING,
        Severity.ERROR,
    ]


def test_severity_labels_round_trip() -> None:
    assert Severity.WARNING.label == "WARNING"
    assert Severity.parse("warning") is Severity.WARNING
    assert Severity.parse(" Error ") is Severity.ERROR
    assert Severity.parse(Severity.INFO) is Severity.INFO
    with pytest.raises(ValueError, match="unknown severity 'fatal'"):
        Severity.parse("fatal")


def test_reject_reasons_are_the_documented_set() -> None:
    assert {r.value for r in RejectReason} == {
        "MISSING_REQUIRED_COLUMN",
        "UNPARSEABLE_TIMESTAMP",
        "NULL_CONTRACT",
        "NONEXISTENT_LOCAL_TIME",
        "MALFORMED_LINE",
    }
    assert str(RejectReason.MALFORMED_LINE) == "MALFORMED_LINE"


def _finding() -> Finding:
    return Finding(
        check_id="invalid_ohlc",
        severity=Severity.ERROR,
        contract="CLG26",
        frequency=Frequency.DAILY,
        message="settlement close outside a carried-forward range",
        count=3,
        start_utc=datetime(2022, 1, 13, tzinfo=UTC),
        end_utc=datetime(2022, 1, 13, tzinfo=UTC),
        session_date=date(2022, 1, 13),
        evidence={"row_ids": [7, 8, 9], "high": 1.0, "low": 2.0},
        suggested_rule_id="ohlc_bounds",
    )


def test_finding_row_matches_finding_schema() -> None:
    frame = pl.DataFrame([_finding().to_row()], schema=FINDING_SCHEMA)
    assert frame.schema == FINDING_SCHEMA
    assert frame["severity"][0] == "ERROR"
    assert frame["frequency"][0] == "daily"
    assert '"row_ids": [7, 8, 9]' in frame["evidence"][0]


def test_finding_round_trips_through_a_row() -> None:
    original = _finding()
    restored = Finding.from_row(original.to_row())
    assert restored == original


def test_finding_from_sparse_row_defaults() -> None:
    restored = Finding.from_row(
        {
            "check_id": "missing_value",
            "severity": "INFO",
            "frequency": "minute",
            "message": "null close",
            "evidence": None,
        }
    )
    assert restored.contract is None
    assert restored.count == 1
    assert restored.evidence == {}
    assert restored.suggested_rule_id is None


def test_finding_to_dict_is_json_friendly() -> None:
    data = _finding().to_dict()
    assert data["severity"] == "ERROR"
    assert data["frequency"] == "daily"
    assert data["evidence"]["row_ids"] == [7, 8, 9]
