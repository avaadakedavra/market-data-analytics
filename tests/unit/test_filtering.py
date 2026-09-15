"""Slicing bars: inclusive session dates, and who gets to say "404"."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from mdq.analytics.filtering import InvalidRangeError, filter_bars
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, BarFrame, C
from support.handmade import Bar, bar_frame

pytestmark = pytest.mark.unit


@pytest.fixture
def two_contracts() -> BarFrame:
    """Two contracts across three sessions, one bar each."""
    return bar_frame(
        [
            Bar(datetime(2026, 3, 2, 17, 0), contract=contract)  # session 2026-03-03
            for contract in ("ESH26", "NQH26")
        ]
        + [
            Bar(datetime(2026, 3, 3, 17, 0), contract=contract)  # session 2026-03-04
            for contract in ("ESH26", "NQH26")
        ]
        + [
            Bar(datetime(2026, 3, 4, 17, 0), contract=contract)  # session 2026-03-05
            for contract in ("ESH26", "NQH26")
        ]
    )


def _sessions(bars: BarFrame) -> list[date]:
    return bars.collect().sort(C.SESSION_DATE)[C.SESSION_DATE].unique(maintain_order=True).to_list()


# --- contracts ---------------------------------------------------------------- #


def test_no_filters_returns_everything(two_contracts: BarFrame) -> None:
    assert filter_bars(two_contracts).collect().equals(two_contracts.collect())


def test_filtering_to_one_contract(two_contracts: BarFrame) -> None:
    result = filter_bars(two_contracts, contracts=["ESH26"])

    assert result.contracts() == ["ESH26"]
    assert result.collect().height == 3


def test_repeated_contracts_do_not_duplicate_rows(two_contracts: BarFrame) -> None:
    result = filter_bars(two_contracts, contracts=["ESH26", "ESH26", "NQH26"])
    assert result.collect().height == 6


def test_an_unknown_contract_is_an_empty_frame_not_an_error(two_contracts: BarFrame) -> None:
    """This layer cannot know the dataset's contract list, so it cannot say 404."""
    result = filter_bars(two_contracts, contracts=["ZZZ99"])

    assert result.is_empty()
    assert result.collect().schema == BAR_SCHEMA


def test_an_explicitly_empty_selection_selects_nothing(two_contracts: BarFrame) -> None:
    """`[]` is a request for no contracts; `None` is a request for all of them."""
    assert filter_bars(two_contracts, contracts=[]).is_empty()
    assert not filter_bars(two_contracts, contracts=None).is_empty()


def test_contract_matching_is_exact(two_contracts: BarFrame) -> None:
    assert filter_bars(two_contracts, contracts=["esh26"]).is_empty()


def test_any_iterable_of_contracts_is_accepted(two_contracts: BarFrame) -> None:
    result = filter_bars(two_contracts, contracts=(c for c in ("ESH26",)))
    assert result.contracts() == ["ESH26"]


# --- dates --------------------------------------------------------------------- #


def test_dates_filter_the_session_not_the_calendar_day(two_contracts: BarFrame) -> None:
    """A bar stamped 2026-03-02 17:00 CT belongs to the session that settles 03-03."""
    kept = filter_bars(two_contracts, start=date(2026, 3, 3), end=date(2026, 3, 3))
    frame = kept.collect()

    assert frame.height == 2
    assert frame[C.SESSION_DATE].unique().to_list() == [date(2026, 3, 3)]
    assert frame[C.TS_LOCAL][0].date() == date(2026, 3, 2)


def test_both_ends_of_the_range_are_inclusive(two_contracts: BarFrame) -> None:
    kept = filter_bars(two_contracts, start=date(2026, 3, 3), end=date(2026, 3, 5))
    assert _sessions(kept) == [date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)]


def test_a_single_day_range_is_allowed(two_contracts: BarFrame) -> None:
    kept = filter_bars(two_contracts, start=date(2026, 3, 4), end=date(2026, 3, 4))
    assert _sessions(kept) == [date(2026, 3, 4)]


def test_each_bound_works_on_its_own(two_contracts: BarFrame) -> None:
    assert _sessions(filter_bars(two_contracts, start=date(2026, 3, 4))) == [
        date(2026, 3, 4),
        date(2026, 3, 5),
    ]
    assert _sessions(filter_bars(two_contracts, end=date(2026, 3, 3))) == [date(2026, 3, 3)]


def test_a_range_that_matches_nothing_is_empty_not_an_error(two_contracts: BarFrame) -> None:
    result = filter_bars(two_contracts, start=date(2030, 1, 1), end=date(2030, 1, 2))

    assert result.is_empty()
    assert result.collect().schema == BAR_SCHEMA


def test_a_backwards_range_is_refused(two_contracts: BarFrame) -> None:
    with pytest.raises(InvalidRangeError, match="2026-03-05 is after end 2026-03-03"):
        filter_bars(two_contracts, start=date(2026, 3, 5), end=date(2026, 3, 3))


def test_a_backwards_range_is_refused_before_any_work_is_done() -> None:
    """The error is about the request, so it does not depend on the data."""
    with pytest.raises(InvalidRangeError):
        filter_bars(BarFrame.empty(Frequency.MINUTE), start=date(2026, 1, 2), end=date(2026, 1, 1))


# --- the carrier ---------------------------------------------------------------- #


def test_filters_combine(two_contracts: BarFrame) -> None:
    result = filter_bars(
        two_contracts,
        contracts=["NQH26"],
        start=date(2026, 3, 4),
        end=date(2026, 3, 5),
    )
    frame = result.collect()

    assert frame.height == 2
    assert frame[C.CONTRACT].unique().to_list() == ["NQH26"]


def test_frequency_and_provenance_survive_the_filter(two_contracts: BarFrame) -> None:
    result = filter_bars(two_contracts, contracts=["ESH26"])

    assert result.frequency is two_contracts.frequency
    assert result.source == two_contracts.source


def test_filtering_an_empty_frame_stays_empty() -> None:
    result = filter_bars(
        BarFrame.empty(Frequency.DAILY),
        contracts=["ESH26"],
        start=date(2026, 1, 1),
        end=date(2026, 12, 31),
    )

    assert result.is_empty()
    assert result.frequency is Frequency.DAILY
