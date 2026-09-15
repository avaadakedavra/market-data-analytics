"""Vectorised finding emitters shared by every check.

A check never builds Python objects per row. It narrows its bar frame to the violating
rows with Polars expressions, hands the result to one of the two emitters here, and the
emitter produces a `FINDING_SCHEMA` frame — including the JSON evidence, which is
encoded with `struct.json_encode()` rather than `json.dumps` in a loop.

Two shapes cover all fourteen checks:

* `aggregate_by_session` — row-level defects (a null price, a broken OHLC relation).
  One finding per `(contract, session_date, severity)` so the finding stays small and
  quotable while `count` still reports every bar. Splitting on severity is what lets one
  check emit ERROR for ACTIVE sessions and INFO for DORMANT ones in a single pass.
* `findings_from_rows` — defects that are already one-per-finding (a gap, a missing
  session, an ingest reject), where the caller supplies each column directly.

Evidence always carries `row_ids`. `cleanse` reads them back, so they are *complete* up
to `MAX_EVIDENCE_ROW_IDS` and the `row_ids_truncated` flag says when they are not.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import FINDING_SCHEMA, C, empty_finding_df
from mdq.quality.context import A, Regime

__all__ = [
    "EMPTY_ROW_IDS",
    "MAX_EVIDENCE_ROW_IDS",
    "SEVERITY_COL",
    "aggregate_by_session",
    "findings_from_rows",
    "no_findings",
    "regime_severity",
]

#: How many `row_id`s one finding carries. Deliberately generous rather than the "sample
#: of 20" of the original sketch: `cleanse` drops rows *by id*, so a 20-id sample would
#: silently leave bad rows in the cleansed frame. `row_ids_truncated` marks the cases
#: that still exceed it, and `QualityReport.truncated_checks()` surfaces them.
MAX_EVIDENCE_ROW_IDS: Final = 1_000

#: The column a check writes its per-row severity into before calling an emitter.
SEVERITY_COL: Final = "_severity"

_ROW_IDS: Final = "row_ids"
_TRUNCATED: Final = "row_ids_truncated"
_UTC = pl.Datetime("us", "UTC")

#: `[]` as an expression — for findings that implicate no row at all (a gap, a session
#: that never arrived). `cleanse` must see an empty list, not a null.
EMPTY_ROW_IDS: Final[pl.Expr] = pl.concat_list(pl.lit(None, pl.UInt32)).list.drop_nulls()


def no_findings() -> pl.DataFrame:
    """The empty, schema-correct result — what a clean frame produces."""
    return empty_finding_df()


def regime_severity(
    normal: Severity,
    dormant: Severity,
    thin: Severity | None = None,
) -> pl.Expr:
    """A severity expression conditioned on the joined activity `regime`.

    A null regime (a session the profile never saw) yields `normal`: absence of
    evidence that trading was dormant is never treated as evidence that it was.
    """
    thin_severity = thin if thin is not None else normal
    return (
        pl.when(pl.col(A.REGIME) == Regime.DORMANT.value)
        .then(pl.lit(dormant.label))
        .when(pl.col(A.REGIME) == Regime.THIN.value)
        .then(pl.lit(thin_severity.label))
        .otherwise(pl.lit(normal.label))
        .alias(SEVERITY_COL)
    )


def aggregate_by_session(
    violations: pl.LazyFrame,
    *,
    check_id: str,
    frequency: Frequency,
    message: pl.Expr,
    suggested_rule_id: str | pl.Expr | None = None,
    evidence: Mapping[str, pl.Expr] | None = None,
) -> pl.DataFrame:
    """One finding per `(contract, session_date, severity)` over violating rows.

    Args:
        violations: Rows that violate the check. Must carry `row_id`, `contract`,
            `session_date`, `ts_utc` and `SEVERITY_COL`.
        check_id: Written into every finding.
        frequency: Written into every finding.
        message: Expression evaluated **after** aggregation, so it may reference
            `count` and any key of `evidence`.
        suggested_rule_id: Rule the insights layer should propose for this pattern. May
            be an expression when the rule depends on the finding (a conflicting
            duplicate needs a different rule from an exact one).
        evidence: Extra aggregate expressions (e.g. `pl.col("high").min()`) merged into
            the JSON evidence next to `row_ids`.
    """
    extra = dict(evidence or {})
    grouped = violations.group_by(C.CONTRACT, C.SESSION_DATE, SEVERITY_COL).agg(
        pl.len().cast(pl.UInt32).alias(C.COUNT),
        pl.col(C.TS_UTC).min().alias(C.START_UTC),
        pl.col(C.TS_UTC).max().alias(C.END_UTC),
        pl.col(C.ROW_ID).sort().head(MAX_EVIDENCE_ROW_IDS).alias(_ROW_IDS),
        *(expr.alias(name) for name, expr in extra.items()),
    )
    evidence_struct = pl.struct(
        pl.col(_ROW_IDS),
        (pl.col(C.COUNT) > MAX_EVIDENCE_ROW_IDS).alias(_TRUNCATED),
        *(pl.col(name) for name in extra),
    ).struct.json_encode()
    return _finalise(
        grouped.with_columns(
            pl.lit(check_id).alias(C.CHECK_ID),
            pl.col(SEVERITY_COL).alias(C.SEVERITY),
            pl.lit(frequency.value).alias(C.FREQUENCY),
            message.alias(C.MESSAGE),
            evidence_struct.alias(C.EVIDENCE),
            _rule_expr(suggested_rule_id).alias(C.SUGGESTED_RULE_ID),
        )
    )


def findings_from_rows(
    rows: pl.LazyFrame,
    *,
    check_id: str,
    frequency: Frequency,
    severity: pl.Expr,
    message: pl.Expr,
    suggested_rule_id: str | pl.Expr | None = None,
    evidence: Mapping[str, pl.Expr] | None = None,
    row_ids: pl.Expr | None = None,
    row_ids_truncated: pl.Expr | None = None,
    count: pl.Expr | None = None,
    contract: pl.Expr | None = None,
    session_date: pl.Expr | None = None,
    start_utc: pl.Expr | None = None,
    end_utc: pl.Expr | None = None,
) -> pl.DataFrame:
    """One finding per row of `rows`, with every finding column supplied as an expression.

    Used by the checks whose natural unit is already a single finding — a gap between two
    bars, an absent session, a rejected input line — where there is nothing to group.
    """
    extra = dict(evidence or {})
    ids = (
        row_ids
        if row_ids is not None
        else pl.concat_list(pl.col(C.ROW_ID)).cast(pl.List(pl.UInt32))
    )
    truncated = row_ids_truncated if row_ids_truncated is not None else pl.lit(value=False)
    evidence_struct = pl.struct(
        ids.alias(_ROW_IDS),
        truncated.alias(_TRUNCATED),
        *(expr.alias(name) for name, expr in extra.items()),
    ).struct.json_encode()
    return _finalise(
        rows.select(
            pl.lit(check_id).alias(C.CHECK_ID),
            severity.alias(C.SEVERITY),
            (contract if contract is not None else pl.col(C.CONTRACT)).alias(C.CONTRACT),
            pl.lit(frequency.value).alias(C.FREQUENCY),
            (start_utc if start_utc is not None else pl.lit(None, _UTC)).alias(C.START_UTC),
            (end_utc if end_utc is not None else pl.lit(None, _UTC)).alias(C.END_UTC),
            (session_date if session_date is not None else pl.lit(None, pl.Date)).alias(
                C.SESSION_DATE
            ),
            (count if count is not None else pl.lit(1)).cast(pl.UInt32).alias(C.COUNT),
            message.alias(C.MESSAGE),
            evidence_struct.alias(C.EVIDENCE),
            _rule_expr(suggested_rule_id).alias(C.SUGGESTED_RULE_ID),
        )
    )


def _rule_expr(suggested_rule_id: str | pl.Expr | None) -> pl.Expr:
    """Accept either a constant rule id or an expression choosing one per finding."""
    if isinstance(suggested_rule_id, pl.Expr):
        return suggested_rule_id
    return pl.lit(suggested_rule_id, pl.String)


def _finalise(lf: pl.LazyFrame) -> pl.DataFrame:
    """Project onto `FINDING_SCHEMA`, cast, and order deterministically."""
    return (
        lf.select(FINDING_SCHEMA.names())
        .cast(dict(FINDING_SCHEMA))  # type: ignore[arg-type]
        .sort([C.CONTRACT, C.SESSION_DATE, C.START_UTC, C.SEVERITY], nulls_last=True)
        .collect()
    )
