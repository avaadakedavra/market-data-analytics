"""`intrabar_gap` — minutes missing from the middle of a session that was trading.

Corpus-wide minute coverage is a median of 222 bars per contract-day (mean 350) against
~1,380 possible — 16% at the median, 25% at the mean, and only 19% if every weekday in a
contract's span is counted rather than only the days it produced bars on.
So "a bar is missing" is the *normal* state of this dataset and flagging it unconditionally
would produce millions of findings. Three filters make the check meaningful instead:

1. **ACTIVE sessions only.** ESH26's median is 2 bars/day in Jun-2025 and 1,379 in
   Mar-2026. A hole in the first is the contract not trading yet; a hole in the second is
   a feed outage. Only the second is reported.
2. **Within a session.** Gaps are computed inside `(contract, session_date)`, so the
   Friday 15:59 → Sunday 17:00 weekend closure is never a gap: those bars belong to
   different sessions by construction.
3. **Net of the maintenance break.** Any part of the gap that falls inside the
   exchange's `break_local` window (16:00–17:00 CT for the CME family) is subtracted
   before the threshold is applied and before `minutes_missing` is reported. With a 17:00
   roll the break already sits at the session edge, so this is belt-and-braces for venues
   whose break falls mid-session — and it means the check does not depend on the roll
   hour and the break hour happening to coincide.

One finding per hole. `count` is `minutes_missing`, the number of bars that should have
been there: a 40-minute jump between consecutive bars means 39 absent minutes.
"""

from __future__ import annotations

from datetime import time
from typing import ClassVar

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality.context import A, Regime
from mdq.quality.emit import findings_from_rows, no_findings
from mdq.quality.registry import CheckContext, register_check
from mdq.time.sessions import SessionProfile

__all__ = ["IntrabarGap"]

_PREV_UTC = "_prev_utc"
_PREV_LOCAL = "_prev_local"
_PREV_ROW_ID = "_prev_row_id"
_GAP = "_gap_minutes"
_BREAK = "_break_minutes"
_MISSING = "_minutes_missing"
_SESSION_KEY = (C.CONTRACT, C.SESSION_DATE)


def _minute_of_day(expr: pl.Expr) -> pl.Expr:
    """Local minute of day. The cast is load-bearing: `dt.hour()` is Int8, and `15 * 60`
    silently wraps to a negative number without it."""
    return expr.dt.hour().cast(pl.Int64) * 60 + expr.dt.minute().cast(pl.Int64)


def _break_bounds(sessions: dict[str, SessionProfile]) -> tuple[dict[str, int], dict[str, int]]:
    """Per-exchange maintenance window as minutes-of-day, for a vectorised lookup."""
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    for exchange, profile in sessions.items():
        if profile.break_local is None:
            continue
        start, end = profile.break_local
        starts[exchange.upper()] = _as_minutes(start)
        ends[exchange.upper()] = _as_minutes(end)
    return starts, ends


def _as_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


@register_check
class IntrabarGap:
    """A hole larger than `max_gap_minutes` inside an ACTIVE minute session."""

    id: ClassVar[str] = "intrabar_gap"
    title: ClassVar[str] = "Missing minutes inside an active session"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset({Frequency.MINUTE})
    default_severity: ClassVar[Severity] = Severity.WARNING
    suggested_rule_id: ClassVar[str | None] = "session_break_window"

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """One finding per gap in an ACTIVE session, net of the maintenance break."""
        starts, ends = _break_bounds(ctx.sessions)
        exchange = pl.col(C.EXCHANGE).str.to_uppercase()
        break_start = (
            exchange.replace_strict(starts, default=None, return_dtype=pl.Int64)
            if starts
            else pl.lit(None, pl.Int64)
        )
        break_end = (
            exchange.replace_strict(ends, default=None, return_dtype=pl.Int64)
            if ends
            else pl.lit(None, pl.Int64)
        )

        active = ctx.with_regime(bars).filter(pl.col(A.REGIME) == Regime.ACTIVE.value)
        ordered = active.sort(C.CONTRACT, C.SESSION_DATE, C.TS_UTC)
        paired = ordered.with_columns(
            pl.col(C.TS_UTC).shift(1).over(*_SESSION_KEY).alias(_PREV_UTC),
            pl.col(C.TS_LOCAL).shift(1).over(*_SESSION_KEY).alias(_PREV_LOCAL),
            pl.col(C.ROW_ID).shift(1).over(*_SESSION_KEY).alias(_PREV_ROW_ID),
            break_start.alias("_break_start"),
            break_end.alias("_break_end"),
        ).filter(pl.col(_PREV_UTC).is_not_null())

        measured = paired.with_columns(
            (pl.col(C.TS_UTC) - pl.col(_PREV_UTC)).dt.total_minutes().alias(_GAP),
            _break_overlap(
                pl.col(_PREV_LOCAL),
                pl.col(C.TS_LOCAL),
                pl.col("_break_start"),
                pl.col("_break_end"),
            ).alias(_BREAK),
        ).with_columns((pl.col(_GAP) - pl.col(_BREAK) - 1).alias(_MISSING))

        gaps = measured.filter((pl.col(_GAP) - pl.col(_BREAK)) > ctx.config.max_gap_minutes)
        if gaps.select(pl.len()).collect().item() == 0:
            return no_findings()
        return findings_from_rows(
            gaps,
            check_id=self.id,
            frequency=ctx.frequency,
            severity=pl.lit(self.default_severity.label),
            message=pl.format(
                "{} minute bar(s) missing between {} and {} local in an active session",
                pl.col(_MISSING),
                pl.col(_PREV_LOCAL).dt.to_string("%Y-%m-%d %H:%M"),
                pl.col(C.TS_LOCAL).dt.to_string("%Y-%m-%d %H:%M"),
            ),
            suggested_rule_id=self.suggested_rule_id,
            count=pl.col(_MISSING),
            session_date=pl.col(C.SESSION_DATE),
            start_utc=pl.col(_PREV_UTC),
            end_utc=pl.col(C.TS_UTC),
            row_ids=pl.concat_list(pl.col(_PREV_ROW_ID), pl.col(C.ROW_ID)).cast(pl.List(pl.UInt32)),
            evidence={
                "minutes_missing": pl.col(_MISSING),
                "gap_minutes": pl.col(_GAP),
                "break_minutes_excluded": pl.col(_BREAK),
                "max_gap_minutes": pl.lit(ctx.config.max_gap_minutes),
                "session_bar_count": pl.col(A.BAR_COUNT),
                "regime": pl.col(A.REGIME),
                "gap_start_minute_of_day": _minute_of_day(pl.col(_PREV_LOCAL)),
            },
        )


def _break_overlap(
    prev_local: pl.Expr, cur_local: pl.Expr, break_start: pl.Expr, break_end: pl.Expr
) -> pl.Expr:
    """Minutes of `[prev_local, cur_local]` that fall inside the maintenance window.

    A gap inside one session spans at most two calendar days, so there are two cases:
    both endpoints on the same day (one overlap interval), or the gap crossing midnight
    (the tail of the first day's break plus the head of the second day's).
    """
    prev_mod = _minute_of_day(prev_local)
    cur_mod = _minute_of_day(cur_local)
    same_day = prev_local.dt.date() == cur_local.dt.date()

    overlap_same = (
        pl.min_horizontal(cur_mod, break_end) - pl.max_horizontal(prev_mod, break_start)
    ).clip(lower_bound=0)
    overlap_cross = (break_end - pl.max_horizontal(prev_mod, break_start)).clip(lower_bound=0) + (
        pl.min_horizontal(cur_mod, break_end) - break_start
    ).clip(lower_bound=0)

    return (
        pl.when(break_start.is_null() | break_end.is_null())
        .then(pl.lit(0, pl.Int64))
        .when(same_day)
        .then(overlap_same)
        .otherwise(overlap_cross)
        .cast(pl.Int64)
    )
