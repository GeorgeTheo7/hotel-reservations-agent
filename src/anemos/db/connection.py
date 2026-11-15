"""Postgres connection (psycopg 3), configured by the DATABASE_URL environment variable.

Locally, load it from .env with: uv run --env-file .env <command>

Every connection gives up instead of waiting forever:
- lock_timeout (5 s): a statement waiting longer than this for a lock (a row lock, or a
  D61/D52 advisory lock) fails with LockNotAvailable. Locks in this app are held for
  milliseconds; a long wait means a lock was leaked (e.g. a transaction left open across
  a model call, D65), and failing loudly beats hanging every later run for that guest.
- statement_timeout (30 s): no single statement runs longer.
"""

import os

import psycopg
from psycopg.rows import TupleRow

# Seconds. Fail fast when the database is down or the host is wrong, instead of hanging.
CONNECT_TIMEOUT = 10
LOCK_TIMEOUT_MS = 5_000
STATEMENT_TIMEOUT_MS = 30_000


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and run with "
            "`uv run --env-file .env ...`."
        )
    return url


def connect(
    url: str | None = None,
    *,
    autocommit: bool = False,
    lock_timeout_ms: int = LOCK_TIMEOUT_MS,
    statement_timeout_ms: int = STATEMENT_TIMEOUT_MS,
) -> psycopg.Connection[TupleRow]:
    """Open a connection to `url` (URL or key=value conninfo), default DATABASE_URL.

    Not autocommit by default: statements run in a transaction until commit.
    autocommit=True is for statements Postgres refuses inside a transaction (CREATE DATABASE).
    The timeouts are session settings, sent with the connection request.
    """
    return psycopg.connect(
        url or database_url(),
        autocommit=autocommit,
        connect_timeout=CONNECT_TIMEOUT,
        options=f"-c lock_timeout={lock_timeout_ms} -c statement_timeout={statement_timeout_ms}",
    )
