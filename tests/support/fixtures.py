"""Locations of, and accessors for, the committed test fixtures.

`tests/fixtures` holds two very different things and it matters which is which:

* real slices of the vendor's files, plus the vendor's own diagnostic counts — the
  basis of the golden tests;
* hand-authored CSVs carrying deliberate defects, because the real sample has none.

Nothing here touches the network or `data/raw`; everything it points at is committed.
Regenerate the whole set with `uv run python scripts/make_fixtures.py`.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

__all__ = [
    "CLG26_DAILY",
    "ESH26_DAILY",
    "ESH26_MINUTE",
    "FIXTURES_DIR",
    "GENERIC_CLEAN",
    "GENERIC_MALFORMED",
    "GENERIC_UTC_ISO",
    "VENDOR_DIAGNOSTICS",
    "vendor_diagnostics",
]

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

#: Two liquid weeks of ESH26 minute bars spanning the 2026-03-08 spring forward.
ESH26_MINUTE = FIXTURES_DIR / "ESH26_minute_2026-03-02_to_03-13.parquet"
#: Complete vendor daily files, so their row counts match `files.parquet` exactly.
ESH26_DAILY = FIXTURES_DIR / "ESH26_daily.parquet"
CLG26_DAILY = FIXTURES_DIR / "CLG26_daily.parquet"
#: The vendor's four per-file diagnostic counts, for all 80 bar files.
VENDOR_DIAGNOSTICS = FIXTURES_DIR / "vendor_diagnostics.csv"

GENERIC_CLEAN = FIXTURES_DIR / "generic_clean.csv"
GENERIC_MALFORMED = FIXTURES_DIR / "generic_malformed.csv"
GENERIC_UTC_ISO = FIXTURES_DIR / "generic_utc_iso.csv"


def vendor_diagnostics(frequency: str | None = None) -> pl.DataFrame:
    """The vendor's published diagnostics, optionally for one frequency.

    Args:
        frequency: `"daily"` or `"minute"`; `None` for every file.
    """
    frame = pl.read_csv(VENDOR_DIAGNOSTICS)
    if frequency is not None:
        frame = frame.filter(pl.col("frequency") == frequency)
    return frame
