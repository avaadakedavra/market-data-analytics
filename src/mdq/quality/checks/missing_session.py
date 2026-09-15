"""`missing_session` — a weekday with no bars at all, inside a contract's trading life.

The naive version of this check needs an exchange holiday calendar. This one does not,
and that is deliberate (PLAN §9 lists holiday calendars as an explicit non-goal). Two
observations replace it:

* **Bounds from the data.** A contract cannot be "missing" a session before it was
  listed or after it expired, so the search window is the contract's own first and last
  ACTIVE session — observed from the activity profile, never from a listing calendar.
* **Corroboration across contracts.** If *every* contract on the exchange is absent on
  the same date, that is a holiday, and the finding is INFO. If one contract is absent
  while its siblings traded, that is a real hole in the feed: WARNING.

US holidays therefore surface as INFO on the real data with no calendar shipped, and the
insights layer can turn a cluster of them into a suggested `holiday_calendar` rule
(PLAN §5 rule 7).

Weekends are excluded outright: `session_date` is already rolled, so a CME session dated
Monday opens on Sunday evening and Saturday/Sunday session dates do not exist.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.emit import EMPTY_ROW_IDS, findings_from_rows, no_findings
from mdq.quality.registry import CheckContext, register_check

__all__ = ["MissingSession"]

_FIRST = "first_active"
_LAST = "last_active"
_SIBLING = "_sibling_traded"
#: Polars weekdays run Monday=1 .. Sunday=7.
_FRIDAY = 5


@register_check
class MissingSession:
    """A Mon–Fri `session_date` with no bars, inside a contract's ACTIVE trading life."""

    id: ClassVar[str] = "missing_session"
    title: ClassVar[str] = "Missing trading session"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.WARNING
    suggested_rule_id: ClassVar[str | None] = "holiday_calendar"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """One finding per absent weekday session, INFO when the whole exchange is absent."""
        span = ctx.profile.active_span()
        expected = (
            span.select(
                C.CONTRACT,
                pl.date_ranges(pl.col(_FIRST), pl.col(_LAST), "1d").alias(C.SESSION_DATE),
            )
            # An empty range (a contract with a single ACTIVE session) must vanish, not
            # become a null row — hence `empty_as_null=True` rather than the 2.0 default.
            .explode(C.SESSION_DATE, empty_as_null=True)
            .drop_nulls(C.SESSION_DATE)
            .filter(pl.col(C.SESSION_DATE).dt.weekday() <= _FRIDAY)
        )
        present = bars.select(C.CONTRACT, C.SESSION_DATE).unique()
        contract_exchange = bars.group_by(C.CONTRACT).agg(
            pl.col(C.EXCHANGE).drop_nulls().first().alias(C.EXCHANGE)
        )
        siblings = _sibling_sessions(bars, ctx)

        missing = (
            expected.join(present, on=[C.CONTRACT, C.SESSION_DATE], how="anti")
            .join(contract_exchange, on=C.CONTRACT, how="left")
            .join(siblings, on=[C.EXCHANGE, C.SESSION_DATE], how="left")
            .with_columns(pl.col(_SIBLING).fill_null(value=False))
            .sort(C.CONTRACT, C.SESSION_DATE)
        )
        if missing.select(pl.len()).collect().item() == 0:
            return no_findings()
        return findings_from_rows(
            missing,
            check_id=self.id,
            frequency=ctx.frequency,
            severity=pl.when(pl.col(_SIBLING))
            .then(pl.lit(Severity.WARNING.label))
            .otherwise(pl.lit(Severity.INFO.label)),
            message=pl.format(
                "no bars for {} on {} ({})",
                pl.col(C.CONTRACT),
                pl.col(C.SESSION_DATE).dt.to_string("%Y-%m-%d"),
                pl.when(pl.col(_SIBLING))
                .then(pl.lit("other contracts on the exchange did trade"))
                .otherwise(pl.lit("no contract on the exchange traded — holiday-like")),
            ),
            suggested_rule_id=self.suggested_rule_id,
            session_date=pl.col(C.SESSION_DATE),
            row_ids=EMPTY_ROW_IDS,
            evidence={
                "exchange": pl.col(C.EXCHANGE),
                "sibling_contracts_traded": pl.col(_SIBLING),
                "weekday": pl.col(C.SESSION_DATE).dt.to_string("%A"),
            },
        )


def _sibling_sessions(bars: pl.LazyFrame, ctx: CheckContext) -> pl.LazyFrame:
    """`(exchange, session_date, _sibling_traded)` — sessions any contract traded on.

    Supplied by the service when several datasets are loaded together; otherwise derived
    from the frame in hand, which is still enough to spot a holiday whenever more than
    one contract of an exchange is present.
    """
    if ctx.sibling_contracts is not None:
        source = ctx.sibling_contracts
        columns = source.collect_schema().names()
        if "has_bars" in columns:
            source = source.filter(pl.col("has_bars"))
        return (
            source.select(C.EXCHANGE, C.SESSION_DATE)
            .unique()
            .with_columns(pl.lit(value=True).alias(_SIBLING))
        )
    return (
        bars.select(C.EXCHANGE, C.SESSION_DATE)
        .unique()
        .with_columns(pl.lit(value=True).alias(_SIBLING))
    )
