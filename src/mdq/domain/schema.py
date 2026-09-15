"""The canonical bar schema and the `BarFrame` carrier.

Every source file, every frequency and every upload is normalised onto exactly one
Polars schema (`BAR_SCHEMA`). Column names are exposed as constants on `C` so that no
string literal for a column ever leaks into analytics, checks or the API.

`BarFrame` is the object the whole application passes around: a lazy frame that is
*guaranteed* to conform to `BAR_SCHEMA`, tagged with its frequency and provenance.
Validation happens once, at construction, so no downstream module has to re-check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import polars as pl

from mdq.domain.frequency import Frequency

__all__ = [
    "BAR_SCHEMA",
    "FINDING_SCHEMA",
    "BarFrame",
    "C",
    "SchemaValidationError",
    "empty_bar_lf",
    "empty_finding_df",
]


class C:
    """Column-name constants.

    The first block is `BAR_SCHEMA`; the second is `FINDING_SCHEMA`; the third is
    intermediate columns produced by the time layer that never reach a `BarFrame`.
    """

    # --- BAR_SCHEMA ------------------------------------------------------- #
    ROW_ID: Final = "row_id"
    CONTRACT: Final = "contract"
    EXCHANGE: Final = "exchange"
    ROOT: Final = "root"
    TS_UTC: Final = "ts_utc"
    TS_LOCAL: Final = "ts_local"
    SESSION_DATE: Final = "session_date"
    OPEN: Final = "open"
    HIGH: Final = "high"
    LOW: Final = "low"
    CLOSE: Final = "close"
    VOLUME: Final = "volume"
    OPEN_INTEREST: Final = "open_interest"

    # --- FINDING_SCHEMA --------------------------------------------------- #
    CHECK_ID: Final = "check_id"
    SEVERITY: Final = "severity"
    FREQUENCY: Final = "frequency"
    START_UTC: Final = "start_utc"
    END_UTC: Final = "end_utc"
    COUNT: Final = "count"
    MESSAGE: Final = "message"
    EVIDENCE: Final = "evidence"
    SUGGESTED_RULE_ID: Final = "suggested_rule_id"

    # --- intermediate (time layer) ---------------------------------------- #
    IS_AMBIGUOUS: Final = "is_ambiguous"
    REASON: Final = "reason"

    #: The OHLC price columns, in canonical order.
    PRICES: Final = (OPEN, HIGH, LOW, CLOSE)


#: The canonical bar schema. Order is part of the contract.
BAR_SCHEMA: Final[pl.Schema] = pl.Schema(
    [
        (C.ROW_ID, pl.UInt32),
        (C.CONTRACT, pl.String),
        (C.EXCHANGE, pl.String),
        (C.ROOT, pl.String),
        (C.TS_UTC, pl.Datetime("us", "UTC")),
        (C.TS_LOCAL, pl.Datetime("us")),
        (C.SESSION_DATE, pl.Date),
        (C.OPEN, pl.Float64),
        (C.HIGH, pl.Float64),
        (C.LOW, pl.Float64),
        (C.CLOSE, pl.Float64),
        (C.VOLUME, pl.Int64),
        (C.OPEN_INTEREST, pl.Int64),
    ]
)

#: The schema every quality check returns. `evidence` is a JSON-encoded object.
FINDING_SCHEMA: Final[pl.Schema] = pl.Schema(
    [
        (C.CHECK_ID, pl.String),
        (C.SEVERITY, pl.String),
        (C.CONTRACT, pl.String),
        (C.FREQUENCY, pl.String),
        (C.START_UTC, pl.Datetime("us", "UTC")),
        (C.END_UTC, pl.Datetime("us", "UTC")),
        (C.SESSION_DATE, pl.Date),
        (C.COUNT, pl.UInt32),
        (C.MESSAGE, pl.String),
        (C.EVIDENCE, pl.String),
        (C.SUGGESTED_RULE_ID, pl.String),
    ]
)


class SchemaValidationError(ValueError):
    """Raised when a frame does not conform to `BAR_SCHEMA`."""


def empty_bar_lf() -> pl.LazyFrame:
    """An empty lazy frame with exactly `BAR_SCHEMA`."""
    return pl.DataFrame(schema=BAR_SCHEMA).lazy()


def empty_finding_df() -> pl.DataFrame:
    """An empty data frame with exactly `FINDING_SCHEMA`."""
    return pl.DataFrame(schema=FINDING_SCHEMA)


def _describe_mismatch(actual: pl.Schema, expected: pl.Schema) -> str | None:
    """Return a human-readable description of how `actual` deviates, or None."""
    missing = [name for name in expected.names() if name not in actual]
    extra = [name for name in actual.names() if name not in expected]
    wrong_type = [
        f"{name}: expected {expected[name]}, got {actual[name]}"
        for name in expected.names()
        if name in actual and actual[name] != expected[name]
    ]
    problems: list[str] = []
    if missing:
        problems.append(f"missing columns {missing}")
    if extra:
        problems.append(f"unexpected columns {extra}")
    if wrong_type:
        problems.append("wrong dtypes -> " + "; ".join(wrong_type))
    if not problems and actual.names() != expected.names():
        problems.append(f"wrong column order: {actual.names()} != {expected.names()}")
    return ", ".join(problems) if problems else None


def validate_bar_schema(frame: pl.LazyFrame | pl.DataFrame, *, source: str = "<frame>") -> None:
    """Raise `SchemaValidationError` unless `frame` conforms exactly to `BAR_SCHEMA`."""
    actual = frame.collect_schema() if isinstance(frame, pl.LazyFrame) else frame.schema
    problem = _describe_mismatch(actual, BAR_SCHEMA)
    if problem is not None:
        raise SchemaValidationError(f"{source} does not conform to BAR_SCHEMA: {problem}")


@dataclass(frozen=True)
class BarFrame:
    """A lazy frame guaranteed to conform to `BAR_SCHEMA`.

    Frames are homogeneous in frequency: `frequency` is metadata, not a column, which
    is why mixed-frequency files are rejected at ingestion rather than split.
    """

    lf: pl.LazyFrame
    frequency: Frequency
    source: str = "<memory>"

    def __post_init__(self) -> None:
        # A DataFrame is accepted for convenience and immediately made lazy, so that
        # every consumer of `.lf` sees a LazyFrame and nothing else.
        raw: object = self.lf
        if isinstance(raw, pl.DataFrame):
            object.__setattr__(self, "lf", raw.lazy())
        elif not isinstance(raw, pl.LazyFrame):
            raise SchemaValidationError(
                f"BarFrame.lf must be a polars LazyFrame or DataFrame, got {type(raw).__name__}"
            )
        validate_bar_schema(self.lf, source=f"BarFrame(source={self.source!r})")

    @classmethod
    def empty(cls, frequency: Frequency, source: str = "<empty>") -> BarFrame:
        """An empty but schema-correct `BarFrame` — the neutral element everywhere."""
        return cls(empty_bar_lf(), frequency, source)

    def collect(self) -> pl.DataFrame:
        """Materialise the frame."""
        return self.lf.collect()

    def contracts(self) -> list[str]:
        """Sorted distinct contract codes present in the frame."""
        series = self.lf.select(pl.col(C.CONTRACT).unique().sort()).collect().get_column(C.CONTRACT)
        return [c for c in series.to_list() if c is not None]

    def is_empty(self) -> bool:
        """True when the frame holds no rows."""
        return int(self.lf.select(pl.len()).collect().item()) == 0

    def with_lf(self, lf: pl.LazyFrame) -> BarFrame:
        """A copy carrying a transformed frame, re-validated against `BAR_SCHEMA`."""
        return BarFrame(lf, self.frequency, self.source)
