"""`duplicate_timestamp` — two or more bars for one `(contract, ts_utc)`.

The severity split is the whole point. Two bars that agree on every value are a
re-delivery: annoying, safe to collapse, INFO. Two bars that *disagree* are a
contradiction — there is no way to know which price was real — so they are an ERROR and
`cleanse` drops both rather than silently picking one.

The real sample contains **zero** duplicates (independently confirmed against the
vendor's own `duplicate_timestamp_count`, 0 mismatches on 40/40 daily files), so this
check is exercised by the fault harness and its golden expectation on real data is zero.

One genuine source of duplicates does exist in the wild: under `ambiguous="earliest"`
the two passes of the fall-back hour receive the *same* `ts_utc`, so they necessarily
trip this check as well as `ambiguous_local_time`. That is the correct reading — the
vendor's wall clock simply does not distinguish them.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.emit import SEVERITY_COL, aggregate_by_session
from mdq.quality.registry import CheckContext, register_check

__all__ = ["DuplicateTimestamp"]

#: Columns that must agree for two bars at the same instant to be "the same bar".
_VALUES = (*C.PRICES, C.VOLUME, C.OPEN_INTEREST)
_KEY = (C.CONTRACT, C.TS_UTC)
_GROUP_SIZE = "_group_size"
_CONFLICTING = "_conflicting"


@register_check
class DuplicateTimestamp:
    """Repeated `(contract, ts_utc)`; ERROR when the repeats disagree."""

    id: ClassVar[str] = "duplicate_timestamp"
    title: ClassVar[str] = "Duplicate timestamp"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.ERROR
    suggested_rule_id: ClassVar[str | None] = "drop_exact_duplicates"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings covering every row that shares its instant with another."""
        # A value column that takes more than one value within the key is a conflict.
        # `n_unique` counts null as a value, so a null-vs-number pair is a conflict too.
        conflicting = pl.any_horizontal(
            pl.col(value).n_unique().over(*_KEY) > 1 for value in _VALUES
        )
        violations = (
            bars.with_columns(
                pl.len().over(*_KEY).alias(_GROUP_SIZE),
                conflicting.alias(_CONFLICTING),
            )
            .filter(pl.col(_GROUP_SIZE) > 1)
            .with_columns(
                pl.when(pl.col(_CONFLICTING))
                .then(pl.lit(Severity.ERROR.label))
                .otherwise(pl.lit(Severity.INFO.label))
                .alias(SEVERITY_COL)
            )
        )
        return aggregate_by_session(
            violations,
            check_id=self.id,
            frequency=ctx.frequency,
            message=pl.format(
                "{} bar(s) share {} duplicated timestamp(s)",
                pl.col(C.COUNT),
                pl.col("distinct_timestamps"),
            ),
            suggested_rule_id=pl.when(pl.col(SEVERITY_COL) == Severity.ERROR.label)
            .then(pl.lit("reject_conflicting_duplicates"))
            .otherwise(pl.lit("drop_exact_duplicates")),
            evidence={
                "distinct_timestamps": pl.col(C.TS_UTC).n_unique().cast(pl.UInt32),
                "largest_group": pl.col(_GROUP_SIZE).max().cast(pl.UInt32),
                "conflicting": pl.col(_CONFLICTING).any(),
            },
        )
