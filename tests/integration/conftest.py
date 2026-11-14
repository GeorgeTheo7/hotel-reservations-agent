"""Integration test harness.

- Once per test session: drop and recreate a throwaway database `<dev db>_test`,
  migrate it to head and load the 2027 seed data. The dev database is never touched.
- Per test: the `db` fixture is a connection inside an outer transaction that is
  always rolled back, so tests cannot leave data behind for each other.
- Inside that transaction, `db.commit()` raises, and `with db.transaction():` becomes a
  SAVEPOINT (used by tests that expect a constraint violation).
- `committed_db` is the second harness, for tests that need real commits and several
  connections at once (the approval transaction, races). It uses its own database
  `<dev db>_commit_test`, so leftovers can never reach the rolled-back tests; the
  operational tables are truncated before and after every test, the 2027 seed stays.

Run with: uv run --env-file .env pytest -m integration
"""

import itertools
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.clock import FixedClock
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

    def quote(self, request_id: int, **overrides: Any) -> int:
        line = {"night": "2027-07-12", "season": "mid", "room_rate_cents": 13000}
        return self.insert(
            "quotes",
            {
                "request_id": request_id,
                "room_type_id": "sea_view",
                "stay": self.STAY,
                "rooms": 1,
                "lines": Jsonb([line, line, line]),
                "extra_bed_bands": [],
                "room_subtotal_cents": 39000,
                "extra_bed_subtotal_cents": 0,
                "accommodation_subtotal_cents": 39000,
                "city_tax_cents": 1200,
                "deposit_cents": 11700,
                "free_cancellation_until": date(2027, 6, 28),
                "changes": [],
                "created_at": self.T0,
            }
            | overrides,
        )

    def draft(self, email_id: int, request_id: int | None, **overrides: Any) -> int:
        return self.insert(
            "drafts",
            {
                "email_id": email_id,
                "request_id": request_id,
                "route": "quote",
                "status": "pending_approval",
                "created_at": self.T0,
            }
            | overrides,
        )

    def version(self, draft_id: int, version: int = 1, **overrides: Any) -> int:
        return self.insert(
            "draft_versions",
            {
                "draft_id": draft_id,
                "version": version,
                "author": "agent" if version == 1 else "owner",
                "body": "Dear guest, {{QUOTE_BLOCK}}",
                "guardrail_results": Jsonb({"quote_block_once": True}),
                "created_at": self.T0,
            }
            | overrides,
            returning="version",
        )

    def offer(
        self, draft_id: int, request_id: int, quote_id: int, *, hold: bool, **overrides: Any
    ) -> None:
        allowed = self.db.execute(
            "SELECT hold_allowed FROM quotes WHERE id = %s", (quote_id,)
        ).fetchone()
        self.insert(
            "draft_offers",
            {
                "draft_id": draft_id,
                "request_id": request_id,
                "quote_id": quote_id,
                "quote_hold_allowed": allowed[0] if allowed else False,
                "hold": hold,
                "rendered_block": f"Our offer: quote {quote_id}\nAccommodation total: €390.00",
            }
            | overrides,
            returning="draft_id",
        )

    def outbox(self, draft_id: int, **overrides: Any) -> int:
        return self.insert(
            "outbox",
            {
                "draft_id": draft_id,
                "to_address": "guest@example.test",
                "subject": "Re: Room in July",
                "body": "Dear guest, here is our offer.",
                "status": "pending",
                "created_at": self.T0,
            }
            | overrides,
        )

    def booking(self, **overrides: Any) -> int:
        """A confirmed booking in the lowest-numbered room, no email/request/draft."""
        return self.insert(
            "bookings",
            {
                "room_id": self.db.execute("SELECT min(id) FROM rooms").fetchone()[0],  # type: ignore[index]
                "stay": self.STAY,
                "status": "confirmed",
                "price_cents": 39000,
                "created_at": self.T0,
            }
            | overrides,
        )

    def audit(self, **overrides: Any) -> int:
        return self.insert(
            "audit_log",
            {"at": self.T0, "actor": "owner", "event": "draft.approved"} | overrides,
        )

    def approved_draft(self) -> tuple[int, int, int]:
        """(email id, request id, draft id) of an approved quote-route draft."""
        email_id = self.email(status="answered")
        request_id = self.request(email_id)
        draft_id = self.draft(
            email_id, request_id, status="approved", decided_at=self.T0 + timedelta(hours=1)
        )
        return email_id, request_id, draft_id

    def check_deferred_now(self, constraint: str) -> None:
        """Run a DEFERRABLE constraint's pending check now instead of at COMMIT (the
        rollback fixture never commits)."""
        self.db.execute(sql.SQL("SET CONSTRAINTS {} IMMEDIATE").format(sql.Identifier(constraint)))


@pytest.fixture(scope="session")
def commit_test_db() -> str:
    """Conninfo of `<dev db>_commit_test`, migrated to head and seeded, once per session."""
    conninfo = recreate_database(f"{dev_database_name()}_commit_test")
    upgrade(conninfo)
    with connect(conninfo) as conn:
        seed(conn)
    return conninfo


# Every table the system writes while running; inventory and seed tables are kept.
# Listed explicitly (no CASCADE): a new table that references one of these makes the
# TRUNCATE fail loudly, and a harness test checks this list covers every non-seed table.
OPERATIONAL_TABLES = (
    "audit_log", "outbox", "draft_offers", "draft_versions", "drafts", "quotes",
    "requests", "emails", "bookings",
)  # fmt: skip
SEED_TABLES = (
    "room_types", "rooms", "seasons", "season_periods", "room_rates", "min_stay_rules",
    "extra_bed_rates",
)  # fmt: skip

LOCK_WAIT_TIMEOUT_S = 5.0


class CommittedDb:
    """Real connections to the commit-test database; every commit is real.

    `connect()` opens a new connection (closed after the test). `reset()` empties the
    operational tables: TRUNCATE skips row triggers, so even frozen rows (D51) go.
    `wait_until_blocked(conn)` returns once `conn` is waiting for a lock held by
    another transaction, so a race test knows for certain where each side stands.
    """

    T0 = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)
    OPERATIONAL_TABLES = OPERATIONAL_TABLES
    SEED_TABLES = SEED_TABLES

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        self.clock = FixedClock(self.T0)
        self._conns: list[Conn] = []

    def connect(self) -> Conn:
        conn = connect(self.conninfo)
        self._conns.append(conn)
        return conn

    def rows(self, conn: Conn) -> "Rows":
        """The same row helpers as the `rows` fixture, on a committed connection."""
        return Rows(conn)

    def reset(self) -> None:
        with connect(self.conninfo, autocommit=True) as admin:
            admin.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY").format(
                    sql.SQL(", ").join(map(sql.Identifier, OPERATIONAL_TABLES))
                )
            )

    def wait_until_blocked(self, conn: Conn, timeout_s: float = LOCK_WAIT_TIMEOUT_S) -> None:
        """Poll pg_stat_activity until `conn`'s server process waits on a lock.

        Read `conn.info.backend_pid` here, before `conn` is handed to another thread:
        a connection that is blocked cannot be asked anything itself.
        """
        pid = conn.info.backend_pid
        deadline = time.monotonic() + timeout_s
        with connect(self.conninfo, autocommit=True) as admin:
            while time.monotonic() < deadline:
                row = admin.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
                ).fetchone()
                if row is not None and row[0] == "Lock":
                    return
                time.sleep(0.01)
        raise TimeoutError(f"connection {pid} did not block on a lock within {timeout_s} s")

    def close_all(self) -> None:
        for conn in self._conns:
            conn.close()
        self._conns.clear()


@pytest.fixture
def committed_db(commit_test_db: str) -> Iterator[CommittedDb]:
    harness = CommittedDb(commit_test_db)
    harness.reset()  # in case an earlier run stopped before its teardown
    try:
        yield harness
    finally:
        harness.close_all()
        harness.reset()


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
