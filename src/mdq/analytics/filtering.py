"""Selecting a slice of bars: by contract, and by session date.

Dates filter on `session_date`, not on the calendar date of `ts_utc`. Asking for
"2026-03-05" means the session that settles on the 5th — which opened at 17:00 on the
4th — because that is the day a trader means. Both ends are inclusive, for the same
reason: nobody asks for a range they expect to be half-open.

The one judgement call worth naming: an unknown contract yields an **empty frame**,
not an error. This layer does not know which contracts exist in the dataset, only
which appear in the frame it was handed; a frame filtered to nothing is a perfectly
valid answer. Deciding that "no such contract" deserves a 404 needs the dataset's
contract list, which only the API has.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import polars as pl

from mdq.domain.schema import BarFrame, C

__all__ = ["InvalidRangeError", "filter_bars"]


class InvalidRangeError(ValueError):
    """Raised when `start` is after `end` — a request that can never match (422)."""


def filter_bars(
    bars: BarFrame,
    contracts: Iterable[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> BarFrame:
    """Restrict `bars` to some contracts and an inclusive session-date range.

    Args:
        bars: The frame to filter.
        contracts: Contract codes to keep. `None` keeps every contract; an **empty**
            collection keeps none, because an explicit empty selection is a request
            for nothing rather than a request for everything.
        start: Earliest `session_date` to keep, inclusive. `None` is unbounded.
        end: Latest `session_date` to keep, inclusive. `None` is unbounded.

    Returns:
        A `BarFrame` with the same frequency and provenance. Filtering to nothing
        returns an empty frame that still conforms to `BAR_SCHEMA`.

    Raises:
        InvalidRangeError: if `start` is later than `end`.
    """
    if start is not None and end is not None and start > end:
        raise InvalidRangeError(f"start {start.isoformat()} is after end {end.isoformat()}")

    lf = bars.lf
    if contracts is not None:
        # dict.fromkeys de-duplicates while preserving the caller's order, which keeps
        # the predicate stable (and therefore query plans comparable) across calls.
        wanted = list(dict.fromkeys(contracts))
        lf = lf.filter(pl.col(C.CONTRACT).is_in(wanted))
    if start is not None:
        lf = lf.filter(pl.col(C.SESSION_DATE) >= start)
    if end is not None:
        lf = lf.filter(pl.col(C.SESSION_DATE) <= end)
    return bars.with_lf(lf)
