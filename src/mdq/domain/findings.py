"""Findings: what a quality check reports, and why a row was rejected at ingestion.

`Severity` is an `IntEnum` so that the natural ordering (`INFO < WARNING < ERROR`) is
the *actual* ordering — filters like `severity >= Severity.WARNING` work without any
lookup table. The string form written into `FINDING_SCHEMA` is the member name.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import IntEnum, StrEnum
from typing import Any

from mdq.domain.frequency import Frequency

__all__ = ["Finding", "RejectReason", "Severity"]


class Severity(IntEnum):
    """Finding severity, ordered least → most serious."""

    INFO = 10
    WARNING = 20
    ERROR = 30

    @property
    def label(self) -> str:
        """The canonical string form stored in `FINDING_SCHEMA.severity`."""
        return self.name

    @classmethod
    def parse(cls, value: str | Severity) -> Severity:
        """Case-insensitive lookup by name (``"error"``, ``"ERROR"``)."""
        if isinstance(value, cls):
            return value
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            raise ValueError(
                f"unknown severity {value!r}; expected one of {[s.name for s in cls]}"
            ) from None


class RejectReason(StrEnum):
    """Why ingestion could not place a row on the canonical timeline.

    Rejects are rows that cannot be *located* (no contract, no usable instant). Rows
    with bad prices or volume are never rejected — they are kept and flagged, because
    a business user needs to see them in context.
    """

    MISSING_REQUIRED_COLUMN = "MISSING_REQUIRED_COLUMN"
    UNPARSEABLE_TIMESTAMP = "UNPARSEABLE_TIMESTAMP"
    NULL_CONTRACT = "NULL_CONTRACT"
    NONEXISTENT_LOCAL_TIME = "NONEXISTENT_LOCAL_TIME"
    MALFORMED_LINE = "MALFORMED_LINE"


@dataclass(frozen=True)
class Finding:
    """One quality observation, about one contract, over one span of time.

    A finding may cover many rows (`count`); `evidence` carries a bounded sample of
    `row_ids` plus whatever values make the finding self-explanatory.
    """

    check_id: str
    severity: Severity
    contract: str | None
    frequency: Frequency
    message: str
    count: int = 1
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    session_date: date | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    suggested_rule_id: str | None = None

    def to_row(self) -> dict[str, Any]:
        """A dict matching `FINDING_SCHEMA` (evidence JSON-encoded)."""
        return {
            "check_id": self.check_id,
            "severity": self.severity.label,
            "contract": self.contract,
            "frequency": self.frequency.value,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "session_date": self.session_date,
            "count": self.count,
            "message": self.message,
            "evidence": json.dumps(self.evidence, default=str, sort_keys=True),
            "suggested_rule_id": self.suggested_rule_id,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Finding:
        """Inverse of `to_row`, for reading findings back out of a frame."""
        evidence = row.get("evidence")
        return cls(
            check_id=row["check_id"],
            severity=Severity.parse(row["severity"]),
            contract=row.get("contract"),
            frequency=Frequency.parse(row["frequency"]),
            message=row["message"],
            count=int(row.get("count") or 1),
            start_utc=row.get("start_utc"),
            end_utc=row.get("end_utc"),
            session_date=row.get("session_date"),
            evidence=json.loads(evidence) if evidence else {},
            suggested_rule_id=row.get("suggested_rule_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Plain-python view (enums as strings), for JSON serialisation."""
        data = asdict(self)
        data["severity"] = self.severity.label
        data["frequency"] = self.frequency.value
        return data
