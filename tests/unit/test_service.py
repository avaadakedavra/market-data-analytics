"""`MarketDataService` — the decisions the facade makes on the caller's behalf.

The API and the dashboard are both thin over this object, so anything they appear to
decide is really decided here: which frequency a request means, what counts as a typo
rather than an empty answer, what an upload is allowed to be, and which files in a vendor
drop are data at all.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from mdq.analytics import FrequencyNotSupportedError, InvalidRangeError, UnknownAnalyticError
from mdq.domain.config import AppSettings
from mdq.domain.findings import Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import C
from mdq.quality import UnknownCheckError
from mdq.service import (
    AUTOLOAD_ID,
    FindingFilter,
    MarketDataService,
    UnknownContractError,
    UploadTooLargeError,
    View,
    check_title,
)
from support.api import PARQUET_FIXTURES, fixture_dir, service_for
from support.fixtures import GENERIC_MALFORMED

pytestmark = pytest.mark.unit


@pytest.fixture
def service(tmp_path: Path) -> MarketDataService:
    """A service over the three committed vendor slices and nothing else."""
    return service_for(fixture_dir(tmp_path, *PARQUET_FIXTURES))


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


def test_autoload_registers_the_files_without_reading_them(service: MarketDataService) -> None:
    """The startup budget: three files known, zero rows read."""
    assert service.store.ids() == [AUTOLOAD_ID]
    data = service.data
    assert data.frequencies() == (Frequency.DAILY, Frequency.MINUTE)
    assert not data.is_loaded(Frequency.DAILY)
    assert not data.is_loaded(Frequency.MINUTE)


def test_autoload_of_a_missing_directory_yields_a_working_empty_service(tmp_path: Path) -> None:
    """Starting the API before `make fetch` must not be a stack trace."""
    service = MarketDataService(AppSettings(data_dir=tmp_path / "absent"))
    assert service.autoload() is None
    assert service.frequencies() == ()
    assert service.default_frequency() is Frequency.DAILY
    assert service.contracts() == []
    assert service.insights() == []
    assert service.findings().height == 0
    assert service.quality_summary().total_findings == 0
    assert service.quality_summary().checks == ()
    assert service.bars().is_empty()


def test_loading_a_missing_path_is_an_error(tmp_path: Path) -> None:
    service = MarketDataService(AppSettings(data_dir=tmp_path))
    with pytest.raises(FileNotFoundError):
        service.load_directory(tmp_path / "nope")


def test_loading_a_single_file_is_allowed(tmp_path: Path) -> None:
    directory = fixture_dir(tmp_path, "ESH26_daily.parquet")
    service = MarketDataService(AppSettings(data_dir=tmp_path))
    report = service.load_directory(directory / "ESH26_daily.parquet")
    assert report.file_count == 1


def test_files_that_are_not_bar_data_are_skipped_with_a_reason(tmp_path: Path) -> None:
    """A vendor drop ships checksums and catalogues next to the bars."""
    directory = fixture_dir(tmp_path, "ESH26_daily.parquet", "vendor_diagnostics.csv")
    (directory / "checksums.sha256").write_text("not data\n")
    service = MarketDataService(AppSettings(data_dir=directory))
    report = service.autoload()

    assert report is not None
    assert report.file_count == 1
    skipped = {Path(item.path).name: item.reason for item in report.skipped}
    # The checksum file has no reader at all and never reaches the scanner.
    assert set(skipped) == {"vendor_diagnostics.csv"}
    assert "no contract or timestamp column" in skipped["vendor_diagnostics.csv"]


def test_a_file_that_cannot_be_read_does_not_abort_the_load(tmp_path: Path) -> None:
    """One corrupt file must never cost the user the other seventy-nine."""
    directory = fixture_dir(tmp_path, "ESH26_daily.parquet")
    (directory / "broken.parquet").write_bytes(b"this is not parquet")
    service = MarketDataService(AppSettings(data_dir=directory))
    report = service.autoload()

    assert report is not None
    assert report.file_count == 1
    assert [Path(item.path).name for item in report.skipped] == ["broken.parquet"]
    assert report.skipped[0].reason.startswith("ComputeError") or report.skipped[0].reason


def test_reloading_a_directory_replaces_rather_than_doubles(tmp_path: Path) -> None:
    directory = fixture_dir(tmp_path, "ESH26_daily.parquet")
    service = MarketDataService(AppSettings(data_dir=directory))
    service.autoload()
    before = service.summary().row_count
    service.autoload()
    assert service.summary().row_count == before


# --------------------------------------------------------------------------- #
# frequency resolution
# --------------------------------------------------------------------------- #


def test_the_default_frequency_is_daily_when_daily_is_loaded(service: MarketDataService) -> None:
    assert service.default_frequency() is Frequency.DAILY


def test_a_minute_only_dataset_defaults_to_minute(tmp_path: Path) -> None:
    """Never silently answer about a frequency nobody loaded."""
    service = service_for(fixture_dir(tmp_path, "ESH26_minute_2026-03-02_to_03-13.parquet"))
    assert service.default_frequency() is Frequency.MINUTE


def test_an_analytic_that_serves_one_frequency_answers_for_itself(
    service: MarketDataService,
) -> None:
    """`rolling_vwap` without a frequency can only have meant minute bars."""
    from mdq.analytics import REGISTRY

    assert service.frequency_for(REGISTRY.get("rolling_vwap")) is Frequency.MINUTE
    assert service.frequency_for(REGISTRY.get("daily_bars")) is Frequency.DAILY


# --------------------------------------------------------------------------- #
# selecting bars
# --------------------------------------------------------------------------- #


def test_an_unknown_contract_is_a_typo_not_an_empty_answer(service: MarketDataService) -> None:
    with pytest.raises(UnknownContractError, match="ESH27"):
        service.bars(contracts=["ESH26", "ESH27"])


def test_a_backwards_date_range_is_refused(service: MarketDataService) -> None:
    with pytest.raises(InvalidRangeError):
        service.bars(start=date(2026, 3, 5), end=date(2026, 3, 1))


def test_filtering_to_nothing_keeps_the_schema(service: MarketDataService) -> None:
    empty = service.bars(contracts=["ESH26"], start=date(1990, 1, 1), end=date(1990, 1, 2))
    assert empty.is_empty()
    assert empty.collect().columns[:3] == [C.ROW_ID, C.CONTRACT, C.EXCHANGE]


def test_the_clean_view_drops_what_the_raw_view_keeps(tmp_path: Path) -> None:
    """The only difference between the two views is the cleansing policy."""
    service = service_for(fixture_dir(tmp_path, "generic_malformed.csv"))
    raw = service.bars(Frequency.MINUTE).collect().height
    clean = service.bars(Frequency.MINUTE, view=View.CLEAN).collect().height
    assert clean < raw


def test_view_is_clean_only_for_the_clean_view() -> None:
    assert View.CLEAN.is_clean
    assert not View.RAW.is_clean


# --------------------------------------------------------------------------- #
# analytics
# --------------------------------------------------------------------------- #


def test_running_an_analytic_over_a_selection(service: MarketDataService) -> None:
    frame = service.run_analytic("rolling_vwap", {"window": "30m"}, contracts=["ESH26"])
    assert frame.height > 0
    assert set(frame.get_column(C.CONTRACT).unique()) == {"ESH26"}


def test_an_unknown_analytic_names_the_ones_that_exist(service: MarketDataService) -> None:
    with pytest.raises(UnknownAnalyticError, match="rolling_vwap"):
        service.run_analytic("nope")


def test_an_analytic_asked_for_the_wrong_frequency_says_which_it_serves(
    service: MarketDataService,
) -> None:
    with pytest.raises(FrequencyNotSupportedError, match="minute"):
        service.run_analytic("rolling_vwap", frequency=Frequency.DAILY)


def test_bad_analytic_parameters_are_the_analytics_own_validation_error(
    service: MarketDataService,
) -> None:
    with pytest.raises(ValidationError):
        service.run_analytic("rolling_vwap", {"window": "not-a-duration"})


# --------------------------------------------------------------------------- #
# quality and insights
# --------------------------------------------------------------------------- #


def test_findings_filter_on_every_axis(service: MarketDataService) -> None:
    every = service.findings()
    narrowed = service.findings(
        FindingFilter(
            contracts=("CLG26",),
            checks=("invalid_ohlc",),
            min_severity=Severity.WARNING,
            start=date(2021, 1, 1),
            end=date(2022, 12, 31),
        )
    )
    assert 0 < narrowed.height < every.height
    assert set(narrowed.get_column(C.CONTRACT).unique()) == {"CLG26"}
    assert set(narrowed.get_column(C.CHECK_ID).unique()) == {"invalid_ohlc"}


def test_severity_is_a_minimum_not_an_exact_match(service: MarketDataService) -> None:
    """Severity is a ranking; a filter that ignored it would hide the worst findings."""
    info = service.findings(FindingFilter(min_severity=Severity.INFO))
    warning = service.findings(FindingFilter(min_severity=Severity.WARNING))
    assert set(warning.get_column(C.SEVERITY).unique()) <= {"WARNING", "ERROR"}
    assert warning.height < info.height


def test_findings_come_back_worst_first(service: MarketDataService) -> None:
    severities = service.findings().get_column(C.SEVERITY).to_list()
    ranks = [int(Severity.parse(s)) for s in severities]
    assert ranks == sorted(ranks, reverse=True)


def test_a_date_range_excludes_findings_that_name_no_session(tmp_path: Path) -> None:
    """A malformed record has no trading day to belong to."""
    service = service_for(fixture_dir(tmp_path, "generic_malformed.csv"))
    unbounded = service.findings(FindingFilter(frequency=Frequency.MINUTE))
    assert "malformed_record" in set(unbounded.get_column(C.CHECK_ID))

    bounded = service.findings(
        FindingFilter(frequency=Frequency.MINUTE, start=date(2026, 3, 1), end=date(2026, 3, 31))
    )
    assert "malformed_record" not in set(bounded.get_column(C.CHECK_ID))


def test_an_unknown_check_id_is_refused_rather_than_answered_with_nothing(
    service: MarketDataService,
) -> None:
    with pytest.raises(UnknownCheckError, match="invalid_ohlc"):
        service.findings(FindingFilter(checks=("invalid_olhc",)))


def test_the_quality_summary_totals_each_check_over_its_contracts(
    service: MarketDataService,
) -> None:
    summary = service.quality_summary()
    by_check = {item.check_id: item for item in summary.checks}
    assert by_check["invalid_ohlc"].contracts == 1
    assert by_check["invalid_ohlc"].findings_by_severity == {"WARNING": 6}
    assert summary.total_findings == sum(item.findings for item in summary.checks)
    assert summary.bars_affected == sum(item.bars_affected for item in summary.checks)
    # The thresholds travel with the answer so a downgrade can be explained.
    assert "config" in summary.thresholds


def test_the_summary_counts_bars_not_findings(tmp_path: Path) -> None:
    """One finding can cover several bars, and the headline number is bars.

    Counting findings instead would understate the problem exactly where it is worst: a
    session-level check reports one finding for a thousand bad bars.
    """
    service = service_for(fixture_dir(tmp_path, "generic_malformed.csv"))
    summary = service.quality_summary(Frequency.MINUTE)
    assert summary.total_findings == 4
    assert summary.bars_affected == 5
    missing = next(item for item in summary.checks if item.check_id == "missing_value")
    assert (missing.findings, missing.bars_affected) == (1, 2)


def test_checks_are_ordered_worst_affected_first(service: MarketDataService) -> None:
    affected = [item.bars_affected for item in service.quality_summary().checks]
    assert affected == sorted(affected, reverse=True)


def test_a_check_title_falls_back_to_the_id_when_no_check_owns_it() -> None:
    """`check_failed` is produced by the runner, not by a registered check."""
    assert check_title("invalid_ohlc") == "Incoherent OHLC"
    assert check_title("check_failed") == "check_failed"


def test_insights_come_back_strongest_first(service: MarketDataService) -> None:
    insights = service.insights()
    assert insights, "the committed slices carry the settlement signature"
    confidences = [insight.confidence for insight in insights]
    assert confidences == sorted(confidences, reverse=True)
    assert "carried_forward_settlement" in {insight.id for insight in insights}


# --------------------------------------------------------------------------- #
# uploads
# --------------------------------------------------------------------------- #


def test_an_upload_is_reported_on_in_full(service: MarketDataService) -> None:
    report = service.ingest_upload("generic_malformed.csv", GENERIC_MALFORMED.read_bytes())

    assert report.dataset_id == "upload:generic_malformed.csv"
    assert report.frequency is Frequency.MINUTE
    assert report.stats["rows_in"] == 25
    assert report.stats["rows_out"] == 22
    assert report.stats["rejects_by_reason"] == {
        "MALFORMED_LINE": 1,
        "NULL_CONTRACT": 1,
        "UNPARSEABLE_TIMESTAMP": 1,
    }
    assert len(report.rejects) == 3
    # Scoped to the file: only the checks that could fire on 22 rows of one session.
    assert {item.check_id for item in report.quality.checks} == {
        "malformed_record",
        "missing_value",
    }
    assert report.findings.height == 4


def test_uploading_the_same_name_twice_keeps_both(service: MarketDataService) -> None:
    payload = GENERIC_MALFORMED.read_bytes()
    first = service.ingest_upload("prices.csv", payload)
    second = service.ingest_upload("prices.csv", payload)
    assert first.dataset_id == "upload:prices.csv"
    assert second.dataset_id == "upload:prices.csv#2"
    assert service.ingest_upload("prices.csv", payload).dataset_id == "upload:prices.csv#3"


def test_an_upload_above_the_limit_is_refused_before_it_is_parsed(tmp_path: Path) -> None:
    service = service_for(fixture_dir(tmp_path), max_upload_bytes=10)
    with pytest.raises(UploadTooLargeError, match="the limit is 10 bytes"):
        service.ingest_upload("prices.csv", b"x" * 11)


def test_an_upload_the_platform_cannot_read_names_the_formats_it_can(
    service: MarketDataService,
) -> None:
    from mdq.ingest import UnsupportedFormatError

    with pytest.raises(UnsupportedFormatError, match=r"\.csv"):
        service.ingest_upload("prices.xlsx", b"anything")


def test_an_upload_changes_what_the_dataset_wide_answers_say(
    service: MarketDataService,
) -> None:
    before = service.quality_summary(Frequency.MINUTE).total_findings
    service.ingest_upload("generic_malformed.csv", GENERIC_MALFORMED.read_bytes())
    assert service.quality_summary(Frequency.MINUTE).total_findings > before


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #


def test_the_summary_adds_up_across_frequencies(service: MarketDataService) -> None:
    summary = service.summary()
    assert summary.contracts == ("CLG26", "ESH26")
    assert summary.exchanges == ("CME", "NYMEX")
    assert summary.row_count == sum(item.row_count for item in summary.frequencies)
    assert {item.frequency for item in summary.frequencies} == {
        Frequency.DAILY,
        Frequency.MINUTE,
    }


def test_a_contract_present_at_both_frequencies_is_one_entry(service: MarketDataService) -> None:
    """A contract picker wants one row per instrument and its widest known span."""
    merged = {info.contract: info for info in service.contracts()}
    daily = {info.contract: info for info in service.contracts(Frequency.DAILY)}
    minute = {info.contract: info for info in service.contracts(Frequency.MINUTE)}

    entry = merged["ESH26"]
    assert entry.frequencies == (Frequency.DAILY, Frequency.MINUTE)
    assert entry.root == "ES"
    assert entry.exchange == "CME"
    assert entry.bar_count == daily["ESH26"].bar_count + minute["ESH26"].bar_count
    assert entry.first_session == min(daily["ESH26"].first_session, minute["ESH26"].first_session)
    assert entry.last_session == max(daily["ESH26"].last_session, minute["ESH26"].last_session)


def test_a_contract_at_one_frequency_only_keeps_that_frequency(service: MarketDataService) -> None:
    """CLG26 has a daily file but no minute file, and the merge must say so."""
    merged = {info.contract: info for info in service.contracts()}
    assert merged["CLG26"].frequencies == (Frequency.DAILY,)
    daily = service.contracts(Frequency.DAILY)
    assert all(info.frequencies == (Frequency.DAILY,) for info in daily)
    assert [info.contract for info in service.contracts(Frequency.MINUTE)] == ["ESH26"]


def test_contract_codes_are_the_list_a_typo_is_checked_against(
    service: MarketDataService,
) -> None:
    assert service.contract_codes(Frequency.DAILY) == ("CLG26", "ESH26")


def test_params_may_be_a_model_a_mapping_or_nothing(service: MarketDataService) -> None:
    """Three callers, one validated object — the registry's `coerce_params` contract."""

    class Params(BaseModel):
        window: str = "5m"

    from_model = service.run_analytic("rolling_vwap", Params(window="5m"), contracts=["ESH26"])
    from_mapping = service.run_analytic("rolling_vwap", {"window": "5m"}, contracts=["ESH26"])
    assert from_model.equals(from_mapping)
