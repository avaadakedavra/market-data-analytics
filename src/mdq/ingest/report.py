"""What ingestion reports about itself: rejects and statistics.

`IngestStats` is the answer to "what happened to my file?" — the question a business
user actually asks after an upload. It is deliberately small and fully serialisable,
because it is rendered verbatim by the API and the dashboard.

`REJECT_SCHEMA` keeps rejects in a *frame* rather than a list of objects for the same
reason findings are frames: they join, group and filter alongside everything else, and
the quality engine lifts them straight into `malformed_record` findings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import polars as pl

from mdq.domain.frequency import Frequency
from mdq.domain.schema import C

__all__ = [
    "RAW_VALUES",
    "REJECT_SCHEMA",
    "SOURCE",
    "IngestStats",
    "empty_reject_df",
]

#: JSON-encoded snapshot of the offending row, exactly as it appeared in the source.
RAW_VALUES: Final = "raw_values"
#: Provenance: which file (or upload) the reject came from.
SOURCE: Final = "source"

#: One row per input row that could not be placed on the canonical timeline.
#:
#: `contract` is null exactly when the reject is the reason we do not know it — a
#: malformed line, a blank contract, a whole file missing a column — and populated
#: otherwise, so the quality report can attribute a reject to an instrument.
REJECT_SCHEMA: Final[pl.Schema] = pl.Schema(
    [
        (C.ROW_ID, pl.UInt32),
        (C.REASON, pl.String),
        (C.CONTRACT, pl.String),
        (RAW_VALUES, pl.String),
        (SOURCE, pl.String),
    ]
)


def empty_reject_df() -> pl.DataFrame:
    """An empty data frame with exactly `REJECT_SCHEMA`."""
    return pl.DataFrame(schema=REJECT_SCHEMA)


@dataclass(frozen=True)
class IngestStats:
    """A factual account of one ingest. No judgements, just counts.

    Attributes:
        source: File path or upload name.
        profile: Name of the `SourceProfile` that was used.
        frequency: Frequency of the resulting `BarFrame`.
        rows_in: Rows the reader produced, malformed lines included.
        rows_out: Rows that reached the `BarFrame`.
        rejects_by_reason: Reject count per `RejectReason` value; empty when clean.
        was_sorted: True when the source was already in canonical order. False is not
            an error — it is worth knowing, because an unordered vendor file is often
            the first sign of a concatenation bug upstream.
        contracts: Distinct contracts, sorted.
        span: `(first, last)` canonical instant, or `None` for an empty frame.
    """

    source: str
    profile: str
    frequency: Frequency
    rows_in: int
    rows_out: int
    rejects_by_reason: Mapping[str, int] = field(default_factory=dict)
    was_sorted: bool = True
    contracts: tuple[str, ...] = ()
    span: tuple[datetime, datetime] | None = None

    @property
    def rows_rejected(self) -> int:
        """Total rejected rows."""
        return sum(self.rejects_by_reason.values())

    @property
    def reject_ratio(self) -> float:
        """Rejected share of the input; 0.0 for an empty file."""
        return self.rows_rejected / self.rows_in if self.rows_in else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Plain-python view, ready for JSON."""
        return {
            "source": self.source,
            "profile": self.profile,
            "frequency": self.frequency.value,
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "rows_rejected": self.rows_rejected,
            "rejects_by_reason": dict(self.rejects_by_reason),
            "was_sorted": self.was_sorted,
            "contracts": list(self.contracts),
            "span": [self.span[0], self.span[1]] if self.span else None,
        }
