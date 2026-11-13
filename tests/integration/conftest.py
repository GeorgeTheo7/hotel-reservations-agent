"""Integration test harness.

- Once per test session: drop and recreate a throwaway database `<dev db>_test`,
  migrate it to head and load the 2027 seed data. The dev database is never touched.
- Per test: the `db` fixture is a connection inside an outer transaction that is
  always rolled back, so tests cannot leave data behind for each other.
- Inside that transaction, `db.commit()` raises, and `with db.transaction():` becomes a
  SAVEPOINT (used by tests that expect a constraint violation).

Run with: uv run --env-file .env pytest -m integration
"""

import itertools
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.db.connection import connect, database_url
from anemos.db.migrate import upgrade
from anemos.db.seed import seed

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
    """Conninfo of the session's test database, migrated to head and seeded (2027).
    Left in place after the run so a failure can be inspected; recreated next run."""
    conninfo = recreate_database(f"{dev_database_name()}_test")
    upgrade(conninfo)
    with connect(conninfo) as conn:
        seed(conn)
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


class Rows:
    """Inserts valid rows with defaults; tests override only the column under test.

    Every insert goes through `insert`, which takes column names from the keyword
    arguments, so a test can pass a bad value (or None) for any column.
    """

    T0 = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)
    STAY = "[2027-07-12,2027-07-15)"  # 3 nights

    def __init__(self, db: Conn) -> None:
        self.db = db
        self._message_ids = itertools.count(1)

    def insert(self, table: str, columns: dict[str, Any], returning: str = "id") -> int:
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.SQL(", ").join(sql.Placeholder(name) for name in columns),
            sql.Identifier(returning),
        )
        row = self.db.execute(query, columns).fetchone()
        assert row is not None
        return int(row[0])

    def email(self, **overrides: Any) -> int:
        return self.insert(
            "emails",
            {
                "message_id": f"<test-{next(self._message_ids)}@example.test>",
                "from_address": "guest@example.test",
                "subject": "Room in July",
                "raw_body": "Hello, do you have a room?",
                "clean_body": "Hello, do you have a room?",
                "received_at": self.T0,
                "created_at": self.T0,
                "status": "received",
            }
            | overrides,
        )

    def request(self, email_id: int | None = None, **overrides: Any) -> int:
        return self.insert(
            "requests",
            {
                "email_id": self.email() if email_id is None else email_id,
                "extracted": Jsonb({"date_spec": {"kind": "exact"}}),
                "created_at": self.T0,
            }
            | overrides,
        )


@pytest.fixture
def rows(db: Conn) -> Rows:
    return Rows(db)


type ExpectViolation = Callable[[type[psycopg.Error], str], AbstractContextManager[None]]


@pytest.fixture
def expect_violation(db: Conn) -> ExpectViolation:
    """`with expect_violation(ExclusionViolation, "constraint_name"): db.execute(...)`

    Runs the block in a SAVEPOINT, so the test's transaction stays usable afterwards,
    and checks both the error class and the name of the constraint that fired.
    """

    @contextmanager
    def check(error: type[psycopg.Error], constraint: str) -> Iterator[None]:
        with pytest.raises(error) as caught, db.transaction():
            yield
        assert caught.value.diag.constraint_name == constraint

    return check
