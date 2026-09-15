"""The quality engine against the vendor's own published diagnostics.

Every other quality test asserts against expectations *we* wrote. These assert against
numbers the vendor published in `files.parquet` before we existed, so they can catch a
check that is confidently, self-consistently wrong. Recomputing all four diagnostics
across the full corpus gives 0 mismatches on 40/40 daily files (PLAN §0.1); the two
committed daily slices are complete vendor files, so the same equality must hold here.

The minute slice is two liquid weeks carved out of a much larger file, so its
`no_range_bar_count` is a subset of the vendor's — only the *invariant* counts
(duplicates, nulls) can be compared for it, and they are.

Two of these numbers are zero and stay zero, and that is worth stating plainly rather
than disguising: the sample really does contain no duplicates, no nulls, no non-positive
prices and no negative volume across all 5.3M minute and 30k daily rows. Those checks are
exercised by `tests/support/faults.py`, not by real data.
"""

from __future__ import annotations

import json

import polars as pl
import pytest

from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.ingest import ingest_file
from mdq.quality import CheckContext, run_checks
from support.fixtures import CLG26_DAILY, ESH26_DAILY, ESH26_MINUTE, vendor_diagnostics

pytestmark = pytest.mark.golden

#: check id -> the vendor column it must reproduce.
VENDOR_COLUMN = {
    "invalid_ohlc": "invalid_ohlc_row_count",
    "stale_bar": "no_range_bar_count",
    "duplicate_timestamp": "duplicate_timestamp_count",
    "missing_value": "null_price_row_count",
}

#: Complete vendor files, so every count is directly comparable.
COMPLETE_DAILY = [("ESH26", ESH26_DAILY), ("CLG26", CLG26_DAILY)]


def _rows_by_check(path, frequency: Frequency) -> dict[str, int]:
    """Run the four vendor-comparable checks and total the rows each implicates."""
    result = ingest_file(path)
    assert result.bars.frequency is frequency
    ctx = CheckContext.build(result.bars, rejects=result.rejects)
    report = run_checks(result.bars, ctx, checks=list(VENDOR_COLUMN))
    totals = dict.fromkeys(VENDOR_COLUMN, 0)
    for row in report.summary().to_dicts():
        totals[row[C.CHECK_ID]] += int(row["rows"])
    return totals


def _vendor_row(contract: str, frequency: str) -> dict[str, int]:
    frame = vendor_diagnostics(frequency).filter(pl.col("contract_symbol") == contract)
    assert frame.height == 1, f"no vendor diagnostics for {contract} {frequency}"
    return frame.row(0, named=True)


@pytest.mark.parametrize(("contract", "path"), COMPLETE_DAILY, ids=[c for c, _ in COMPLETE_DAILY])
def test_the_engine_reproduces_every_vendor_diagnostic_on_a_complete_daily_file(
    contract: str, path
) -> None:
    actual = _rows_by_check(path, Frequency.DAILY)
    vendor = _vendor_row(contract, "daily")
    assert {check: actual[check] for check in VENDOR_COLUMN} == {
        check: vendor[column] for check, column in VENDOR_COLUMN.items()
    }


def test_the_minute_slice_reproduces_the_counts_a_slice_can_reproduce() -> None:
    """Duplicates and nulls are invariant under slicing; no-range bars are not."""
    actual = _rows_by_check(ESH26_MINUTE, Frequency.MINUTE)
    vendor = _vendor_row("ESH26", "minute")
    assert actual["duplicate_timestamp"] == vendor["duplicate_timestamp_count"] == 0
    assert actual["missing_value"] == vendor["null_price_row_count"] == 0
    assert actual["invalid_ohlc"] == vendor["invalid_ohlc_row_count"] == 0
    assert 0 < actual["stale_bar"] <= vendor["no_range_bar_count"]


def test_the_real_ohlc_violations_carry_the_settlement_signature() -> None:
    """CLG26's six violations are the corpus pattern in miniature: OHL carried forward on
    a non-trading day while `close` moved to the new settlement, leaving the settlement
    outside its own bar's range. Every one is zero-volume and therefore DORMANT, so the
    engine downgrades them to WARNING — the difference between a report a desk reads and
    43 false alarms. The evidence is what PLAN §5 rule 2 consumes."""
    result = ingest_file(CLG26_DAILY)
    ctx = CheckContext.build(result.bars, rejects=result.rejects)
    report = run_checks(result.bars, ctx, checks=["invalid_ohlc"])

    findings = report.to_dicts()
    assert len(findings) == 6
    assert {f[C.SEVERITY] for f in findings} == {Severity.WARNING.label}
    # Both axes of the feed-incident insight are populated on every finding.
    assert all(f[C.CONTRACT] == "CLG26" for f in findings)
    assert all(f[C.SESSION_DATE] is not None for f in findings)

    evidence = [json.loads(f[C.EVIDENCE]) for f in findings]
    assert sum(e["carried_forward_settlement"] for e in evidence) == 6
    assert sum(e["zero_volume"] for e in evidence) == 6
    # `high < low` never occurs in the real corpus — 0 of 43 — so it needs a fixture.
    assert sum(e["high_lt_low"] for e in evidence) == 0


def test_the_real_sample_exhibits_none_of_the_crude_defects() -> None:
    """Stated rather than disguised: the crude checks the spec asks for find nothing here
    because the sample is pre-normalised. They are exercised by the fault harness."""
    result = ingest_file(ESH26_DAILY)
    ctx = CheckContext.build(result.bars, rejects=result.rejects)
    report = run_checks(
        result.bars,
        ctx,
        checks=[
            "missing_value",
            "duplicate_timestamp",
            "non_positive_price",
            "negative_volume",
            "malformed_record",
            "timestamp_out_of_range",
        ],
    )
    assert report.is_empty(), report.to_dicts()
    assert result.rejects.height == 0


def test_dormancy_explains_the_staleness_on_real_data() -> None:
    """81% of CLG26's daily bars are flat. Every one of its 1,629 no-range bars is downgraded,
    which is exactly the insight PLAN §5 rule 1 turns into a cleansing rule — and the
    reason this tool is usable on this dataset at all."""
    result = ingest_file(CLG26_DAILY)
    ctx = CheckContext.build(result.bars)
    report = run_checks(result.bars, ctx, checks=["stale_bar"])
    assert report.counts_by_severity()["WARNING"] == 0
    assert sum(int(r["rows"]) for r in report.summary().to_dicts()) == 1_629
