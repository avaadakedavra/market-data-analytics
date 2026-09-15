"""Helpers shared by the service and API tests.

Two things the WP6 tests keep needing and that are easy to get subtly wrong:

* **a fixture directory holding only the files a test is about.** `tests/fixtures` mixes
  real vendor parquet with hand-authored broken CSVs, and the CSVs carry ESH26 minute bars
  at made-up prices. Autoloading the whole directory therefore merges those prices into
  ESH26's real minute series, which is fine for exercising the pipeline and fatal for any
  test comparing against the vendor. `fixture_dir` builds a directory containing exactly
  the files asked for.
* **a service and a client over it**, so no test has to remember to call `autoload`.

Nothing here touches the network or `data/raw`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from mdq.api.app import create_app
from mdq.domain.config import AppSettings
from mdq.service import MarketDataService
from support.fixtures import FIXTURES_DIR

__all__ = [
    "PARQUET_FIXTURES",
    "client_for",
    "fixture_dir",
    "service_for",
]

#: The three committed vendor slices — real data, no hand-made defects.
PARQUET_FIXTURES: tuple[str, ...] = (
    "CLG26_daily.parquet",
    "ESH26_daily.parquet",
    "ESH26_minute_2026-03-02_to_03-13.parquet",
)


def fixture_dir(tmp_path: Path, *names: str) -> Path:
    """A directory holding copies of the named files from `tests/fixtures`."""
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(FIXTURES_DIR / name, directory / name)
    return directory


def service_for(directory: Path, **settings: object) -> MarketDataService:
    """A service pointed at `directory`, already autoloaded."""
    service = MarketDataService(AppSettings(data_dir=directory, **settings))
    service.autoload()
    return service


def client_for(service: MarketDataService) -> TestClient:
    """A `TestClient` over an app wired to `service`."""
    return TestClient(create_app(service))
