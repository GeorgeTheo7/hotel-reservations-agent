"""D82's CSRF guard and D83's security headers, no database: the guard answers before
any connection is opened (no_database would turn a leak into a 500), and the headers
are on every kind of response, the 500 handler's included."""

from datetime import UTC, datetime
from typing import NoReturn

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from anemos.api import create_app
from anemos.api.auth import OwnerAuth
from anemos.api.deps import require_owner
from anemos.api.security import CLIENT_HEADER, CLIENT_VALUE, SECURITY_HEADERS
from anemos.clock import FixedClock

pytestmark = pytest.mark.unit

UI = {CLIENT_HEADER: CLIENT_VALUE}
CLOCK = FixedClock(datetime(2026, 11, 13, 7, 0, tzinfo=UTC))
CHANGES = [("POST", "/api/session"), ("DELETE", "/api/session"),
           ("POST", "/api/drafts/1/approve"), ("POST", "/api/bookings/1/confirm"),
           ("POST", "/api/nowhere")]  # fmt: skip


def no_database() -> NoReturn:
    raise AssertionError("this test must not open a connection")


def deny() -> NoReturn:
    raise HTTPException(status_code=401)


def app() -> FastAPI:
    application = create_app(no_database, CLOCK, OwnerAuth(password="unit-test-password"))
    application.dependency_overrides[require_owner] = deny  # past the guard: 401, no DB
    return application


def client() -> TestClient:
    return TestClient(app(), base_url="https://testserver", raise_server_exceptions=False)


# --- the CSRF guard ----------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), CHANGES)
def test_a_change_without_the_client_header_is_403(method: str, path: str) -> None:
    """Login and a path that does not exist included: the guard runs before routing."""
    response = client().request(method, path)
    assert (response.status_code, response.json()) == (403, {"error": "csrf"})


@pytest.mark.parametrize("value", ["", "Web", "curl", "web "])
def test_only_the_exact_header_value_passes(value: str) -> None:
    response = client().delete("/api/session", headers={CLIENT_HEADER: value})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "http://testserver", "https://testserver:8443", "null"]
)
def test_a_foreign_origin_is_403_even_with_the_header(origin: str) -> None:
    for method, path in CHANGES:
        response = client().request(method, path, headers=UI | {"Origin": origin})
        assert (response.status_code, response.json()) == (403, {"error": "csrf"}), path


def test_the_header_and_our_own_origin_pass_the_guard() -> None:
    """Past the guard the (overridden) owner check answers: 401, not 403."""
    for headers in (UI, UI | {"Origin": "https://testserver"}):
        assert client().delete("/api/session", headers=headers).status_code == 401
        assert client().post("/api/drafts/1/approve", headers=headers).status_code == 401


def test_reads_need_no_header() -> None:
    assert client().get("/api/holds").status_code == 401  # the owner check, not the guard


# --- headers on every response --------------------------------------------------------


def boom() -> NoReturn:
    raise RuntimeError("a bug")


def answers() -> dict[str, int]:
    application = app()
    application.get("/api/boom", response_model=None)(boom)
    test_client = TestClient(application, base_url="https://testserver",
                             raise_server_exceptions=False)  # fmt: skip
    responses = {
        "csrf": test_client.post("/api/session"),
        "unauthenticated": test_client.get("/api/holds"),
        "not_found": test_client.get("/api/nowhere"),
        "ui_path": test_client.get("/inbox"),
        "internal": test_client.get("/api/boom"),
    }
    for name, response in responses.items():
        for header, value in SECURITY_HEADERS.items():
            assert response.headers.get(header) == value, (name, header)
    return {name: r.status_code for name, r in responses.items()}


def test_security_headers_on_every_kind_of_response() -> None:
    assert answers() == {"csrf": 403, "unauthenticated": 401, "not_found": 404,
                         "ui_path": 404, "internal": 500}  # fmt: skip


def test_the_csp_forbids_framing_and_foreign_scripts() -> None:
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "default-src 'self'" in csp
    assert "unsafe-inline" not in csp
