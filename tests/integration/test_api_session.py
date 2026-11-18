"""Owner login, the session check and logout over HTTP (D82), on committed_db.

Each test builds its own apps here (not owner_api, which logs in for you): the point
is what happens before and around a login. https base URL: a client never sends a
Secure cookie over plain http.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from anemos.api import create_app
from anemos.api.auth import COOKIE_NAME, OwnerAuth, token_hash
from anemos.api.security import CLIENT_HEADER, CLIENT_VALUE
from anemos.clock import FixedClock

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)
PASSWORD = "session-test-password"  # fictional, test-only
PROTECTED = "/api/holds"  # any route behind require_owner


@pytest.fixture
def make(committed_db: CommittedDb) -> Iterator[Any]:
    """make(at, secure=True, token=None): a client with no session, or with `token`
    as its cookie (the same browser at a later time)."""
    opened: list[TestClient] = []

    def build(at: datetime = T0, *, secure: bool = True, token: str | None = None) -> TestClient:
        app = create_app(committed_db.connect, FixedClock(at), OwnerAuth(PASSWORD, secure))
        client = TestClient(
            app, base_url="https://testserver", headers={CLIENT_HEADER: CLIENT_VALUE}
        )
        if token is not None:
            client.cookies.set(COOKIE_NAME, token)
        opened.append(client)
        return client

    yield build
    for client in opened:
        client.close()


def read(committed_db: CommittedDb, query: str) -> list[tuple[Any, ...]]:
    conn = committed_db.connect()
    found = conn.execute(query).fetchall()
    conn.commit()
    return found


def audit(committed_db: CommittedDb) -> list[tuple[Any, ...]]:
    return read(committed_db, "SELECT actor, event, email_id, draft_id, output FROM audit_log"
                " ORDER BY id")  # fmt: skip


def log_in(client: TestClient) -> str:
    response = client.post("/api/session", json={"password": PASSWORD})
    assert response.status_code == 204, response.text
    token = client.cookies.get(COOKIE_NAME)
    assert token
    return token


# --- without a session ------------------------------------------------------------------


def test_no_cookie_is_401(make: Any) -> None:
    for method, path in [("GET", PROTECTED), ("GET", "/api/session"), ("DELETE", "/api/session")]:
        response = make().request(method, path)
        assert (response.status_code, response.json()) == (401, {"error": "unauthenticated"})


def test_an_unknown_token_is_401(make: Any) -> None:
    assert make(token="made-up-token").get(PROTECTED).status_code == 401


# --- login --------------------------------------------------------------------------------


def test_a_wrong_password_is_401_audited_and_sets_no_cookie(
    committed_db: CommittedDb, make: Any
) -> None:
    client = make()
    response = client.post("/api/session", json={"password": PASSWORD + "x"})
    assert (response.status_code, response.json()) == (401, {"error": "wrong_password"})
    assert "set-cookie" not in response.headers
    assert read(committed_db, "SELECT count(*) FROM owner_sessions") == [(0,)]
    assert audit(committed_db) == [("system", "owner.login_failed", None, None, None)]
    assert client.get(PROTECTED).status_code == 401


def test_login_sets_a_strict_http_only_cookie_and_stores_only_its_hash(
    committed_db: CommittedDb, make: Any
) -> None:
    client = make()
    response = client.post("/api/session", json={"password": PASSWORD})
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    token = client.cookies.get(COOKIE_NAME)
    assert token and len(token) >= 43  # 32 random bytes, base64
    attributes = {part.strip().split("=")[0].lower() for part in cookie.split(";")[1:]}
    assert attributes == {"httponly", "max-age", "path", "samesite", "secure"}
    assert "Max-Age=43200" in cookie and "Path=/api" in cookie
    assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie

    stored = read(committed_db, "SELECT token_sha256, created_at, expires_at FROM owner_sessions")
    assert stored == [(token_hash(token), T0, T0 + timedelta(hours=12))]
    assert audit(committed_db) == [
        ("owner", "owner.login", None, None, {"expires_at": (T0 + timedelta(hours=12)).isoformat()})
    ]
    assert token not in str(audit(committed_db)) and PASSWORD not in str(audit(committed_db))

    assert client.get(PROTECTED).status_code == 200
    assert client.get("/api/session").json() == {"owner": True}


def test_without_secure_the_cookie_has_no_secure_attribute(make: Any) -> None:
    """ANEMOS_COOKIE_SECURE=false, for a local browser that refuses (NOTES)."""
    response = make(secure=False).post("/api/session", json={"password": PASSWORD})
    assert "secure" not in response.headers["set-cookie"].lower()


def test_each_login_is_its_own_session(committed_db: CommittedDb, make: Any) -> None:
    """A phone and a laptop: two tokens, both live."""
    first, second = log_in(make()), log_in(make())
    assert first != second
    assert make(token=first).get(PROTECTED).status_code == 200
    assert make(token=second).get(PROTECTED).status_code == 200


# --- expiry: 12 h, absolute ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("later", "status"),
    [(timedelta(hours=12) - timedelta(microseconds=1), 200), (timedelta(hours=12), 401)],
)
def test_a_session_ends_exactly_twelve_hours_after_login(
    make: Any, later: timedelta, status: int
) -> None:
    token = log_in(make(T0))
    assert make(T0 + later, token=token).get(PROTECTED).status_code == status


def test_using_a_session_does_not_extend_it(make: Any) -> None:
    token = log_in(make(T0))
    assert make(T0 + timedelta(hours=11), token=token).get(PROTECTED).status_code == 200
    assert make(T0 + timedelta(hours=12), token=token).get(PROTECTED).status_code == 401


# --- logout ------------------------------------------------------------------------------


def test_logout_deletes_the_session_so_a_copied_cookie_is_dead(
    committed_db: CommittedDb, make: Any
) -> None:
    client = make()
    token = log_in(client)
    other = log_in(make())

    response = client.delete("/api/session")
    assert response.status_code == 204
    assert 'anemos_session=""' in response.headers["set-cookie"] or (
        "Max-Age=0" in response.headers["set-cookie"]
    )
    assert make(token=token).get(PROTECTED).status_code == 401  # the copy
    assert make(token=other).get(PROTECTED).status_code == 200  # the other browser stays
    assert read(committed_db, "SELECT token_sha256 FROM owner_sessions") == [(token_hash(other),)]
    assert [event for _, event, *_ in audit(committed_db)] == [
        "owner.login", "owner.login", "owner.logout",
    ]  # fmt: skip


# --- the login lock: 5 failures within 15 minutes (D82) ---------------------------------


def fail(make: Any, at: datetime) -> int:
    return int(make(at).post("/api/session", json={"password": "wrong-password!"}).status_code)


def test_the_sixth_failed_login_within_the_window_is_429(
    committed_db: CommittedDb, make: Any
) -> None:
    assert [fail(make, T0 + timedelta(minutes=m)) for m in range(5)] == [401] * 5
    client = make(T0 + timedelta(minutes=5))
    locked = client.post("/api/session", json={"password": PASSWORD})  # even the right one
    assert (locked.status_code, locked.json()) == (429, {"error": "login_locked"})
    assert locked.headers["Retry-After"] == "600"  # the first failure leaves at T0 + 15 min
    assert "set-cookie" not in locked.headers
    assert read(committed_db, "SELECT count(*) FROM owner_sessions") == [(0,)]
    events = [event for _, event, *_ in audit(committed_db)]
    assert events == ["owner.login_failed"] * 5  # a locked attempt writes nothing


@pytest.mark.parametrize(
    ("later", "status"),
    [(timedelta(minutes=15) - timedelta(microseconds=1), 429), (timedelta(minutes=15), 204)],
)
def test_login_works_again_once_the_window_has_passed(
    make: Any, later: timedelta, status: int
) -> None:
    """Five failures at T0: locked until exactly T0 + 15 min, then open again."""
    assert [fail(make, T0) for _ in range(5)] == [401] * 5
    response = make(T0 + later).post("/api/session", json={"password": PASSWORD})
    assert response.status_code == status
    if status == 429:
        assert response.headers["Retry-After"] == "1"  # rounded up, never 0


def test_four_failures_do_not_lock(make: Any) -> None:
    assert [fail(make, T0) for _ in range(4)] == [401] * 4
    log_in(make(T0))


def test_successful_logins_are_not_counted(make: Any) -> None:
    """Five logins and four failures in the window: still open."""
    for _ in range(5):
        log_in(make(T0))
    assert [fail(make, T0) for _ in range(4)] == [401] * 4
    log_in(make(T0))


# --- headers on a real answer ------------------------------------------------------------


def test_security_headers_on_a_logged_in_read(make: Any) -> None:
    client = make()
    log_in(client)
    response = client.get(PROTECTED)
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
