"""The API against numbers nobody on this project chose.

Two independent sources of truth, both reached through HTTP rather than through the
internals, so they cover the whole stack the way a user does:

* **the vendor's own published diagnostics.** `files.parquet` records four per-file counts
  that were computed before this codebase existed. `GET /quality/summary` must reproduce
  them. Recomputing all four across the full 40-file corpus gives 0 mismatches on 40/40
  (PLAN §0.1); the two committed daily slices are *complete* vendor files, so the equality
  has to hold exactly here too.
* **the vendor's own daily bars.** Aggregating ESH26 minute bars with a 17:00 CT session
  roll must reproduce the vendor's daily open, high and low.

The second one is scoped, deliberately and narrowly, and the scoping is the finding. The
roll reproduces the vendor's `open` on 193 of 197 days against 36 for calendar-date
grouping, so it is unambiguously right — but O/H/L do **not** match everywhere, because a
thin session's true high simply is not in the minute file (corpus median 222 bars against
1,380 possible). So the equality is asserted on the verified liquid sessions of PLAN §0.1
and nowhere else, and `close` is never compared at all: the vendor's daily close is a
settlement price and agrees with the last traded price about 1% of the time.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from support.api import PARQUET_FIXTURES, client_for, fixture_dir, service_for
from support.fixtures import vendor_diagnostics

pytestmark = pytest.mark.golden

#: check id → the vendor column it must reproduce (PLAN §4.3).
VENDOR_COLUMN = {
    "invalid_ohlc": "invalid_ohlc_row_count",
    "stale_bar": "no_range_bar_count",
    "duplicate_timestamp": "duplicate_timestamp_count",
    "missing_value": "null_price_row_count",
}

#: Liquid ESH26 sessions verified in PLAN §0.1. Full 1,380-bar (or 1,379) CME sessions,
#: entirely inside the committed minute slice.
LIQUID_SESSIONS = ("2026-03-04", "2026-03-05", "2026-03-10", "2026-03-11")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """A client over the committed vendor slices, and nothing hand-authored.

    The hand-made CSV fixtures also carry ESH26 minute bars, at invented prices. Loading
    them here would merge those prices into ESH26's real series and quietly destroy every
    assertion below.
    """
    return client_for(service_for(fixture_dir(tmp_path, *PARQUET_FIXTURES)))


def _vendor_totals() -> dict[str, int]:
    """The vendor's four counts, summed over the two committed daily files."""
    frame = vendor_diagnostics("daily").filter(pl.col("contract_symbol").is_in(["CLG26", "ESH26"]))
    assert frame.height == 2
    return {check: int(frame.get_column(column).sum()) for check, column in VENDOR_COLUMN.items()}


def test_the_fixtures_still_carry_the_counts_these_tests_are_about() -> None:
    """A guard on everything below: a regenerated fixture would make it all vacuous."""
    assert _vendor_totals() == {
        "invalid_ohlc": 6,
        "stale_bar": 2_026,
        "duplicate_timestamp": 0,
        "missing_value": 0,
    }


def test_the_quality_summary_reproduces_the_vendors_published_diagnostics(
    client: TestClient,
) -> None:
    """The headline golden test: our checks agree with numbers the vendor published."""
    body = client.get("/quality/summary?frequency=daily").json()
    reported = {item["check_id"]: item["bars_affected"] for item in body["checks"]}
    actual = {check: reported.get(check, 0) for check in VENDOR_COLUMN}
    assert actual == _vendor_totals()


def test_the_vendors_ohlc_violations_are_attributed_to_the_right_contract(
    client: TestClient,
) -> None:
    """All six violations are CLG26's; ESH26 has none. A check that fired on both would
    be describing nothing."""
    body = client.get("/quality/findings?check=invalid_ohlc&limit=50000").json()
    contracts = {finding["contract"] for finding in body["findings"]}
    assert contracts == {"CLG26"}
    assert body["row_count"] == 6


def _sessions(client: TestClient, path: str, key: str = "rows") -> dict[str, dict[str, float]]:
    body = client.get(path).json()
    return {row["session_date"]: row for row in body[key]}


def test_session_aggregation_reproduces_the_vendors_daily_open_high_and_low(
    client: TestClient,
) -> None:
    """`daily_bars` over minute bars, against the vendor's own daily file, through HTTP."""
    derived = _sessions(client, "/analytics/daily_bars?frequency=minute&contract=ESH26&limit=100")
    vendor = _sessions(
        client, "/bars?frequency=daily&contract=ESH26&start=2026-03-02&end=2026-03-13&limit=100"
    )

    for session in LIQUID_SESSIONS:
        ours, theirs = derived[session], vendor[session]
        assert ours["bar_count"] >= 1_379, "a liquid CME session is a full 23 hours of bars"
        assert (ours["open"], ours["high"], ours["low"]) == (
            theirs["open"],
            theirs["high"],
            theirs["low"],
        ), f"session {session} diverges from the vendor's daily bar"


def test_the_daily_close_is_a_settlement_price_and_is_never_asserted_on(
    client: TestClient,
) -> None:
    """Stated rather than quietly avoided: the vendor's close is not the last trade.

    If this ever starts matching, the golden test above is the one that needs revisiting —
    not this one.
    """
    derived = _sessions(client, "/analytics/daily_bars?frequency=minute&contract=ESH26&limit=100")
    vendor = _sessions(
        client, "/bars?frequency=daily&contract=ESH26&start=2026-03-02&end=2026-03-13&limit=100"
    )
    agreements = sum(
        1 for session in LIQUID_SESSIONS if derived[session]["close"] == vendor[session]["close"]
    )
    assert agreements == 0


def test_a_truncated_session_is_visibly_truncated_rather_than_silently_wrong(
    client: TestClient,
) -> None:
    """The slice starts mid-session on 2026-03-02, so its open cannot match — and
    `bar_count` is what says so. That coverage number is exactly why the golden assertion
    is scoped to liquid sessions instead of being claimed as a general property."""
    derived = _sessions(client, "/analytics/daily_bars?frequency=minute&contract=ESH26&limit=100")
    vendor = _sessions(
        client, "/bars?frequency=daily&contract=ESH26&start=2026-03-02&end=2026-03-13&limit=100"
    )
    first = derived["2026-03-02"]
    assert first["bar_count"] < 1_379
    assert first["open"] != vendor["2026-03-02"]["open"]
    # The high and low still agree, because the session's extremes fell inside the slice.
    assert (first["high"], first["low"]) == (
        vendor["2026-03-02"]["high"],
        vendor["2026-03-02"]["low"],
    )


def test_the_real_settlement_signature_reaches_the_insights_endpoint(
    client: TestClient,
) -> None:
    """The headline insight, end to end: six incoherent bars are one convention."""
    insights = {i["id"]: i for i in client.get("/insights?frequency=daily").json()["insights"]}
    carried = insights["carried_forward_settlement"]
    assert carried["affected_contracts"] == ["CLG26"]
    assert carried["finding_count"] == 6
    assert carried["rule"]["confidence"] == 1.0
    assert carried["rule"]["rule_id"] == "ohlc_bounds"
