"""Helpers for the WP7 tests: two backends over the same data, and an `AppTest` harness.

The point of this module is the `both_backends` fixture idea made concrete. `EmbeddedBackend`
and `HttpBackend` are supposed to be indistinguishable to a page, and the only way to keep
them that way is to run the *same* assertions over both — so every test that can be
parametrised over a backend is, and the two are also compared to each other directly.

`HttpBackend` is given a `TestClient`, which is itself an `httpx.Client`: the requests are
real HTTP requests through the real routers and the real response models, with no server
socket to start. Nothing here touches the network or `data/raw`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from mdq.api.app import create_app
from mdq.dashboard.backend import BackendError, EmbeddedBackend, HttpBackend
from mdq.service import MarketDataService
from support.api import fixture_dir, service_for

__all__ = [
    "PAGE_TIMEOUT",
    "FailingBackend",
    "backend_of",
    "embedded_backend",
    "http_backend",
    "page_test",
    "service_dir",
    "upload_test",
]

#: Streamlit's own default is 3 seconds, which the first plotly import alone can exceed on
#: a cold interpreter. Generous on purpose: a timeout here is a flake, never a finding.
PAGE_TIMEOUT = 120.0


def service_dir(tmp_path: Path, *names: str) -> Path:
    """A directory holding copies of the named committed fixtures."""
    return fixture_dir(tmp_path, *names)


def embedded_backend(service: MarketDataService) -> EmbeddedBackend:
    """An in-process backend over `service`."""
    return EmbeddedBackend(service, label="embedded-under-test")


def http_backend(service: MarketDataService) -> HttpBackend:
    """An HTTP backend whose transport is a `TestClient` over the real FastAPI app."""
    return HttpBackend("http://testserver", client=TestClient(create_app(service)))


def backend_of(kind: str, directory: Path) -> EmbeddedBackend | HttpBackend:
    """Either backend, over a service autoloaded from `directory`.

    Each backend gets its *own* service so that one backend's memoised quality report can
    never be mistaken for the other's answer — the whole point is to prove the two compute
    the same thing independently.
    """
    service = service_for(directory)
    return embedded_backend(service) if kind == "embedded" else http_backend(service)


class FailingBackend:
    """A backend that cannot answer anything — the API is down, or the service is broken.

    Every page must survive this: a non-engineer looking at the screen should read one
    sentence about what went wrong, in the place the answer would have been, rather than a
    Streamlit traceback quoting httpx.
    """

    label = "a backend that is not answering"

    def _fail(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise BackendError("the service is not answering")

    summary = contracts = bars = analytic = _fail
    quality_summary = findings = insights = activity = upload = _fail


def _page_script(module_name, backend):
    """The script `AppTest` runs: import one page module and render it.

    Deliberately annotation-free and self-contained. `AppTest.from_function` takes this
    function's *source*, execs it in a fresh module and calls it — so anything the
    signature or body names must be resolvable there, and a `-> None` referring to a
    `Backend` imported in this file would be a `NameError` inside the script runner rather
    than a failure anyone could read.
    """
    import importlib

    importlib.import_module(module_name).render(backend)


def _upload_script(backend, filename, payload):
    """The script that renders an upload report, as the sidebar's button would.

    `st.file_uploader` cannot be driven by `AppTest`, so the report is produced the way the
    button produces it and handed to the same renderer the sidebar uses — which is the part
    worth testing.
    """
    import streamlit as st

    from mdq.dashboard import sidebar

    with st.sidebar:
        sidebar.report_card(backend.upload(filename, payload))


def page_test(module_name: str, backend: Any) -> AppTest:
    """Render one page in a Streamlit script runner and return the result.

    Pages are plain `render(backend)` functions, so this is the whole harness: no script
    file, no navigation, no session to fake. `AppTest` still catches an exception raised
    anywhere in the page, which is what makes these smoke tests worth having.
    """
    return AppTest.from_function(
        _page_script,
        default_timeout=PAGE_TIMEOUT,
        kwargs={"module_name": module_name, "backend": backend},
    ).run()


def upload_test(backend: Any, filename: str, payload: bytes) -> AppTest:
    """Drive the sidebar's upload report as though a user had dropped a file on it."""
    return AppTest.from_function(
        _upload_script,
        default_timeout=PAGE_TIMEOUT,
        kwargs={"backend": backend, "filename": filename, "payload": payload},
    ).run()
