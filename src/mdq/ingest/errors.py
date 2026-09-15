"""Errors ingestion raises rather than reports.

The distinction matters and is deliberate:

* A **reject** is a row the pipeline could not place on the canonical timeline. It is
  *data*: it lands in `IngestResult.rejects` with a row id and a reason, and a business
  user sees it in the report.
* An **error** is a statement about the *file*: we cannot honestly produce a
  `BarFrame` from it at all. Those are the exceptions below.

They live in their own module so readers, profiles and normalisation can all raise
them without importing one another.
"""

from __future__ import annotations

__all__ = [
    "IngestError",
    "MixedFrequencyError",
    "TooManyMalformedLinesError",
    "UnsupportedFormatError",
]


class IngestError(Exception):
    """Base class for every ingestion failure."""


class UnsupportedFormatError(IngestError):
    """No registered reader claims the file's extension."""


class MixedFrequencyError(IngestError):
    """One file carries both daily and intraday bars.

    `BarFrame` is homogeneous in frequency by construction (frequency is metadata, not
    a column), and splitting a mixed file is an explicit non-goal: a caller who knows
    better can declare the frequency through `IngestOptions.frequency`.
    """


class TooManyMalformedLinesError(IngestError):
    """More of the file is unparseable than `max_reject_ratio` allows.

    Reporting ten bad lines out of a million is useful; reporting a million out of a
    million means the file is not the format it claims to be, and silently returning a
    near-empty `BarFrame` would be the dishonest answer.
    """
