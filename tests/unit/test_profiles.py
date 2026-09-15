"""Source profiles.

The profiles are a *declaration*, so these tests read like an inspection of that
declaration: given a vendor's column list, which profile claims it, which column does
it take the instant from, and — the one that actually matters — what does it say those
values *mean*. Mislabelling `timestamp_ms` as a UTC epoch would shift every minute bar
by five or six hours, and nothing downstream could tell.
"""

from __future__ import annotations

import pytest

from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.ingest.profiles import (
    GENERIC,
    HF_DAILY,
    HF_MINUTE,
    TIMESTAMP,
    detect_profile,
    profile_by_name,
)

pytestmark = pytest.mark.unit

#: Exactly the vendor's column lists, as verified against the real files.
HF_MINUTE_COLUMNS = [
    "root_id",
    "exchange",
    "root",
    "contract_symbol",
    "timestamp_ms",
    "timestamp_chicago_wall",
    "trading_date",
    "minute_of_day",
    "open",
    "high",
    "low",
    "close",
    "volume",
]
HF_DAILY_COLUMNS = [
    "root_id",
    "exchange",
    "root",
    "contract_symbol",
    "timestamp_ms",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
]


def test_the_minute_layout_is_recognised_and_the_daily_layout_is_not_mistaken_for_it() -> None:
    assert detect_profile(HF_MINUTE_COLUMNS) is HF_MINUTE
    # Both layouts carry `timestamp_ms`; `minute_of_day` is what tells them apart.
    assert not HF_MINUTE.detect(HF_DAILY_COLUMNS)
    assert detect_profile(HF_DAILY_COLUMNS) is HF_DAILY


def test_minute_timestamps_are_declared_as_chicago_wall_clock_not_utc() -> None:
    chosen = HF_MINUTE.resolve_timestamp(HF_MINUTE_COLUMNS)
    assert chosen is not None
    assert chosen.column == "timestamp_chicago_wall"
    assert chosen.kind == "chicago_wall_naive"
    assert HF_MINUTE.timestamp_kind == "chicago_wall_naive"


def test_the_minute_fallback_decodes_timestamp_ms_as_wall_clock_not_an_epoch() -> None:
    without = [c for c in HF_MINUTE_COLUMNS if c != "timestamp_chicago_wall"]
    chosen = HF_MINUTE.resolve_timestamp(without)
    assert chosen is not None
    assert (chosen.column, chosen.kind) == ("timestamp_ms", "epoch_ms_chicago_wall")


def test_the_daily_instant_is_a_date_label() -> None:
    chosen = HF_DAILY.resolve_timestamp(HF_DAILY_COLUMNS)
    assert chosen is not None
    assert (chosen.column, chosen.kind) == ("date", "date")


@pytest.mark.parametrize(
    ("profile", "frequency"),
    [(HF_MINUTE, Frequency.MINUTE), (HF_DAILY, Frequency.DAILY)],
)
def test_vendor_profiles_declare_their_frequency_so_it_is_never_inferred(
    profile: object, frequency: Frequency
) -> None:
    assert getattr(profile, "frequency_hint") is frequency  # noqa: B009
    assert GENERIC.frequency_hint is None


def test_the_vendor_columns_map_onto_the_canonical_names() -> None:
    resolved = HF_MINUTE.resolve(HF_MINUTE_COLUMNS)
    assert resolved[C.CONTRACT] == "contract_symbol"
    assert resolved[C.EXCHANGE] == "exchange"
    assert resolved[C.CLOSE] == "close"
    assert resolved[TIMESTAMP] == "timestamp_chicago_wall"
    # Minute bars have no open interest; the canonical column stays unmapped.
    assert C.OPEN_INTEREST not in resolved


# --------------------------------------------------------------------------- #
# generic
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "columns",
    [
        ["contract", "timestamp", "open", "high", "low", "close", "volume"],
        ["Symbol", "TS", "O", "H", "L", "C", "Vol"],
        ["TICKER", "DateTime", "last", "v"],
    ],
)
def test_generic_matches_hand_made_headers_whatever_their_case(columns: list[str]) -> None:
    assert detect_profile(columns) is GENERIC


def test_generic_resolves_aliases_to_the_canonical_names() -> None:
    resolved = GENERIC.resolve(["Symbol", "TS", "O", "H", "L", "C", "Vol"])
    assert resolved[C.CONTRACT] == "Symbol"
    assert resolved[TIMESTAMP] == "TS"
    assert resolved[C.OPEN] == "O"
    assert resolved[C.VOLUME] == "Vol"


def test_generic_prefers_the_most_explicit_alias_when_several_are_present() -> None:
    resolved = GENERIC.resolve(["symbol", "contract", "timestamp", "date", "close"])
    assert resolved[C.CONTRACT] == "contract"
    assert resolved[TIMESTAMP] == "timestamp"


def test_only_the_columns_that_locate_a_bar_are_required() -> None:
    # No prices at all: still ingestible. A row with no price is locatable, and a
    # locatable row is kept and flagged rather than thrown away.
    assert GENERIC.detect(["contract", "timestamp"])
    assert GENERIC.missing(["contract", "timestamp"]) == []


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        (["timestamp", "close"], [C.CONTRACT]),
        (["contract", "close"], [TIMESTAMP]),
        (["close"], [C.CONTRACT, TIMESTAMP]),
    ],
)
def test_a_file_that_cannot_locate_its_bars_reports_exactly_what_is_missing(
    columns: list[str], expected: list[str]
) -> None:
    assert GENERIC.missing(columns) == sorted(expected)


def test_an_unrecognisable_file_falls_back_to_generic_so_it_can_report_why() -> None:
    assert detect_profile(["alpha", "beta"]) is GENERIC


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["hf_minute", "hf_daily", "generic"])
def test_every_shipped_profile_is_reachable_by_name(name: str) -> None:
    assert profile_by_name(name).name == name


def test_an_unknown_profile_name_lists_the_ones_that_exist() -> None:
    with pytest.raises(KeyError, match="hf_minute"):
        profile_by_name("nope")
