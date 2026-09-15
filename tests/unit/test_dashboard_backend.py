"""The dashboard's logic layer, tested without a script runner.

Everything a page could get wrong lives in `mdq.dashboard.backend`, so everything a page
could get wrong is asserted here: the severity the findings table opens on, how a JSON
instant becomes a Polars timestamp, how an activity profile becomes a heatmap, how a rule
becomes a copyable YAML block, and what happens when the API is not there.

The pure helpers are exercised against hand-built inputs so a failure names the function
rather than the fixture. The two backends are exercised against the committed vendor
slices, and against *each other*, in `tests/integration/test_dashboard_backends.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest

from mdq.api.schemas import (
    CheckTotal,
    FindingOut,
    QualitySummaryResponse,
)
from mdq.dashboard import backend as be
from mdq.dashboard.backend import (
    ACTIVITY_BAR_CAP,
    CHECK_SCHEMA,
    DEFAULT_API_URL,
    DEFAULT_MIN_SEVERITY,
    GAP_SCHEMA,
    Backend,
    BackendError,
    EmbeddedBackend,
    HttpBackend,
    SeverityTriage,
    build_backend,
    check_totals,
    conform,
    findings_table,
    gap_segments,
    heatmap_data,
    reconciliation,
    severity_triage,
    to_yaml,
)
from mdq.domain.config import AppSettings, QualityConfig
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, C
from mdq.quality.context import ACTIVITY_SCHEMA, A, Regime
from mdq.service import MarketDataService
from support.api import PARQUET_FIXTURES, service_for
from support.dashboard import backend_of, embedded_backend, http_backend, service_dir
from support.fixtures import GENERIC_MALFORMED

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A directory holding the three committed vendor slices."""
    return service_dir(tmp_path, *PARQUET_FIXTURES)


@pytest.fixture(params=["embedded", "http"])
def backend(request: pytest.FixtureRequest, data_dir: Path) -> Backend:
    """Both backends, each over its own service loaded from the same files."""
    built: Backend = backend_of(request.param, data_dir)
    return built


def _finding(**overrides: Any) -> FindingOut:
    """One `FindingOut` with sensible defaults, for the pure helpers."""
    fields: dict[str, Any] = {
        "check_id": "stale_bar",
        "title": "Stale (no-range) bar",
        "severity": "INFO",
        "contract": "ESH26",
        "frequency": Frequency.DAILY,
        "session_date": date(2026, 3, 4),
        "start_utc": None,
        "end_utc": None,
        "bars_affected": 1,
        "message": "flat bar with no volume",
        "evidence": {},
        "suggested_rule_id": None,
    }
    fields.update(overrides)
    return FindingOut(**fields)


# --------------------------------------------------------------------------- #
# the protocol
# --------------------------------------------------------------------------- #


def test_both_implementations_satisfy_the_protocol(data_dir: Path) -> None:
    service = service_for(data_dir)
    assert isinstance(embedded_backend(service), Backend)
    assert isinstance(http_backend(service), Backend)


def test_the_findings_view_opens_above_info() -> None:
    """The one product decision in this module: 2.5M INFO findings must not be the default."""
    assert DEFAULT_MIN_SEVERITY == "WARNING"


# --------------------------------------------------------------------------- #
# conform — the dtypes JSON cannot carry
# --------------------------------------------------------------------------- #


def test_conform_of_nothing_is_an_empty_frame_with_the_full_schema() -> None:
    frame = conform([], BAR_SCHEMA)
    assert frame.height == 0
    assert frame.schema == BAR_SCHEMA


def test_conform_parses_iso_strings_back_into_instants_and_dates() -> None:
    rows = [
        {
            "row_id": 1,
            "contract": "ESH26",
            "ts_utc": "2026-03-02T23:00:00Z",
            "ts_local": "2026-03-02T17:00:00",
            "session_date": "2026-03-03",
            "open": 1.0,
        }
    ]
    frame = conform(rows, BAR_SCHEMA)
    assert frame.schema == BAR_SCHEMA
    row = frame.row(0, named=True)
    assert row[C.TS_UTC] == datetime(2026, 3, 2, 23, tzinfo=UTC)
    assert row[C.TS_LOCAL] == datetime(2026, 3, 2, 17)
    assert row[C.SESSION_DATE] == date(2026, 3, 3)


def test_conform_fills_a_missing_column_with_typed_nulls_and_drops_extras() -> None:
    frame = conform([{"row_id": 1, "contract": "ESH26", "not_a_bar_column": 9}], BAR_SCHEMA)
    assert frame.columns == BAR_SCHEMA.names()
    assert frame.get_column(C.CLOSE).to_list() == [None]


def test_conform_parses_a_date_column_itself_rather_than_leaning_on_a_cast() -> None:
    """Asserted below the final cast: Polars happens to cast an ISO string to a Date today,
    and `conform` must not silently depend on that staying true for every dtype."""
    parsed = pl.DataFrame({C.SESSION_DATE: ["2026-03-03"]}).select(
        be._conform_expr(C.SESSION_DATE, pl.Date, pl.String)
    )
    assert parsed.schema[C.SESSION_DATE] == pl.Date


def test_conform_leaves_a_genuine_string_column_alone() -> None:
    frame = conform([{"row_id": 1, "contract": "ESH26"}], BAR_SCHEMA)
    assert frame.get_column(C.CONTRACT).to_list() == ["ESH26"]


# --------------------------------------------------------------------------- #
# triage
# --------------------------------------------------------------------------- #


def test_triage_separates_what_needs_a_human_from_what_is_expected() -> None:
    triage = severity_triage({"ERROR": 5, "WARNING": 39, "INFO": 14_974})
    assert (triage.needs_attention, triage.expected, triage.total) == (44, 14_974, 15_018)
    assert "14,974" in triage.hidden_note


def test_triage_says_so_when_nothing_is_being_hidden() -> None:
    assert severity_triage({"ERROR": 1}).hidden_note == "Every finding is shown."


def test_triage_of_an_empty_report_is_all_zeroes() -> None:
    assert severity_triage({}) == SeverityTriage(0, 0, 0)


# --------------------------------------------------------------------------- #
# the Overview chart data
# --------------------------------------------------------------------------- #


def _summary_with(checks: list[CheckTotal]) -> QualitySummaryResponse:
    return QualitySummaryResponse(
        frequency=Frequency.DAILY,
        total_findings=sum(c.findings for c in checks),
        bars_affected=sum(c.bars_affected for c in checks),
        findings_by_severity={},
        checks=checks,
        by_contract=[],
        checks_run=[],
        thresholds={},
    )


def test_check_totals_puts_the_busiest_check_first_and_splits_by_severity() -> None:
    frame = check_totals(
        _summary_with(
            [
                CheckTotal(
                    check_id="invalid_ohlc",
                    title="Incoherent OHLC",
                    findings=43,
                    bars_affected=43,
                    contracts=4,
                    findings_by_severity={"ERROR": 5, "WARNING": 38},
                ),
                CheckTotal(
                    check_id="stale_bar",
                    title="Stale (no-range) bar",
                    findings=14_152,
                    bars_affected=14_152,
                    contracts=40,
                    findings_by_severity={"INFO": 14_152},
                ),
            ]
        )
    )
    assert frame.schema == CHECK_SCHEMA
    assert frame.get_column("check").to_list()[0] == "Stale (no-range) bar"
    assert set(frame.get_column("severity").to_list()) == {"ERROR", "WARNING", "INFO"}
    assert frame.filter(pl.col("severity") == "ERROR").get_column("findings").to_list() == [5]


def test_check_totals_of_a_clean_report_is_an_empty_frame_not_an_error() -> None:
    assert check_totals(_summary_with([])).schema == CHECK_SCHEMA


# --------------------------------------------------------------------------- #
# the heatmap
# --------------------------------------------------------------------------- #


def _activity(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=ACTIVITY_SCHEMA)


def test_heatmap_of_nothing_is_empty() -> None:
    assert heatmap_data(_activity([])).is_empty


def _session(contract: str, day: date, regime: Regime) -> dict[str, Any]:
    return {
        C.CONTRACT: contract,
        C.SESSION_DATE: day,
        A.BAR_COUNT: 1,
        A.VOLUME: 0,
        A.REGIME: regime.value,
        A.BASELINE: 1.0,
        A.ACTIVE_THRESHOLD: 1.0,
        A.DORMANT_THRESHOLD: 0.0,
    }


def test_heatmap_reports_the_share_of_sessions_that_traded_normally() -> None:
    data = heatmap_data(
        _activity(
            [
                _session("ESH26", date(2026, 3, 2), Regime.ACTIVE),
                _session("ESH26", date(2026, 3, 3), Regime.ACTIVE),
                _session("ESH26", date(2026, 3, 4), Regime.DORMANT),
                _session("ESH26", date(2026, 3, 5), Regime.THIN),
            ]
        )
    )
    assert data.contracts == ["ESH26"] and data.months == ["2026-03"]
    assert data.active_share == [[0.5]]
    assert "2 normal, 1 light, 1 with nothing traded" in data.hover[0][0]


def test_heatmap_leaves_a_month_a_contract_was_not_listed_in_blank() -> None:
    """A blank cell means "not listed"; a dark one means "listed and silent". Different."""
    data = heatmap_data(
        _activity(
            [
                _session("CLG26", date(2026, 1, 5), Regime.DORMANT),
                _session("ESH26", date(2026, 2, 5), Regime.ACTIVE),
            ]
        )
    )
    assert data.months == ["2026-01", "2026-02"]
    assert data.active_share == [[0.0, None], [None, 1.0]]
    assert "not listed" in data.hover[0][1]


def test_heatmap_copes_with_a_dataset_that_only_ever_saw_one_regime() -> None:
    data = heatmap_data(_activity([_session("ESH26", date(2026, 3, 2), Regime.ACTIVE)]))
    assert data.active_share == [[1.0]]


# --------------------------------------------------------------------------- #
# the findings table and the gap strip
# --------------------------------------------------------------------------- #


def test_findings_table_is_readable_and_keeps_the_evidence_for_the_expander() -> None:
    frame = findings_table([_finding(evidence={"row_ids": [3, 4]})])
    row = frame.row(0, named=True)
    assert row["What was found"] == "Stale (no-range) bar"
    assert row["Instrument"] == "ESH26"
    assert json.loads(row["evidence"]) == {"row_ids": [3, 4]}


def test_findings_table_of_nothing_still_has_its_columns() -> None:
    assert findings_table([]).columns[:2] == ["Severity", "Instrument"]


def test_gap_segments_draws_an_observed_hole_at_its_real_width() -> None:
    frame = gap_segments(
        [
            _finding(
                check_id="intrabar_gap",
                severity="WARNING",
                start_utc=datetime(2026, 3, 4, 14, 0, tzinfo=UTC),
                end_utc=datetime(2026, 3, 4, 14, 40, tzinfo=UTC),
                evidence={"minutes_missing": 39},
            )
        ]
    )
    assert frame.schema == GAP_SCHEMA
    row = frame.row(0, named=True)
    assert row["minutes_missing"] == 39
    assert row["whole_session"] is False
    assert (row["to_utc"] - row["from_utc"]).total_seconds() == 40 * 60


def test_gap_segments_gives_a_missing_session_the_whole_day_and_no_invented_minutes() -> None:
    frame = gap_segments([_finding(check_id="missing_session", session_date=date(2026, 3, 4))])
    row = frame.row(0, named=True)
    assert row["whole_session"] is True
    assert row["minutes_missing"] is None
    assert row["from_utc"] == datetime(2026, 3, 4, tzinfo=UTC)
    assert row["to_utc"] == datetime(2026, 3, 5, tzinfo=UTC)


def test_gap_segments_ignores_findings_that_have_no_duration() -> None:
    assert gap_segments([_finding(check_id="invalid_ohlc")]).height == 0


def test_gap_segments_ignores_a_hole_that_names_neither_instants_nor_a_session() -> None:
    assert gap_segments([_finding(check_id="missing_session", session_date=None)]).height == 0


def test_gap_segments_of_nothing_keeps_the_schema() -> None:
    assert gap_segments([]).schema == GAP_SCHEMA


# --------------------------------------------------------------------------- #
# reconciliation — the settlement-close difference, labelled
# --------------------------------------------------------------------------- #


def test_reconciliation_labels_the_close_difference_as_a_convention() -> None:
    rows = reconciliation(
        {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0},
        {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.5, "bar_count": 1_380},
    )
    by_field = {row["Field"]: row for row in rows}
    assert by_field["Open"]["Difference"] == 0.0
    assert by_field["Close"]["Difference"] == pytest.approx(0.5)
    assert "settlement price" in by_field["Close"]["Note"]
    assert by_field["Bars in session"]["Rebuilt from minutes"] == 1_380


def test_reconciliation_of_a_session_only_one_side_has_is_empty() -> None:
    assert reconciliation(None, {"open": 1.0}) == []
    assert reconciliation({"open": 1.0}, None) == []


def test_reconciliation_reports_no_difference_when_a_field_is_missing() -> None:
    rows = reconciliation({"open": 10.0}, {"open": None})
    assert rows[0]["Difference"] is None


# --------------------------------------------------------------------------- #
# the copyable rule block
# --------------------------------------------------------------------------- #


def test_to_yaml_renders_a_nested_rule() -> None:
    text = to_yaml(
        {
            "rule_id": "ohlc_bounds",
            "kind": "validation",
            "params": {"exempt_when": {"volume": 0}, "assert": ["high >= low"]},
            "confidence": 0.907,
        }
    )
    assert text == (
        "rule_id: ohlc_bounds\n"
        "kind: validation\n"
        "params:\n"
        "  exempt_when:\n"
        "    volume: 0\n"
        "  assert:\n"
        '    - "high >= low"\n'
        "confidence: 0.907"
    )


def test_to_yaml_quotes_anything_yaml_would_read_as_something_else() -> None:
    text = to_yaml(
        {
            "yes_like": "yes",
            "numeric": "12",
            "empty": "",
            "colon": "a: b",
            "leading_dash": "-x",
            "padded": " x ",
        }
    )
    assert 'yes_like: "yes"' in text
    assert 'numeric: "12"' in text
    assert 'empty: ""' in text
    assert 'colon: "a: b"' in text
    assert 'leading_dash: "-x"' in text
    assert 'padded: " x "' in text


def test_to_yaml_handles_the_degenerate_shapes() -> None:
    assert to_yaml({}) == "{}"
    assert to_yaml([]) == "[]"
    assert to_yaml("plain") == "plain"
    assert to_yaml(None) == "null"
    assert to_yaml(True) == "true"
    assert to_yaml(False) == "false"
    assert to_yaml({"a": {}, "b": []}) == "a: {}\nb: []"
    assert to_yaml([{"a": 1}, [2]]) == "-\n  a: 1\n-\n  - 2"


# --------------------------------------------------------------------------- #
# choosing a backend
# --------------------------------------------------------------------------- #


def test_embedded_is_chosen_by_the_flag_make_ui_passes(data_dir: Path) -> None:
    chosen = build_backend(["--embedded"], {"MDQ_DATA_DIR": str(data_dir)})
    assert isinstance(chosen, EmbeddedBackend)
    assert chosen.summary().bars > 0


def test_embedded_is_chosen_by_the_environment_too(data_dir: Path) -> None:
    chosen = build_backend([], {"MDQ_UI_EMBEDDED": "TRUE", "MDQ_DATA_DIR": str(data_dir)})
    assert isinstance(chosen, EmbeddedBackend)


def test_embedded_without_a_data_directory_starts_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dashboard started before `make fetch` must come up, not fall over."""
    monkeypatch.setenv("MDQ_DATA_DIR", "definitely/not/here")
    chosen = EmbeddedBackend.autoloaded()
    assert chosen.summary().bars == 0


@pytest.mark.parametrize(
    ("argv", "env", "expected"),
    [
        ([], {}, DEFAULT_API_URL),
        ([], {"MDQ_API_URL": "http://elsewhere:9000"}, "http://elsewhere:9000"),
        (["--api-url=http://flag:1"], {}, "http://flag:1"),
        (["--api-url", "http://flag:2"], {}, "http://flag:2"),
        (["--api-url"], {}, DEFAULT_API_URL),
        ([], {"MDQ_UI_EMBEDDED": "no"}, DEFAULT_API_URL),
    ],
)
def test_the_api_url_comes_from_the_flag_then_the_environment_then_the_default(
    argv: list[str], env: dict[str, str], expected: str
) -> None:
    chosen = build_backend(argv, env)
    assert isinstance(chosen, HttpBackend)
    assert chosen.base_url == expected


def test_build_backend_defaults_to_no_arguments_at_all() -> None:
    assert isinstance(build_backend(), HttpBackend)


# --------------------------------------------------------------------------- #
# HttpBackend — the failure paths a page has to survive
# --------------------------------------------------------------------------- #


def _transport(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://stub")


def test_an_unreachable_api_becomes_a_sentence_naming_the_fix() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(BackendError, match="make api"):
        HttpBackend("http://stub", client=_transport(refuse)).summary()


def test_an_api_error_surfaces_the_apis_own_detail() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unknown contract 'XX'"})

    with pytest.raises(BackendError, match="unknown contract 'XX'"):
        HttpBackend("http://stub", client=_transport(reject)).summary()


def test_a_validation_error_list_is_shown_rather_than_dropped() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"loc": ["query", "limit"]}]})

    with pytest.raises(BackendError, match="limit"):
        HttpBackend("http://stub", client=_transport(reject)).summary()


def test_a_body_that_is_not_an_error_object_is_still_reported() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=["boom"])

    with pytest.raises(BackendError, match="boom"):
        HttpBackend("http://stub", client=_transport(reject)).summary()


def test_a_body_that_is_not_json_at_all_is_still_reported() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream is down")

    with pytest.raises(BackendError, match="upstream is down"):
        HttpBackend("http://stub", client=_transport(reject)).summary()


def test_an_empty_non_json_body_falls_back_to_the_status_reason() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="")

    with pytest.raises(BackendError, match="Service Unavailable"):
        HttpBackend("http://stub", client=_transport(reject)).summary()


def test_an_upload_to_an_unreachable_api_is_a_backend_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(BackendError, match="could not upload"):
        HttpBackend("http://stub", client=_transport(refuse)).upload("x.csv", b"a,b\n")


def test_http_backend_builds_its_own_client_when_it_is_not_given_one() -> None:
    made = HttpBackend("http://example.test:8000/")
    assert made.base_url == "http://example.test:8000"
    assert "example.test" in made.label


def test_an_unknown_analytic_still_returns_a_usable_frame(data_dir: Path) -> None:
    """No schema is known for it, so the rows come back with inferred dtypes, not an error."""
    stub = HttpBackend(
        "http://stub",
        client=_transport(
            lambda _request: httpx.Response(
                200, json={"rows": [{"a": 1}], "next_offset": None, "row_count": 1}
            )
        ),
    )
    assert stub.analytic("not_in_the_table", Frequency.DAILY).to_dicts() == [{"a": 1}]


def test_paging_follows_next_offset_until_the_api_runs_out() -> None:
    pages = {
        0: {"rows": [{"row_id": 1, "contract": "A"}], "next_offset": 1},
        1: {"rows": [{"row_id": 2, "contract": "B"}], "next_offset": None},
    }

    def serve(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json=pages[offset])

    frame = HttpBackend("http://stub", client=_transport(serve)).bars(Frequency.DAILY)
    assert frame.get_column(C.CONTRACT).to_list() == ["A", "B"]


def test_paging_stops_at_the_row_limit_the_caller_asked_for() -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "1"
        return httpx.Response(200, json={"rows": [{"row_id": 1}], "next_offset": 1})

    frame = HttpBackend("http://stub", client=_transport(serve)).bars(Frequency.DAILY, limit=1)
    assert frame.height == 1


def test_the_activity_map_refuses_to_rebuild_the_minute_corpus_over_http() -> None:
    body = {
        "datasets": [],
        "contracts": [],
        "exchanges": [],
        "bars": ACTIVITY_BAR_CAP + 1,
        "by_frequency": [
            {
                "frequency": "minute",
                "files": 1,
                "contracts": [],
                "exchanges": [],
                "bars": ACTIVITY_BAR_CAP + 1,
                "first_session": None,
                "last_session": None,
            }
        ],
    }
    stub = HttpBackend(
        "http://stub", client=_transport(lambda _request: httpx.Response(200, json=body))
    )
    with pytest.raises(BackendError, match="more than"):
        stub.activity(Frequency.MINUTE)


def test_the_activity_map_of_an_empty_service_is_an_empty_profile(tmp_path: Path) -> None:
    empty = http_backend(service_for(tmp_path))
    assert empty.activity(Frequency.DAILY).schema == ACTIVITY_SCHEMA


def test_the_activity_map_uses_the_servers_own_thresholds(data_dir: Path) -> None:
    """A re-tuned server must be redrawn with *its* numbers, never with our defaults.

    The threshold is moved from 1% of the contract's busiest session to 50%, which
    reclassifies hundreds of light sessions. If the heatmap were built from a local
    `QualityConfig()` it would keep showing the old picture while the checks used the new
    one — the two would disagree about the same data, which is the worst outcome available.
    """
    retuned = MarketDataService(
        AppSettings(data_dir=data_dir, quality=QualityConfig(daily_thin_volume_ratio=0.5))
    )
    retuned.autoload()
    over_http = http_backend(retuned)
    assert over_http._config(Frequency.DAILY).daily_thin_volume_ratio == 0.5

    theirs = over_http.activity(Frequency.DAILY)
    mine = EmbeddedBackend(retuned).activity(Frequency.DAILY)
    assert theirs.equals(mine)
    with_defaults = backend_of("embedded", data_dir).activity(Frequency.DAILY)
    assert not theirs.equals(with_defaults), "the retuned thresholds changed nothing"


def test_the_thresholds_fall_back_to_the_defaults_when_the_report_declares_none() -> None:
    body = {
        "frequency": "daily",
        "total_findings": 0,
        "bars_affected": 0,
        "findings_by_severity": {},
        "checks": [],
        "by_contract": [],
        "checks_run": [],
        "thresholds": {},
    }
    stub = HttpBackend(
        "http://stub", client=_transport(lambda _request: httpx.Response(200, json=body))
    )
    assert stub._config(Frequency.DAILY).max_gap_minutes == 5


# --------------------------------------------------------------------------- #
# EmbeddedBackend — the failure paths a page has to survive
# --------------------------------------------------------------------------- #


def test_a_typo_in_a_contract_code_is_a_sentence_not_a_traceback(backend: Backend) -> None:
    with pytest.raises(BackendError, match="unknown contract"):
        backend.bars(Frequency.DAILY, ["NOPE1"])


def test_an_unknown_analytic_is_a_sentence_too(backend: Backend) -> None:
    with pytest.raises(BackendError, match="unknown analytic"):
        backend.analytic("not_an_analytic", Frequency.DAILY)


def test_an_impossible_date_range_is_a_sentence_too(backend: Backend) -> None:
    with pytest.raises(BackendError):
        backend.bars(Frequency.DAILY, None, date(2026, 3, 5), date(2026, 3, 1))


def test_an_unreadable_upload_is_a_sentence_too(backend: Backend) -> None:
    """`.docx` is not a format any reader claims — a 400, said as a sentence."""
    with pytest.raises(BackendError, match="ingest"):
        backend.upload("notes.docx", b"not bars")


def test_an_unknown_check_id_is_a_sentence_too(backend: Backend) -> None:
    with pytest.raises(BackendError):
        backend.findings(be.FindingQuery(check=["no_such_check"]))


# --------------------------------------------------------------------------- #
# query-parameter shaping
# --------------------------------------------------------------------------- #


def test_a_selection_omits_everything_left_unsaid() -> None:
    assert be._selection() == {}


def test_a_selection_sends_dates_and_enums_as_strings() -> None:
    params = be._selection(
        Frequency.MINUTE, ["ESH26"], date(2026, 3, 2), date(2026, 3, 5), be.View.CLEAN
    )
    assert params == {
        "frequency": "minute",
        "contract": ["ESH26"],
        "start": "2026-03-02",
        "end": "2026-03-05",
        "view": "clean",
    }


def test_scalar_parameters_drop_nulls_and_flatten_models() -> None:
    from mdq.analytics.vwap import VwapParams

    flattened = be._scalar(
        {
            "nothing": None,
            "frequency": Frequency.DAILY,
            "view": be.View.RAW,
            "day": date(2026, 3, 2),
            "params": VwapParams(window="5m"),
            "limit": 10,
        }
    )
    assert flattened["frequency"] == "daily"
    assert flattened["view"] == "raw"
    assert flattened["day"] == "2026-03-02"
    assert flattened["params"]["window"] == "5m"
    assert flattened["limit"] == 10
    assert "nothing" not in flattened


# --------------------------------------------------------------------------- #
# the composed helpers, over real data
# --------------------------------------------------------------------------- #


def test_candles_come_back_in_session_order(backend: Backend) -> None:
    frame = be.candles(backend, "ESH26", date(2026, 3, 2), date(2026, 3, 13))
    sessions = frame.get_column(C.SESSION_DATE).to_list()
    assert sessions == sorted(sessions)
    assert frame.height > 0


def test_session_coverage_exposes_the_truncated_session(backend: Backend) -> None:
    """2026-03-02 holds 960 of a possible 1,380 minutes; a reader must be able to see that."""
    coverage = be.session_coverage(backend, "ESH26")
    counts = dict(
        zip(
            coverage.get_column(C.SESSION_DATE).to_list(),
            coverage.get_column("bar_count").to_list(),
            strict=True,
        )
    )
    assert counts[date(2026, 3, 2)] == 960
    assert counts[date(2026, 3, 3)] == 1_380


def test_asking_for_minute_coverage_of_a_daily_only_instrument_says_so(
    backend: Backend,
) -> None:
    """CLG26 has no minute file. The page checks `frequencies` first; the backend is blunt."""
    with pytest.raises(BackendError, match="CLG26"):
        be.session_coverage(backend, "CLG26")


def test_the_vwap_series_keeps_one_row_per_bar_with_its_window_size(
    backend: Backend,
) -> None:
    series = be.session_vwap(backend, "ESH26", date(2026, 3, 3))
    assert series.height == 1_380
    assert series.get_column("bars_in_window").to_list()[0] == 1
    assert series.get_column(C.CLOSE).null_count() == 0


def test_the_vwap_series_for_a_session_with_no_bars_is_empty_not_an_error(
    backend: Backend,
) -> None:
    series = be.session_vwap(backend, "ESH26", date(2026, 3, 7))
    assert series.height == 0
    assert "vwap" in series.columns


def test_an_upload_reports_its_rejects_and_its_findings(backend: Backend) -> None:
    report = backend.upload(GENERIC_MALFORMED.name, GENERIC_MALFORMED.read_bytes())
    assert report.ingest.rows_rejected > 0
    assert report.rejects
    assert report.dataset_id.startswith("upload:")


# --------------------------------------------------------------------------- #
# the composed helpers, against a backend that answers awkwardly
# --------------------------------------------------------------------------- #


class _StubBackend:
    """A backend that answers with exactly what a test hands it."""

    label = "stub"

    def __init__(self, bars: pl.DataFrame, analytic: pl.DataFrame) -> None:
        self._bars, self._analytic = bars, analytic

    def bars(self, *args: Any, **kwargs: Any) -> pl.DataFrame:
        del args, kwargs
        return self._bars

    def analytic(self, *args: Any, **kwargs: Any) -> pl.DataFrame:
        del args, kwargs
        return self._analytic


def test_the_vwap_series_keeps_a_bar_whose_window_produced_no_average() -> None:
    """A window with no volume has no VWAP. The bar must stay on the chart with a hole in
    the average line, not vanish from it."""
    bars = conform(
        [
            {
                C.ROW_ID: index,
                C.CONTRACT: "ESH26",
                C.TS_UTC: f"2026-03-03T14:0{index}:00Z",
                C.TS_LOCAL: f"2026-03-03T08:0{index}:00",
                C.SESSION_DATE: "2026-03-03",
                C.CLOSE: 100.0 + index,
                C.VOLUME: 0,
            }
            for index in range(3)
        ],
        BAR_SCHEMA,
    )
    stub = _StubBackend(bars, pl.DataFrame(schema=be.VWAP_SCHEMA))
    series = be.session_vwap(stub, "ESH26", date(2026, 3, 3))
    assert series.height == 3
    assert series.get_column("vwap").to_list() == [None, None, None]
    assert series.get_column(C.CLOSE).to_list() == [100.0, 101.0, 102.0]


def test_session_coverage_puts_the_sessions_in_order_whatever_order_they_arrive_in() -> None:
    unsorted = pl.DataFrame(
        {
            C.CONTRACT: ["ESH26", "ESH26"],
            C.SESSION_DATE: [date(2026, 3, 5), date(2026, 3, 2)],
            "bar_count": [1_380, 960],
        }
    )
    stub = _StubBackend(pl.DataFrame(schema=BAR_SCHEMA), unsorted)
    coverage = be.session_coverage(stub, "ESH26")
    assert coverage.get_column(C.SESSION_DATE).to_list() == [date(2026, 3, 2), date(2026, 3, 5)]


def test_candles_put_the_sessions_in_order_whatever_order_they_arrive_in() -> None:
    rows = [
        {
            C.ROW_ID: index,
            C.CONTRACT: "ESH26",
            C.SESSION_DATE: day,
            C.CLOSE: 100.0,
        }
        for index, day in enumerate(("2026-03-05", "2026-03-02"))
    ]
    stub = _StubBackend(conform(rows, BAR_SCHEMA), pl.DataFrame())
    assert be.candles(stub, "ESH26", None, None).get_column(C.SESSION_DATE).to_list() == [
        date(2026, 3, 2),
        date(2026, 3, 5),
    ]


def test_the_embedded_backend_honours_the_row_limit_it_is_given(data_dir: Path) -> None:
    """Without it, a page asking for a preview would pull the whole frame into the browser."""
    assert backend_of("embedded", data_dir).bars(Frequency.DAILY, ["ESH26"], limit=5).height == 5
