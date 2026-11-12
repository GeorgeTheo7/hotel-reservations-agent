"""Postgres connection (psycopg 3), configured by the DATABASE_URL environment variable.

Locally, load it from .env with: uv run --env-file .env <command>
"""

import os

import psycopg
from psycopg.rows import TupleRow

# Seconds. Fail fast when the database is down or the host is wrong, instead of hanging.
CONNECT_TIMEOUT = 10


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and run with "
            "`uv run --env-file .env ...`."
        )
    return url


def connect(url: str | None = None, *, autocommit: bool = False) -> psycopg.Connection[TupleRow]:
    """Open a connection to `url` (URL or key=value conninfo), default DATABASE_URL.

    Not autocommit by default: statements run in a transaction until commit.
    autocommit=True is for statements Postgres refuses inside a transaction (CREATE DATABASE).
    """
    return psycopg.connect(
        url or database_url(), autocommit=autocommit, connect_timeout=CONNECT_TIMEOUT
    )
