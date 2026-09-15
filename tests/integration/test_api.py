"""The HTTP surface, end to end through `TestClient`.

These tests run the real stack — autoload, ingest, checks, cleansing, insights — over the
committed vendor slices, and assert on the JSON a client actually receives. They are
deliberately about *contract*: field names a business user reads, status codes a client
branches on, and paging that terminates.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import polars as pl
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

import mdq.api
from mdq.analytics import REGISTRY
from mdq.api.deps import ROW_LIMIT_DEFAULT, ROW_LIMIT_MAX
from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, BarFrame, C
from support.api import PARQUET_FIXTURES, client_for, fixture_dir, service_for
from support.fixtures import GENERIC_MALFORMED

pytestmark = pytest.mark.integration


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """A client over the three committed vendor slices."""
    return client_for(service_for(fixture_dir(tmp_path, *PARQUET_FIXTURES)))


class _ScaleParams(BaseModel):
    """Knobs for the throwaway analytic below."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scale: int = Field(default=2, ge=1)


@pytest.fixture
def throwaway_analytic() -> Iterator[str]:
    """Register an analytic this codebase has never heard of, then unregister it.

    Registration is by decorator at import time, so an analytic that only exists inside a
    test has to be put into — and taken back out of — the process-wide registry by hand.
    The teardown reaches into the registry's private table for that reason and no other.
    """

    class Scaled:
        name: ClassVar[str] = "scaled_close_probe"
        frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
        Params: ClassVar[type[BaseModel]] = _ScaleParams

        def run(self, bars: BarFrame, params: BaseModel) -> pl.DataFrame:
            scale = getattr(params, "scale", 1)
            return bars.lf.select(
                C.CONTRACT, (pl.col(C.CLOSE) * scale).alias("scaled_close")
            ).collect()

    REGISTRY.register(Scaled)  # type: ignore[arg-type]
    try:
        yield Scaled.name
    finally:
        REGISTRY._analytics.pop(Scaled.name, None)


# --------------------------------------------------------------------------- #
# health and inventory
# --------------------------------------------------------------------------- #


def test_health_reports_what_is_registered_without_reading_it(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["files"] == 3
    assert body["frequencies"] == ["daily", "minute"]
    assert body["in_memory"] == [], "health must not materialise anything"
    assert "rolling_vwap" in body["analytics"]
    assert "invalid_ohlc" in body["checks"]


def test_health_shows_a_frequency_once_it_has_been_queried(client: TestClient) -> None:
    client.get("/bars?frequency=daily&limit=1")
    assert client.get("/health").json()["in_memory"] == ["daily"]


def test_the_summary_describes_both_frequencies(client: TestClient) -> None:
    body = client.get("/datasets/summary").json()
    assert body["contracts"] == ["CLG26", "ESH26"]
    assert body["exchanges"] == ["CME", "NYMEX"]
    by_frequency = {item["frequency"]: item for item in body["by_frequency"]}
    assert by_frequency["daily"]["files"] == 2
    assert by_frequency["minute"]["files"] == 1
    assert body["bars"] == sum(item["bars"] for item in body["by_frequency"])


def test_contracts_lists_instruments_with_their_spans(client: TestClient) -> None:
    body = client.get("/contracts").json()
    assert body["contract_count"] == 2
    esh = next(c for c in body["contracts"] if c["contract"] == "ESH26")
    assert esh["root"] == "ES"
    assert esh["exchange"] == "CME"
    assert esh["frequencies"] == ["daily", "minute"]
    assert esh["first_session"] < esh["last_session"]


def test_contracts_can_be_narrowed_to_one_frequency(client: TestClient) -> None:
    body = client.get("/contracts?frequency=minute").json()
    assert [c["contract"] for c in body["contracts"]] == ["ESH26"]


# --------------------------------------------------------------------------- #
# bars
# --------------------------------------------------------------------------- #


def test_bars_come_back_on_the_canonical_schema(client: TestClient) -> None:
    body = client.get("/bars?contract=ESH26&frequency=daily&limit=1").json()
    assert body["frequency"] == "daily"
    assert body["view"] == "raw"
    row = body["rows"][0]
    assert set(row) == set(BAR_SCHEMA.names())
    assert row["contract"] == "ESH26"
    assert row["ts_utc"].endswith("Z") or "+00:00" in row["ts_utc"]


def test_paging_walks_the_whole_frame_and_then_stops(client: TestClient) -> None:
    """`next_offset` is null exactly once there is nothing further to fetch."""
    expected = next(
        item["bars"]
        for item in client.get("/contracts?frequency=daily").json()["contracts"]
        if item["contract"] == "CLG26"
    )
    seen: list[dict[str, object]] = []
    offset: int | None = 0
    pages = 0
    while offset is not None:
        body = client.get(f"/bars?contract=CLG26&frequency=daily&limit=500&offset={offset}").json()
        seen.extend(body["rows"])
        assert body["row_count"] == len(body["rows"])
        offset = body["next_offset"]
        pages += 1

    assert pages > 1, "the fixture must be big enough for this test to mean anything"
    assert len(seen) == expected
    assert len({row[C.ROW_ID] for row in seen}) == expected


def test_the_last_page_has_no_next_offset(client: TestClient) -> None:
    body = client.get("/bars?contract=ESH26&frequency=daily").json()
    assert body["next_offset"] is None


def test_filtering_to_nothing_is_an_empty_list_not_an_error(client: TestClient) -> None:
    body = client.get("/bars?contract=ESH26&start=1990-01-01&end=1990-01-02").json()
    assert body["rows"] == []
    assert body["row_count"] == 0
    assert body["next_offset"] is None


def test_the_clean_view_is_available_and_differs_from_raw(tmp_path: Path) -> None:
    client = client_for(service_for(fixture_dir(tmp_path, "generic_malformed.csv")))
    raw = client.get("/bars?view=raw&frequency=minute").json()
    clean = client.get("/bars?view=clean&frequency=minute").json()
    assert clean["view"] == "clean"
    assert clean["row_count"] < raw["row_count"]


def test_the_row_limit_bounds_are_published_and_enforced(client: TestClient) -> None:
    parameters = {
        p["name"]: p
        for p in client.get("/openapi.json").json()["paths"]["/bars"]["get"]["parameters"]
    }
    schema = parameters["limit"]["schema"]
    assert schema["default"] == ROW_LIMIT_DEFAULT == 5_000
    assert schema["maximum"] == ROW_LIMIT_MAX == 50_000
    assert client.get(f"/bars?limit={ROW_LIMIT_MAX + 1}").status_code == 422
    assert client.get("/bars?limit=0").status_code == 422


# --------------------------------------------------------------------------- #
# analytics — generated from the registry
# --------------------------------------------------------------------------- #


def test_every_registered_analytic_has_a_route(client: TestClient) -> None:
    """`/analytics/rolling_vwap` is in the OpenAPI schema because it is registered.

    Asserted against the registry rather than a literal list, so an analytic added
    tomorrow is covered by this test today.
    """
    paths = client.get("/openapi.json").json()["paths"]
    assert "/analytics/rolling_vwap" in paths
    for name in REGISTRY.names():
        assert f"/analytics/{name}" in paths


def test_no_analytic_route_is_hand_written() -> None:
    """The other half: a hand-written route would have to spell its own path.

    Nothing in `mdq.api` may contain the literal path of a registered analytic. Combined
    with the test above — the route exists — and the one below — a *new* analytic gets one
    too — this pins the seam shut from all three sides.
    """
    for module in Path(mdq.api.__file__).parent.glob("*.py"):
        source = module.read_text()
        for name in REGISTRY.names():
            assert f'"/analytics/{name}"' not in source
            assert f"'/analytics/{name}'" not in source


def test_an_analytic_registered_today_gets_a_route_with_no_code_change(
    throwaway_analytic: str, tmp_path: Path
) -> None:
    """Add a file, register, done — proved by doing it.

    The strongest available form of "the routes are generated": an analytic this codebase
    has never heard of is registered, a fresh app is built, and the route, its query
    parameters and its 422s all exist without a line being written for it.
    """
    client = client_for(service_for(fixture_dir(tmp_path, *PARQUET_FIXTURES)))
    path = f"/analytics/{throwaway_analytic}"

    parameters = {
        p["name"]: p for p in client.get("/openapi.json").json()["paths"][path]["get"]["parameters"]
    }
    assert parameters["scale"]["schema"]["default"] == 2
    assert parameters["scale"]["schema"]["minimum"] == 1

    body = client.get(f"{path}?contract=ESH26&frequency=daily&scale=3&limit=2").json()
    assert body["analytic"] == throwaway_analytic
    assert body["parameters"] == {"scale": 3}
    assert body["rows"][0]["scaled_close"] > 0
    assert client.get(f"{path}?scale=0").status_code == 422

    assert throwaway_analytic in {a["name"] for a in client.get("/analytics").json()["analytics"]}


def test_an_analytics_query_parameters_come_from_its_params_model(client: TestClient) -> None:
    from mdq.analytics.vwap import VwapParams

    parameters = {
        p["name"]: p
        for p in client.get("/openapi.json").json()["paths"]["/analytics/rolling_vwap"]["get"][
            "parameters"
        ]
    }
    assert set(VwapParams.model_fields) <= set(parameters)
    assert parameters["window"]["schema"]["default"] == "15m"
    assert parameters["min_volume"]["schema"]["minimum"] == 0


def test_the_catalogue_publishes_each_analytics_parameter_schema(client: TestClient) -> None:
    body = client.get("/analytics").json()
    by_name = {item["name"]: item for item in body["analytics"]}
    assert by_name["rolling_vwap"]["frequencies"] == ["minute"]
    assert by_name["rolling_vwap"]["path"] == "/analytics/rolling_vwap"
    assert set(by_name["rolling_vwap"]["parameters"]["properties"]) == {
        "window",
        "price",
        "min_volume",
    }
    assert by_name["daily_bars"]["parameters"]["properties"] == {}


def test_an_analytic_runs_with_its_defaults_filled_in(client: TestClient) -> None:
    body = client.get("/analytics/rolling_vwap?contract=ESH26&limit=3").json()
    assert body["analytic"] == "rolling_vwap"
    assert body["frequency"] == "minute", "a minute-only analytic picks its own frequency"
    assert body["parameters"] == {"window": "15m", "price": "typical", "min_volume": 0}
    assert set(body["rows"][0]) == {
        "contract",
        "session_date",
        "ts_utc",
        "vwap",
        "window_volume",
        "bars_in_window",
        "window_start_utc",
    }


def test_analytic_parameters_are_honoured(client: TestClient) -> None:
    body = client.get("/analytics/rolling_vwap?contract=ESH26&window=1h&price=close").json()
    assert body["parameters"] == {"window": "1h", "price": "close", "min_volume": 0}


def test_an_analytic_over_an_empty_selection_returns_an_empty_list(client: TestClient) -> None:
    body = client.get("/analytics/rolling_vwap?contract=ESH26&start=1990-01-01&end=1990-01-02")
    assert body.status_code == 200
    assert body.json()["rows"] == []


# --------------------------------------------------------------------------- #
# quality
# --------------------------------------------------------------------------- #


def test_the_quality_summary_is_readable_without_a_glossary(client: TestClient) -> None:
    body = client.get("/quality/summary").json()
    assert body["frequency"] == "daily"
    assert set(body["findings_by_severity"]) == {"ERROR", "WARNING", "INFO"}
    worst = body["checks"][0]
    assert set(worst) == {
        "check_id",
        "title",
        "findings",
        "bars_affected",
        "contracts",
        "findings_by_severity",
    }
    assert worst["title"] and not worst["title"].startswith(worst["check_id"])
    assert body["checks_run"], "every check that ran is named, even the silent ones"
    assert "config" in body["thresholds"]


def test_findings_can_be_filtered_the_way_a_person_asks(client: TestClient) -> None:
    body = client.get("/quality/findings?contract=CLG26&check=invalid_ohlc&limit=100").json()
    assert body["row_count"] == 6
    finding = body["findings"][0]
    assert finding["contract"] == "CLG26"
    assert finding["title"] == "Incoherent OHLC"
    assert finding["severity"] == "WARNING"
    assert finding["bars_affected"] == 1
    assert isinstance(finding["evidence"], dict), "evidence is decoded, not a JSON string"
    assert finding["evidence"]["carried_forward_settlement"] == 1
    assert finding["suggested_rule_id"]


def test_severity_filters_from_a_floor(client: TestClient) -> None:
    every = client.get("/quality/findings?limit=50000").json()["row_count"]
    warnings = client.get("/quality/findings?severity=WARNING&limit=50000").json()
    assert 0 < warnings["row_count"] < every
    assert {f["severity"] for f in warnings["findings"]} <= {"WARNING", "ERROR"}


def test_findings_can_be_narrowed_to_a_date_range(client: TestClient) -> None:
    body = client.get("/quality/findings?start=2021-01-01&end=2021-12-31&limit=50000").json()
    assert body["row_count"] > 0
    assert all("2021-01-01" <= f["session_date"] <= "2021-12-31" for f in body["findings"])


def test_a_filter_that_matches_nothing_is_an_empty_list(client: TestClient) -> None:
    body = client.get("/quality/findings?check=negative_volume")
    assert body.status_code == 200
    assert body.json()["findings"] == []


# --------------------------------------------------------------------------- #
# insights
# --------------------------------------------------------------------------- #


def test_insights_carry_the_pattern_and_the_rule_to_adopt(client: TestClient) -> None:
    body = client.get("/insights").json()
    assert body["frequency"] == "daily"
    assert body["insight_count"] == len(body["insights"])
    insight = next(i for i in body["insights"] if i["id"] == "carried_forward_settlement")
    assert "settlement" in insight["pattern"].lower()
    assert insight["affected_contracts"] == ["CLG26"]
    assert insight["rule"]["kind"] == "validation"
    assert 0.0 <= insight["rule"]["confidence"] <= 1.0
    assert insight["rule"]["rationale"]


def test_insights_are_ordered_by_confidence(client: TestClient) -> None:
    confidences = [i["rule"]["confidence"] for i in client.get("/insights").json()["insights"]]
    assert confidences == sorted(confidences, reverse=True)


def test_an_empty_service_answers_every_endpoint_with_nothing(tmp_path: Path) -> None:
    """A user who starts the API before fetching data gets empty states, not errors."""
    client = client_for(service_for(fixture_dir(tmp_path)))
    assert client.get("/health").json()["files"] == 0
    assert client.get("/datasets/summary").json()["by_frequency"] == []
    assert client.get("/contracts").json()["contracts"] == []
    assert client.get("/bars").json()["rows"] == []
    assert client.get("/quality/summary").json()["checks"] == []
    assert client.get("/quality/findings").json()["findings"] == []
    assert client.get("/insights").json()["insights"] == []
    assert client.get("/analytics/rolling_vwap").json()["rows"] == []


# --------------------------------------------------------------------------- #
# upload
# --------------------------------------------------------------------------- #


def _upload(client: TestClient, name: str, payload: bytes) -> dict[str, object]:
    response = client.post("/ingest", files={"file": (name, payload, "text/csv")})
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def test_uploading_a_faulty_csv_returns_findings_and_insights(client: TestClient) -> None:
    """The whole point of the upload path: what is wrong, and what it means."""
    body = _upload(client, "generic_malformed.csv", GENERIC_MALFORMED.read_bytes())

    ingest = body["ingest"]
    assert isinstance(ingest, dict)
    assert ingest["rows_in"] == 25
    assert ingest["rows_out"] == 22
    assert ingest["profile"] == "generic"
    assert ingest["rejects_by_reason"] == {
        "MALFORMED_LINE": 1,
        "NULL_CONTRACT": 1,
        "UNPARSEABLE_TIMESTAMP": 1,
    }

    rejects = body["rejects"]
    assert isinstance(rejects, list)
    assert {r["reason"] for r in rejects} == set(ingest["rejects_by_reason"])
    assert all(r["source"] == "generic_malformed.csv" for r in rejects)

    findings = body["findings"]
    assert isinstance(findings, list)
    assert findings, "a faulty file must come back with findings"
    by_check = {f["check_id"]: f for f in findings}
    assert set(by_check) == {"malformed_record", "missing_value"}
    # One finding can cover several bars, and the number must be the finding's own.
    assert by_check["missing_value"]["bars_affected"] == 2
    assert by_check["missing_value"]["evidence"]["row_ids"] == [5, 17]

    insights = body["insights"]
    assert isinstance(insights, list)
    assert insights, "and with the patterns those findings sit inside"
    assert all(i["rule"]["rule_id"] for i in insights)


def test_an_uploaded_file_is_queryable_afterwards(client: TestClient) -> None:
    body = _upload(client, "generic_malformed.csv", GENERIC_MALFORMED.read_bytes())
    assert body["dataset_id"] == "upload:generic_malformed.csv"
    assert client.get("/health").json()["datasets"] == ["autoload", "upload:generic_malformed.csv"]

    checks = client.get("/quality/findings?frequency=minute&limit=50000").json()
    assert "malformed_record" in {f["check_id"] for f in checks["findings"]}


def test_a_clean_upload_reports_no_rejects(client: TestClient) -> None:
    from support.fixtures import GENERIC_CLEAN

    body = _upload(client, "generic_clean.csv", GENERIC_CLEAN.read_bytes())
    ingest = body["ingest"]
    assert isinstance(ingest, dict)
    assert ingest["rows_rejected"] == 0
    assert body["rejects"] == []


def test_a_parquet_upload_works_too(client: TestClient) -> None:
    from support.fixtures import CLG26_DAILY

    payload = CLG26_DAILY.read_bytes()
    response = client.post(
        "/ingest",
        files={"file": ("CLG26_daily.parquet", payload, "application/octet-stream")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["frequency"] == "daily"
    assert body["ingest"]["profile"] == "hf_daily"


# --------------------------------------------------------------------------- #
# the four error paths
# --------------------------------------------------------------------------- #


def test_400_when_no_reader_claims_the_uploaded_format(client: TestClient) -> None:
    response = client.post("/ingest", files={"file": ("prices.xlsx", b"anything", "text/plain")})
    assert response.status_code == 400
    assert ".csv" in response.json()["detail"], "the message names the formats that would work"


def test_404_for_an_unknown_contract(client: TestClient) -> None:
    response = client.get("/bars?contract=ESH27")
    assert response.status_code == 404
    assert "ESH27" in response.json()["detail"]


def test_404_for_an_unknown_analytic(client: TestClient) -> None:
    response = client.get("/analytics/moving_average")
    assert response.status_code == 404
    assert "rolling_vwap" in response.json()["detail"], "the message names the ones that exist"


def test_404_for_an_unknown_check(client: TestClient) -> None:
    response = client.get("/quality/findings?check=invalid_olhc")
    assert response.status_code == 404
    assert "invalid_ohlc" in response.json()["detail"]


def test_413_for_an_upload_above_the_limit(tmp_path: Path) -> None:
    client = client_for(service_for(fixture_dir(tmp_path), max_upload_bytes=16))
    response = client.post("/ingest", files={"file": ("prices.csv", b"x" * 64, "text/csv")})
    assert response.status_code == 413
    assert "16 bytes" in response.json()["detail"]


def test_422_for_a_backwards_date_range(client: TestClient) -> None:
    response = client.get("/bars?start=2026-03-05&end=2026-03-01")
    assert response.status_code == 422
    assert "after end" in response.json()["detail"]


def test_422_for_an_analytic_parameter_the_model_refuses(client: TestClient) -> None:
    response = client.get("/analytics/rolling_vwap?window=every-so-often")
    assert response.status_code == 422
    assert "polars duration" in str(response.json()["detail"])


def test_422_for_a_parameter_no_analytic_declares(client: TestClient) -> None:
    """`extra='forbid'` on the analytic's own model, inherited by the generated route."""
    response = client.get("/analytics/rolling_vwap?windwo=15m")
    assert response.status_code == 422
    assert "windwo" in str(response.json()["detail"])


def test_422_when_an_analytic_is_asked_for_a_frequency_it_does_not_serve(
    client: TestClient,
) -> None:
    response = client.get("/analytics/rolling_vwap?frequency=daily")
    assert response.status_code == 422
    assert "does not support daily" in response.json()["detail"]


def test_422_for_an_unknown_severity(client: TestClient) -> None:
    response = client.get("/quality/findings?severity=CRITICAL")
    assert response.status_code == 422


def test_the_error_shape_is_documented(client: TestClient) -> None:
    responses = client.get("/openapi.json").json()["paths"]["/bars"]["get"]["responses"]
    assert set(responses) >= {"200", "400", "404", "413", "422"}
