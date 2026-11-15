"""The API's wiring, no database: the owner hook on every route, the error handlers,
no pricing code in api/, the local-only entry point."""

import logging
import re
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, NoReturn

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from anemos.api import create_app
from anemos.api.__main__ import HOST, main
from anemos.api.deps import require_owner
from anemos.clock import FixedClock
from anemos.states import NotFound

pytestmark = pytest.mark.unit

API = Path(str(files("anemos"))) / "api"


def no_database() -> NoReturn:
    raise AssertionError("this test must not open a connection")


def app() -> FastAPI:
    return create_app(no_database, FixedClock(datetime(2026, 11, 13, 7, 0, tzinfo=UTC)))


# --- D19 hook ------------------------------------------------------------------------


def deny() -> NoReturn:
    raise HTTPException(status_code=401)


def test_every_route_requires_the_owner() -> None:
    """Week 7 replaces require_owner with the password check; a route added without
    the hook would then be open. With the hook replaced by one that refuses, every
    path in the API description answers 401, before any connection is opened
    (no_database would turn that into a 500).

    It only sees routes in the OpenAPI schema: a route declared with
    include_in_schema=False must be added to `calls` by hand."""
    application = app()
    application.dependency_overrides[require_owner] = deny
    client = TestClient(application, raise_server_exceptions=False)
    calls = [
        (method.upper(), re.sub(r"\{[^}]+\}", "1", path))
        for path, operations in application.openapi()["paths"].items()
        for method in operations
    ]
    assert len(calls) >= 3, calls
    answers = {call: client.request(*call).status_code for call in calls}
    assert answers == dict.fromkeys(calls, 401)


# --- error handlers --------------------------------------------------------------------


def client_with(route: Any) -> TestClient:
    application = app()
    application.get("/boom", response_model=None)(route)
    # The 500 handler answers, then Starlette re-raises for the server log; the test
    # client must not turn that into a test error.
    return TestClient(application, raise_server_exceptions=False)


def test_not_found_is_404() -> None:
    def boom() -> NoReturn:
        raise NotFound("no email 7")

    response = client_with(boom).get("/boom")
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


def test_a_plain_lookup_error_is_a_bug_not_404() -> None:
    """approval.py raises LookupError for rows that should exist (a draft without a
    version): that is a 500, not "not found"."""

    def boom() -> NoReturn:
        raise LookupError("draft 3 has no version")

    response = client_with(boom).get("/boom")
    assert (response.status_code, response.json()) == (500, {"error": "internal"})


def test_an_unhandled_error_is_logged_and_hidden(caplog: pytest.LogCaptureFixture) -> None:
    def boom() -> NoReturn:
        raise RuntimeError("secret detail")

    with caplog.at_level(logging.ERROR, logger="anemos.api"):
        response = client_with(boom).get("/boom")
    assert (response.status_code, response.json()) == (500, {"error": "internal"})
    assert "secret detail" not in response.text
    [record] = [r for r in caplog.records if r.name == "anemos.api"]
    assert record.exc_info is not None  # the full traceback, server side
    assert "GET /boom" in record.getMessage()


# --- boundaries ------------------------------------------------------------------------


def test_the_api_never_imports_pricing_or_date_code() -> None:
    """The API returns what the database and the rendered blocks hold; it computes no
    price and no date, so it has no reason to import anemos.reservations."""
    pattern = re.compile(r"^\s*(from|import)\s+anemos(\.reservations\b|\s+import\s+reservations)")
    found = [
        f"{path.name}:{n}"
        for path in sorted(API.glob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.match(line)
    ]
    assert found == []


def test_the_entry_point_binds_this_machine_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """D19: no password until week 7, so `python -m anemos.api` never listens on other
    interfaces."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kw: calls.append(kw))
    main(["--port", "8123"])
    assert HOST == "127.0.0.1"
    assert calls == [{"factory": True, "host": "127.0.0.1", "port": 8123}]
