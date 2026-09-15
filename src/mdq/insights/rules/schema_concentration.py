"""Rule 9 — the bad input is all bad in the same place.

Rows that arrive broken are reported one at a time, which makes a systematic problem look
like a stream of unrelated accidents. It usually is not. A supplier whose volume column
occasionally ships an empty string, a spreadsheet exported with thousands separators, a
file whose timestamp column changed format between deliveries — each of these shows up as
dozens of individually unremarkable rejects that all name the *same field*. Once that is
said out loud the fix stops being "clean these rows" and becomes "declare what this column
must contain and refuse the file at the door", which is a thing a supplier can be held to.

The rule pools two sources of bad input, because a business user does not distinguish
them and should not have to:

* `malformed_record` — rows ingestion could not place at all (a blank contract, an
  unparseable timestamp, a line that never split into fields). The reject reason names
  the field.
* `missing_value` — rows that *were* placed but carry a null price or volume. The check
  publishes a per-column null count, so the field is named there too.

If one field accounts for most of the damage, that field gets a schema contract. If the
damage is spread evenly, it does not: a file with a little of everything wrong is a
different problem, and claiming otherwise would send someone to argue with a supplier
about the wrong column.

The field names are this tool's **canonical** ones (`volume`, `close`, `timestamp`); the
source profile maps them to whatever the supplier's file calls them.

**On the real sample this rule cannot fire.** The vendor's sample has zero nulls and zero
unplaceable rows across all 5.3M minute and 30k daily values — 0 rejects, and a
`null_price_row_count` of 0 reproduced against the vendor's own published figure on 40 of
40 daily files. There is nothing for it to concentrate on, and manufacturing something
would misrepresent the data. It fires on uploaded CSVs and on the fault harness.
"""

from __future__ import annotations

from typing import ClassVar, Final

import polars as pl

from mdq.domain.findings import RejectReason
from mdq.domain.schema import C
from mdq.insights.engine import (
    InsightContext,
    decode_evidence,
    distinct,
    evidence_share,
    register_rule,
)
from mdq.insights.models import Insight, SuggestedRule

__all__ = ["SchemaConcentration"]

_REJECT_CHECK: Final = "malformed_record"
_NULL_CHECK: Final = "missing_value"
_REASON: Final = "reason"
_SOURCE: Final = "source"
_RAW: Final = "raw_values"

#: The canonical field a rejected row failed on, plus the type a contract would declare.
#: `None` means the failure was structural — the line never became fields at all — and the
#: contract is then about the file's layout rather than about one column.
_TIMESTAMP: Final = "timestamp"
_RECORD_TYPE: Final = "record"

_REASON_FIELDS: Final[dict[str, str | None]] = {
    RejectReason.UNPARSEABLE_TIMESTAMP.value: _TIMESTAMP,
    RejectReason.NONEXISTENT_LOCAL_TIME.value: _TIMESTAMP,
    RejectReason.NULL_CONTRACT.value: C.CONTRACT,
    RejectReason.MISSING_REQUIRED_COLUMN.value: None,
    RejectReason.MALFORMED_LINE.value: None,
}

_FIELD_TYPES: Final[dict[str, str]] = {
    C.OPEN: "number",
    C.HIGH: "number",
    C.LOW: "number",
    C.CLOSE: "number",
    C.VOLUME: "integer",
    C.CONTRACT: "string",
    _TIMESTAMP: "timestamp",
}

#: `missing_value` publishes one null counter per field it guards.
_NULL_FIELDS: Final[tuple[str, ...]] = (*C.PRICES, C.VOLUME)

_REJECT_EVIDENCE: Final[dict[str, pl.DataType]] = {
    _REASON: pl.String(),
    _SOURCE: pl.String(),
    _RAW: pl.String(),
}
_NULL_EVIDENCE: Final[dict[str, pl.DataType]] = {
    f"null_{field}": pl.UInt32() for field in _NULL_FIELDS
}


@register_rule
class SchemaConcentration:
    """Rejected and null-bearing rows that pile up on one field."""

    id: ClassVar[str] = "bad_input_concentrates_in_one_field"
    title: ClassVar[str] = "The broken rows are all broken in the same column"

    #: Share of all bad values that must land on one field before a contract for that
    #: field is the right prescription rather than a distraction.
    min_share: ClassVar[float] = 0.6

    def applies(self, ctx: InsightContext) -> bool:
        """True when bad input exists and most of it fails on a single field."""
        tally = _tally(ctx)
        if not tally:
            return False
        total = sum(count for _, count in tally)
        return evidence_share(tally[0][1], total) >= self.min_share

    def build(self, ctx: InsightContext) -> Insight:
        """Name the field, quantify the concentration and propose a schema contract."""
        findings = ctx.subset([_REJECT_CHECK, _NULL_CHECK])
        tally = _tally(ctx)
        field, worst = tally[0]
        total = sum(count for _, count in tally)
        share = evidence_share(worst, total)
        structural = field is None
        field_type = _RECORD_TYPE if field is None else _FIELD_TYPES.get(field, "string")
        label = "the row layout itself" if field is None else f"the {field} column"
        reasons = _reason_counts(ctx)
        sources = _sources(ctx)
        example = _example(ctx)
        contracts = ctx.contracts_in(findings)

        pattern = (
            f"{total:,} input value(s) could not be used — some rows were rejected "
            f"outright, some arrived with a field empty. They are not scattered across "
            f"the file: {worst:,} of them ({share:.0%}) fail on {label}. "
            f"{_structural_sentence(structural, field_type)}"
            f"{_source_sentence(sources)}"
            f"{_example_sentence(example)}"
            f"A problem this concentrated is a supplier problem, not a row problem — "
            f"cleaning these rows one delivery at a time fixes nothing, because the next "
            f"delivery will bring more of them."
        )
        rationale = (
            f"Declare what {label} must contain — {field_type}, present on every row — "
            f"and enforce it at ingestion so a delivery that breaks the contract is "
            f"refused with a reason the supplier can act on, instead of being silently "
            f"repaired downstream. The declaration is the thing worth having: it turns "
            f"{worst:,} recurring failures into one conversation, and it makes any future "
            f"change to the supplier's format visible on the first file rather than on "
            f"the first wrong number. The remaining {total - worst:,} bad value(s) fail "
            f"elsewhere and still need looking at. Confidence is the share of bad input "
            f"that this one field accounts for ({worst:,} of {total:,})."
        )
        return Insight(
            id=self.id,
            title=self.title,
            pattern=pattern,
            evidence={
                "checks": [_REJECT_CHECK, _NULL_CHECK],
                "bars_affected": total,
                "bad_values_in_worst_field": worst,
                "worst_field": field,
                "declared_type": field_type,
                "bad_values_by_field": {_label(name): count for name, count in tally},
                "rejects_by_reason": reasons,
                "sources": sources,
                "example_value": example,
                "share_in_worst_field": round(share, 4),
                "min_share_threshold": self.min_share,
                "affected_contract_count": ctx.contract_total(findings),
            },
            affected_contracts=contracts,
            finding_count=findings.height,
            rule=SuggestedRule(
                rule_id="schema_contract",
                kind="validation",
                params={
                    "column": field,
                    "type": field_type,
                    "required": True,
                    "applies_to": "the whole row" if structural else field,
                    "enforce_at": "ingestion, before the row reaches any analytic",
                    "on_violation": "reject the row and report it against the source file",
                    "sources": sources,
                    "also_failing": {_label(name): count for name, count in tally[1:]},
                },
                rationale=rationale,
                confidence=share,
            ),
        )


def _label(field: str | None) -> str:
    """A field name a reader recognises; structural failures are not about a column."""
    return field if field is not None else "(the whole row)"


def _tally(ctx: InsightContext) -> list[tuple[str | None, int]]:
    """Bad values per canonical field, worst first; `None` is a structural failure.

    Nulls and rejects are pooled deliberately: an empty cell and an unparseable cell are
    the same defect to the person who has to ring the supplier about it.
    """
    counts: dict[str | None, int] = {}
    for field, count in (*_null_counts(ctx), *_reject_counts(ctx)):
        counts[field] = counts.get(field, 0) + count
    return sorted(
        ((field, count) for field, count in counts.items() if count > 0),
        key=lambda item: (-item[1], item[0] or ""),
    )


def _null_counts(ctx: InsightContext) -> list[tuple[str | None, int]]:
    """Per-column null counts published by `missing_value`."""
    findings = ctx.subset(_NULL_CHECK)
    if findings.height == 0:
        return []
    decoded = decode_evidence(findings, _NULL_EVIDENCE)
    return [(field, int(decoded.get_column(f"null_{field}").sum() or 0)) for field in _NULL_FIELDS]


def _reject_counts(ctx: InsightContext) -> list[tuple[str | None, int]]:
    """Rejected rows per canonical field, via the reason each was rejected for."""
    grouped = _rejects_by_reason(ctx)
    return [(_REASON_FIELDS.get(str(reason)), int(count)) for reason, count in grouped.iter_rows()]


def _reason_counts(ctx: InsightContext) -> dict[str, int]:
    """Rejected rows per reason, for the evidence block."""
    grouped = _rejects_by_reason(ctx)
    return {str(reason): int(count) for reason, count in grouped.iter_rows()}


def _rejects_by_reason(ctx: InsightContext) -> pl.DataFrame:
    """`(reason, rows)` over `malformed_record` findings; empty when there are none."""
    findings = ctx.subset(_REJECT_CHECK)
    if findings.height == 0:
        return pl.DataFrame(schema=pl.Schema([(_REASON, pl.String), ("rows", pl.UInt32)]))
    return (
        decode_evidence(findings, _REJECT_EVIDENCE)
        .with_columns(pl.col(_REASON).fill_null("UNKNOWN"))
        .group_by(_REASON)
        .agg(pl.col(C.COUNT).sum().cast(pl.UInt32).alias("rows"))
        .sort(_REASON)
    )


def _sources(ctx: InsightContext) -> list[str]:
    """Source files the rejects came from — PLAN §5's "or file" axis.

    `malformed_record` samples one source per `(contract, reason)` group rather than
    listing every one, so this is a lower bound on the number of files involved. The
    prose says "at least" for that reason.
    """
    findings = ctx.subset(_REJECT_CHECK)
    if findings.height == 0:
        return []
    decoded = decode_evidence(findings, _REJECT_EVIDENCE)
    return [str(source) for source in distinct(decoded, _SOURCE)]


def _example(ctx: InsightContext) -> str | None:
    """One offending input value, so the prose can show rather than assert."""
    findings = ctx.subset(_REJECT_CHECK)
    if findings.height == 0:
        return None
    values = decode_evidence(findings, _REJECT_EVIDENCE).get_column(_RAW).drop_nulls()
    return str(values[0]) if len(values) else None


def _structural_sentence(structural: bool, field_type: str) -> str:
    """Explain what "failing on this field" means for the reader."""
    if structural:
        return (
            "Those rows never split into columns at all — a ragged line, a stray "
            "delimiter or a missing header — so no per-column rule can catch them; the "
            "file's layout is what has to be pinned down. "
        )
    return (
        f"Every one of those rows was rejected or blanked because that field did not "
        f"hold a usable {field_type} value; the rest of the row was fine. "
    )


def _source_sentence(sources: list[str]) -> str:
    """Name the file when the damage all came from one."""
    if len(sources) == 1:
        return f"All of them arrived in a single file, {sources[0]}. "
    if len(sources) > 1:
        return f"They arrived across at least {len(sources)} different files. "
    return ""


def _example_sentence(example: str | None) -> str:
    """Show one offending value; abstract descriptions of bad data help nobody."""
    if not example:
        return ""
    return f"One of the offending values reads {example!r}. "
