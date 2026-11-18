"""The owner's password and sessions (D82): settings, and the session table's queries.

A session is a random token in the browser's cookie; the database holds only its
SHA-256, with an absolute expiry 12 h after login (injected clock, D20). Logout deletes
the row, so a copied cookie stops working too.
"""

import hashlib
import hmac
import math
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import TupleRow

from anemos.audit import AuditEvent

type Conn = psycopg.Connection[TupleRow]

COOKIE_NAME = "anemos_session"
COOKIE_PATH = "/api"  # the browser sends it to the API only, never with the UI's files
SESSION_LIFETIME = timedelta(hours=12)
MIN_PASSWORD_LENGTH = 12
PASSWORD_ENV = "ANEMOS_OWNER_PASSWORD"
COOKIE_SECURE_ENV = "ANEMOS_COOKIE_SECURE"
LOCK_AFTER_FAILURES = 5
LOCK_WINDOW = timedelta(minutes=15)


@dataclass(frozen=True)
class OwnerAuth:
    password: str
    # Secure: the browser sends the cookie over HTTPS (and to http://127.0.0.1, which
    # browsers treat as secure); False only for a local browser that refuses (NOTES).
    cookie_secure: bool = True

    def __post_init__(self) -> None:
        if len(self.password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"{PASSWORD_ENV} must be at least {MIN_PASSWORD_LENGTH} characters")

    def __repr__(self) -> str:  # never print the password in a traceback or log
        return f"OwnerAuth(password=<hidden>, cookie_secure={self.cookie_secure})"


def auth_from_env() -> OwnerAuth:
    """ANEMOS_OWNER_PASSWORD (required, >= 12 characters) and ANEMOS_COOKIE_SECURE
    (`true` by default; `false` only for local development). The production app calls
    this at startup, so a missing password stops it from starting at all."""
    password = os.environ.get(PASSWORD_ENV, "")
    if not password.strip():
        raise ValueError(f"set {PASSWORD_ENV} (at least {MIN_PASSWORD_LENGTH} characters)")
    raw = os.environ.get(COOKIE_SECURE_ENV, "true").strip().lower()
    if raw not in ("true", "false"):
        raise ValueError(f"{COOKIE_SECURE_ENV} must be true or false, got {raw!r}")
    return OwnerAuth(password=password, cookie_secure=raw == "true")


def password_matches(given: str, expected: str) -> bool:
    """Constant time: both sides are hashed to the same length first, and
    compare_digest takes as long for a near miss as for a wild guess."""
    return hmac.compare_digest(
        hashlib.sha256(given.encode()).digest(), hashlib.sha256(expected.encode()).digest()
    )


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def create_session(conn: Conn, now: datetime) -> tuple[str, datetime]:
    """A new session in the caller's transaction: (token for the cookie, expires_at)."""
    token = secrets.token_urlsafe(32)
    expires_at = now + SESSION_LIFETIME
    conn.execute(
        "INSERT INTO owner_sessions (token_sha256, created_at, expires_at) VALUES (%s, %s, %s)",
        (token_hash(token), now, expires_at),
    )
    return token, expires_at


def session_is_live(conn: Conn, token: str | None, now: datetime) -> bool:
    """A stored, unexpired session; exactly at expires_at it is over."""
    if not token:
        return False
    row = conn.execute(
        "SELECT 1 FROM owner_sessions WHERE token_sha256 = %s AND expires_at > %s",
        (token_hash(token), now),
    ).fetchone()
    return row is not None


def delete_session(conn: Conn, token: str) -> None:
    conn.execute("DELETE FROM owner_sessions WHERE token_sha256 = %s", (token_hash(token),))


def login_locked_until(conn: Conn, now: datetime) -> datetime | None:
    """D82's throttle: with 5 failed logins in the last 15 minutes, login is locked
    until the oldest of those five is 15 minutes old (a failure exactly 15 minutes old
    no longer counts). Counted from the audit, so a restart does not reset it; only
    `owner.login_failed` counts, never a successful login or a refused (locked) one."""
    row = conn.execute(
        "SELECT at FROM audit_log WHERE event = %s AND at > %s"
        " ORDER BY at DESC, id DESC LIMIT 1 OFFSET %s",
        (AuditEvent.OWNER_LOGIN_FAILED.value, now - LOCK_WINDOW, LOCK_AFTER_FAILURES - 1),
    ).fetchone()
    if row is None:
        return None
    until: datetime = row[0] + LOCK_WINDOW
    return until


def retry_after_seconds(until: datetime, now: datetime) -> int:
    """Whole seconds for the Retry-After header, rounded up, at least 1."""
    return max(1, math.ceil((until - now).total_seconds()))
