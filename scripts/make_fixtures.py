#!/usr/bin/env python
"""Carve the committed test fixtures out of `data/raw`.

    uv run python scripts/make_fixtures.py            # everything
    uv run python scripts/make_fixtures.py --csv-only  # no data/raw needed

Two kinds of fixture, for two kinds of test:

* **Real slices** (parquet) — small windows of the vendor's own files, plus the four
  per-file diagnostic counts the vendor publishes in `files.parquet`. These make the
  golden tests possible: our numbers have to reproduce the vendor's.
* **Hand-authored CSVs** — the sample is pre-normalised (0 duplicates, 0 nulls, 0
  non-positive prices), so every crude defect the exercise asks about has to be
  authored deliberately. They are written from this script rather than typed into the
  repository so that the defects, and the row each one sits on, are *declared* in one
  place and stay in step with the tests that assert them.

Everything is deterministic: re-running produces byte-identical files. The whole set is
kept under 400 KB so it can live in git without apology.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "raw"
DEFAULT_OUT_DIR = REPO_ROOT / "tests" / "fixtures"

#: PLAN §6 — the whole fixture set must stay small enough to commit without apology.
SIZE_BUDGET_BYTES = 400 * 1024

#: Two liquid weeks of ESH26 spanning the 2026-03-08 spring-forward weekend.
MINUTE_SLICE_START = date(2026, 3, 2)
MINUTE_SLICE_END = date(2026, 3, 13)
MINUTE_SLICE_NAME = "ESH26_minute_2026-03-02_to_03-13.parquet"

#: `ESH26` is clean; `CLG26` is dormant-heavy and carries real OHLC violations.
DAILY_SLICES = (
    ("ESH26_daily.parquet", "data/daily/CME/ES/ESH26.parquet"),
    ("CLG26_daily.parquet", "data/daily/NYMEX/CL/CLG26.parquet"),
)
MINUTE_SOURCE = "data/minute/CME/ES/ESH26.parquet"

#: The vendor's own diagnostics, kept for every file so corpus-wide totals survive.
VENDOR_DIAGNOSTIC_COLUMNS = (
    "exchange",
    "root",
    "contract_symbol",
    "frequency",
    "path",
    "row_count",
    "duplicate_timestamp_count",
    "null_price_row_count",
    "invalid_ohlc_row_count",
    "no_range_bar_count",
)

CSV_HEADER = "contract,exchange,timestamp,open,high,low,close,volume"
CSV_CONTRACT = "ESH26"
CSV_EXCHANGE = "CME"
#: 09:00 CT on an ordinary Tuesday: no DST edge, session date == calendar date.
CSV_START = datetime(2026, 3, 3, 9, 0)


@dataclass(frozen=True)
class Defect:
    """One deliberate flaw, pinned to the row id the tests assert against."""

    row_id: int
    note: str


#: The malformed-CSV contract, in one place. `tests/unit/test_normalise.py` asserts
#: exactly these row ids, so changing a number here is a visible, deliberate act.
MALFORMED_DEFECTS = (
    Defect(5, "text in a price -> open is null, row is KEPT and flagged later"),
    Defect(9, "impossible date -> UNPARSEABLE_TIMESTAMP reject"),
    Defect(13, "blank contract -> NULL_CONTRACT reject"),
    Defect(17, "thousands separator -> close is null, row is KEPT (never read as 6123.5)"),
    Defect(21, "ragged line (one field too many) -> MALFORMED_LINE reject"),
)
MALFORMED_ROWS = 25


def _price_at(index: int) -> float:
    """A deterministic, gently drifting price."""
    return round(6100.0 + index * 0.25, 2)


def _bar(index: int) -> tuple[float, float, float, float, int]:
    """One coherent OHLCV bar: `low <= min(o, c) <= max(o, c) <= high`."""
    open_ = _price_at(index)
    close = round(open_ + 0.5, 2)
    return open_, round(close + 0.75, 2), round(open_ - 0.75, 2), close, 100 + index


def build_clean_csv() -> str:
    """Six impeccable bars across two contracts, deliberately interleaved.

    Two contracts in one file is the ordinary case, and it is what proves the output
    really is sorted by `(contract, ts_utc, row_id)` rather than left in file order.
    """
    lines = [CSV_HEADER]
    for index in range(6):
        contract = CSV_CONTRACT if index % 2 == 0 else "CLG26"
        exchange = CSV_EXCHANGE if index % 2 == 0 else "NYMEX"
        stamp = CSV_START + timedelta(minutes=index // 2)
        open_, high, low, close, volume = _bar(index)
        lines.append(
            f"{contract},{exchange},{stamp:%Y-%m-%d %H:%M:%S},{open_},{high},{low},{close},{volume}"
        )
    return "\n".join(lines) + "\n"


def build_malformed_csv() -> str:
    """`MALFORMED_ROWS` bars carrying exactly the defects in `MALFORMED_DEFECTS`.

    The good rows are not padding: they keep the malformed-line share under
    `max_reject_ratio`, so the file exercises the *reporting* path rather than the
    "this is not a CSV at all" abort — which has its own test.
    """
    lines = [CSV_HEADER]
    for index in range(MALFORMED_ROWS):
        stamp = f"{CSV_START + timedelta(minutes=index):%Y-%m-%d %H:%M:%S}"
        open_, high, low, close, volume = _bar(index)
        contract = CSV_CONTRACT
        fields = [
            contract,
            CSV_EXCHANGE,
            stamp,
            f"{open_}",
            f"{high}",
            f"{low}",
            f"{close}",
            f"{volume}",
        ]
        if index == 5:
            fields[3] = "n/a"
        elif index == 9:
            fields[2] = "2026-02-30 09:09:00"
        elif index == 13:
            fields[0] = ""
        elif index == 17:
            fields[6] = '"6,123.50"'
        elif index == 21:
            fields.append("unexpected")
        lines.append(",".join(fields))
    return "\n".join(lines) + "\n"


def build_utc_iso_csv() -> str:
    """The same bars, but as UTC ISO-8601 under mixed-case alias headers.

    Proves two things at once: aliases are matched case-insensitively, and a source
    whose instants are genuinely UTC is *declared* as such rather than guessed.
    """
    lines = ["Symbol,TS,O,H,L,C,Vol"]
    for index in range(4):
        # 09:00 CT on 2026-03-03 is 15:00 UTC (CST, UTC-6).
        stamp = datetime(2026, 3, 3, 15, 0) + timedelta(minutes=index)
        open_, high, low, close, volume = _bar(index)
        lines.append(
            f"{CSV_CONTRACT},{stamp:%Y-%m-%dT%H:%M:%SZ},{open_},{high},{low},{close},{volume}"
        )
    return "\n".join(lines) + "\n"


CSV_FIXTURES = {
    "generic_clean.csv": build_clean_csv,
    "generic_malformed.csv": build_malformed_csv,
    "generic_utc_iso.csv": build_utc_iso_csv,
}


def write_csv_fixtures(out_dir: Path) -> list[Path]:
    """Write the hand-authored CSVs."""
    written: list[Path] = []
    for name, build in CSV_FIXTURES.items():
        path = out_dir / name
        path.write_text(build(), encoding="utf-8")
        written.append(path)
    return written


def write_real_slices(data_dir: Path, out_dir: Path) -> list[Path]:
    """Carve the real parquet slices and the vendor diagnostics table.

    Raises:
        FileNotFoundError: if `data_dir` has not been populated by `fetch_data.py`.
    """
    written: list[Path] = []
    minute_source = data_dir / MINUTE_SOURCE
    _require(minute_source)
    minute = (
        pl.scan_parquet(minute_source)
        .filter(pl.col("trading_date").is_between(MINUTE_SLICE_START, MINUTE_SLICE_END))
        .collect()
    )
    written.append(_write_parquet(minute, out_dir / MINUTE_SLICE_NAME))

    for name, relative in DAILY_SLICES:
        source = data_dir / relative
        _require(source)
        written.append(_write_parquet(pl.read_parquet(source), out_dir / name))

    files = data_dir / "files.parquet"
    _require(files)
    diagnostics = (
        pl.read_parquet(files)
        .select(VENDOR_DIAGNOSTIC_COLUMNS)
        .sort("frequency", "exchange", "root", "contract_symbol")
    )
    path = out_dir / "vendor_diagnostics.csv"
    diagnostics.write_csv(path)
    written.append(path)
    return written


def _require(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing; run `uv run python scripts/fetch_data.py --all` first, "
            "or pass --csv-only to regenerate only the hand-authored fixtures"
        )


def _display(path: Path) -> str:
    """A path relative to the repository when it is inside it, absolute otherwise."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _write_parquet(frame: pl.DataFrame, path: Path) -> Path:
    """Write a slice as small as parquet will make it, deterministically."""
    frame.write_parquet(path, compression="zstd", compression_level=9, statistics=True)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Regenerate the fixture set. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--csv-only",
        action="store_true",
        help="regenerate only the hand-authored CSVs (no data/raw required)",
    )
    args = parser.parse_args(argv)

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    written = write_csv_fixtures(out_dir)
    if not args.csv_only:
        written += write_real_slices(args.data_dir, out_dir)

    total = 0
    for path in sorted(written):
        size = path.stat().st_size
        total += size
        print(f"{size:>9,} B  {_display(path)}")
    share = total / SIZE_BUDGET_BYTES
    print(f"{total:>9,} B  total ({share:.0%} of the {SIZE_BUDGET_BYTES:,} B budget)")
    if total > SIZE_BUDGET_BYTES:
        print("fixture set exceeds the size budget", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
