"""Integration test harness.

- Once per test session: drop and recreate a throwaway database `<dev db>_test`,
  then migrate it to head. The dev database is never touched.
- Per test: the `db` fixture is a connection inside an outer transaction that is
  always rolled back, so tests cannot leave data behind for each other.
- Inside that transaction, `db.commit()` raises, and `with db.transaction():` becomes a
  SAVEPOINT (used by tests that expect a constraint violation).

Run with: uv run --env-file .env pytest -m integration
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import TupleRow

from anemos.db.connection import connect, database_url
from anemos.db.migrate import upgrade

type Conn = psycopg.Connection[TupleRow]


def recreate_database(name: str) -> str:
    """Drop and create database `name`; return its conninfo. Only `*_test` names allowed."""
    if not name.endswith("_test"):
        raise ValueError(f"refusing to recreate {name!r}: test database names end in _test")
    with connect(autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return make_conninfo(database_url(), dbname=name)


def dev_database_name() -> str:
    return str(conninfo_to_dict(database_url())["dbname"])


@pytest.fixture(scope="session")
def make_test_database() -> Callable[[str], str]:
    """For tests that need their own empty database (e.g. replaying migrations)."""
    return recreate_database


@pytest.fixture(scope="session")
def test_db() -> str:
    """Conninfo of the session's test database, migrated to head. Left in place after the
    run so a failure can be inspected; recreated at the start of the next run."""
    conninfo = recreate_database(f"{dev_database_name()}_test")
    upgrade(conninfo)
    return conninfo


@contextmanager
def _rolled_back(conninfo: str) -> Iterator[Conn]:
    with connect(conninfo) as conn, conn.transaction(force_rollback=True):
        yield conn


@pytest.fixture(scope="session")
def open_rolled_back(test_db: str) -> Callable[[], AbstractContextManager[Conn]]:
    """Opens a connection whose work is always rolled back. `db` is built on it."""
    return lambda: _rolled_back(test_db)


@pytest.fixture
def db(open_rolled_back: Callable[[], AbstractContextManager[Conn]]) -> Iterator[Conn]:
    with open_rolled_back() as conn:
        yield conn
