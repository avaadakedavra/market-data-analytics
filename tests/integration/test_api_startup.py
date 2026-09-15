"""Starting the app for real: the lifespan, the environment, and the module-level `app`.

Every other API test injects a service. These do not — they exercise the path
`uvicorn mdq.api.app:app` actually takes, which is the one place `MDQ_DATA_DIR` is read
and the only code a misconfigured deployment would hit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mdq.api.app import app, create_app
from support.api import PARQUET_FIXTURES, fixture_dir

pytestmark = pytest.mark.integration


def test_startup_autoloads_the_configured_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MDQ_DATA_DIR` is read at startup, and the files are registered, not read."""
    monkeypatch.setenv("MDQ_DATA_DIR", str(fixture_dir(tmp_path, *PARQUET_FIXTURES)))
    with TestClient(create_app()) as client:
        body = client.get("/health").json()
        assert body["datasets"] == ["autoload"]
        assert body["files"] == 3
        assert body["in_memory"] == [], "startup must not materialise anything"
        assert client.get("/contracts").json()["contract_count"] == 2


def test_startup_against_a_missing_directory_still_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running `make api` before `make fetch` gives an empty service, not a stack trace."""
    monkeypatch.setenv("MDQ_DATA_DIR", str(tmp_path / "not-fetched-yet"))
    with TestClient(create_app()) as client:
        body = client.get("/health").json()
        assert body["datasets"] == []
        assert body["frequencies"] == []
        assert client.get("/bars").json()["rows"] == []


def test_an_injected_service_survives_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test's own service must not be replaced by whatever the environment says."""
    monkeypatch.setenv("MDQ_DATA_DIR", str(fixture_dir(tmp_path, *PARQUET_FIXTURES)))
    from support.api import service_for

    injected = service_for(fixture_dir(tmp_path / "other", "ESH26_daily.parquet"))
    with TestClient(create_app(injected)) as client:
        assert client.get("/health").json()["files"] == 1
        assert client.app.state.service is injected


def test_the_module_level_app_is_the_one_uvicorn_serves() -> None:
    """`uvicorn mdq.api.app:app` has to find a fully built application at import time."""
    assert app.title.startswith("Market Data Quality")
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    assert {
        "/health",
        "/datasets/summary",
        "/contracts",
        "/ingest",
        "/bars",
        "/analytics",
        "/analytics/rolling_vwap",
        "/quality/summary",
        "/quality/findings",
        "/insights",
    } <= set(paths)
