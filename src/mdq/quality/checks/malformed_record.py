"""`malformed_record` — input rows ingestion could not place on the timeline at all.

Every other check looks at bars. This one looks at what never *became* a bar: the
`rejects` frame produced by normalisation (a ragged CSV line, text in a price column, a
blank contract, a wall clock inside the spring-forward gap). Without it the report would
quietly describe only the rows that survived, and a file that lost 30% of its lines would
look clean.

Rejects are rows that cannot be **located** — no contract, no usable instant. Rows with
bad prices or volume are never rejected; they are kept and flagged by `missing_value`,
`non_positive_price` and friends, because a business user needs to see them in context.

**Expected shape of `CheckContext.rejects`** (`IngestResult.rejects`, PLAN §3.1):

| column | dtype | required |
|---|---|---|
| `reason` | str — a `RejectReason` value | yes |
| `row_id` | integer, position in the source file | no (nulls tolerated) |
| `contract` | str | no |
| `source` | str — file or upload name | no |
| `raw_values` | str — the offending input, for the evidence | no |

Extra columns are ignored, so WP2 can enrich the frame without touching this check. The
context is per-frequency, so pass the rejects belonging to the frame being checked.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.emit import MAX_EVIDENCE_ROW_IDS, findings_from_rows, no_findings
from mdq.quality.registry import CheckContext, register_check

__all__ = ["MalformedRecord"]

_ROW_IDS = "_row_ids"
_TRUNCATED = "_truncated"
_OPTIONAL_TEXT = ("source", "raw_values")


@register_check
class MalformedRecord:
    """Input rows rejected at ingestion, grouped by contract and reason."""

    id: ClassVar[str] = "malformed_record"
    title: ClassVar[str] = "Malformed input record"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.ERROR
    suggested_rule_id: ClassVar[str | None] = "schema_contract"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:  # noqa: ARG002
        """One finding per `(contract, reason)` in `ctx.rejects`; nothing if there are none."""
        rejects = ctx.rejects
        if rejects is None or rejects.height == 0:
            return no_findings()
        columns = set(rejects.columns)
        if C.REASON not in columns:
            raise ValueError(
                f"rejects frame must carry a {C.REASON!r} column; got {sorted(columns)}"
            )

        row_id = (
            pl.col(C.ROW_ID).cast(pl.UInt32) if C.ROW_ID in columns else pl.lit(None, pl.UInt32)
        )
        contract = (
            pl.col(C.CONTRACT).cast(pl.String) if C.CONTRACT in columns else pl.lit(None, pl.String)
        )
        samples = {
            name: pl.col(name).cast(pl.String).drop_nulls().first().alias(name)
            for name in _OPTIONAL_TEXT
            if name in columns
        }

        grouped = (
            rejects.lazy()
            .with_columns(row_id.alias(C.ROW_ID), contract.alias(C.CONTRACT))
            .group_by(C.CONTRACT, C.REASON)
            .agg(
                pl.len().cast(pl.UInt32).alias(C.COUNT),
                pl.col(C.ROW_ID).drop_nulls().sort().head(MAX_EVIDENCE_ROW_IDS).alias(_ROW_IDS),
                *samples.values(),
            )
            .with_columns((pl.col(C.COUNT) > MAX_EVIDENCE_ROW_IDS).alias(_TRUNCATED))
            .sort(C.CONTRACT, C.REASON, nulls_last=True)
        )
        return findings_from_rows(
            grouped,
            check_id=self.id,
            frequency=ctx.frequency,
            severity=pl.lit(self.default_severity.label),
            message=pl.format(
                "{} input row(s) rejected at ingestion: {}", pl.col(C.COUNT), pl.col(C.REASON)
            ),
            suggested_rule_id=self.suggested_rule_id,
            count=pl.col(C.COUNT),
            row_ids=pl.col(_ROW_IDS),
            row_ids_truncated=pl.col(_TRUNCATED),
            evidence={
                "reason": pl.col(C.REASON),
                **{name: pl.col(name) for name in samples},
            },
        )
