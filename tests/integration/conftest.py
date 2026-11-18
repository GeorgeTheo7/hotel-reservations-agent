"""Integration test harness.

- Once per test session: drop and recreate a throwaway database `<dev db>_test`,
  migrate it to head and load the 2027 seed data. The dev database is never touched.
- Per test: the `db` fixture is the session's one connection (`session_conn`) inside
  a transaction that is always rolled back, so tests cannot leave data behind for each
  other. One connection, not one per test: per-test connections exhausted local ports
  on Windows (TIME_WAIT). A commit on it fails the test loudly.
- Inside that transaction, `db.commit()` raises, and `with db.transaction():` becomes a
  SAVEPOINT (used by tests that expect a constraint violation).
- `committed_db` is the second harness, for tests that need real commits and several
  connections at once (the approval transaction, races). It uses its own database
  `<dev db>_commit_test`, so leftovers can never reach the rolled-back tests; the
  operational tables are truncated before and after every test, the 2027 seed stays.
- API audit coverage: before truncating, `committed_db` records which audit events a
  test in a `test_api_*.py` module committed. `API_COVERAGE_TEST` (moved to the end of
  the run) then checks every event in audit.API_ONLY_EVENTS was written by one of them.

Run with: uv run --env-file .env pytest -m integration
"""

import itertools
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.api import create_app
from anemos.api.auth import COOKIE_NAME, OwnerAuth, create_session
from anemos.clock import Clock, FixedClock
from anemos.db.connection import connect
from anemos.db.migrate import upgrade
from anemos.db.scratch import (
    OPERATIONAL_TABLES,
    SEED_TABLES,
    dev_database_name,
    recreate_database,
    truncate,
)
from anemos.db.seed import seed

type Conn = psycopg.Connection[TupleRow]


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


@pytest.fixture(scope="session")
def session_conn(test_db: str) -> Iterator[Conn]:
    """The one connection every `db` test uses. A new connection per test left
    thousands of closed sockets holding local ports (TCP TIME_WAIT), and on Windows a
    connect now and then failed with "Address already in use" (NOTES 2026-11-16)."""
    conn = connect(test_db)
    try:
        yield conn
    finally:
        conn.close()


class CommitUnderRolledBack(AssertionError):
    """Something committed on the rolled-back `db` connection: its writes may have
    leaked into the database every later test sees."""


def assert_still_rolled_back(conn: Conn) -> None:
    """The connection must still be inside the test's transaction. psycopg refuses
    `commit()` inside it; this also catches a raw `COMMIT`/`ROLLBACK` statement."""
    status = conn.info.transaction_status
    if status not in (TransactionStatus.INTRANS, TransactionStatus.INERROR):
        raise CommitUnderRolledBack(
            f"the db connection left its transaction (status {status.name}): a commit "
            "under the rolled-back fixture would leak data between tests"
        )


@pytest.fixture
def rolled_back_check() -> Callable[[Conn], None]:
    """The check `db` runs after every test, for the harness test that proves it works."""
    return assert_still_rolled_back


@pytest.fixture
def db(session_conn: Conn) -> Iterator[Conn]:
    """`session_conn` inside a transaction that is always rolled back: one top-level
    transaction per test, not a savepoint in one long transaction, so advisory locks
    (D52, D61: transaction-level) and row locks end with each test, as they would on a
    fresh connection. `db.commit()` raises; `with db.transaction():` is a SAVEPOINT."""
    conn = session_conn
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        conn.rollback()  # an earlier test broke out of its transaction; start clean
    with conn.transaction(force_rollback=True):
        yield conn
        assert_still_rolled_back(conn)


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
                "extracted": Jsonb({"date_spec": None}),  # no dates given
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
        truncate(self.conninfo)

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


# --- API audit coverage --------------------------------------------------------------

API_TEST_PREFIX = "test_api_"
API_COVERAGE_TEST = "test_every_api_only_event_is_written_by_an_api_test"


class ApiAuditCoverage:
    """Audit events committed by API tests in this run, and whether every API test ran
    (a run with -k or a single file cannot prove coverage, so the check skips)."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.complete = False


API_AUDIT = ApiAuditCoverage()
_deselected_api_tests: list[str] = []


def _is_api_test(item: pytest.Item) -> bool:
    """An integration API test. tests/unit/test_api_app.py has the same prefix but
    writes no audit rows, and `-m integration` deselects it, so it must not count."""
    return item.path.parent == Path(__file__).parent and item.path.name.startswith(API_TEST_PREFIX)


def pytest_deselected(items: list[pytest.Item]) -> None:
    _deselected_api_tests.extend(i.nodeid for i in items if _is_api_test(i))


@pytest.hookimpl(trylast=True)  # after -k / -m deselection
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Move the coverage test to the end, and note whether every API test is selected:
    no API test deselected, and every test_api_*.py file collected."""
    guard = [i for i in items if i.name == API_COVERAGE_TEST]
    for item in guard:
        items.remove(item)
        items.append(item)
    on_disk = {p.name for p in Path(__file__).parent.glob(f"{API_TEST_PREFIX}*.py")}
    collected = {i.path.name for i in items if _is_api_test(i)}
    API_AUDIT.complete = bool(guard) and collected == on_disk and not _deselected_api_tests


@pytest.fixture
def api_audit() -> ApiAuditCoverage:
    """The run's API audit coverage (tests/ is not a package, so tests reach it here)."""
    return API_AUDIT


@pytest.fixture
def committed_db(commit_test_db: str, request: pytest.FixtureRequest) -> Iterator[CommittedDb]:
    harness = CommittedDb(commit_test_db)
    harness.reset()  # in case an earlier run stopped before its teardown
    try:
        yield harness
    finally:
        harness.close_all()
        if request.node.path.name.startswith(API_TEST_PREFIX):
            with connect(commit_test_db) as conn:
                found = conn.execute("SELECT DISTINCT event FROM audit_log").fetchall()
            API_AUDIT.seen.update(event for (event,) in found)
        harness.reset()


# D82: the owner password every integration test's app uses. Fictional, test-only.
TEST_AUTH = OwnerAuth(password="test-owner-password")

type OwnerApi = Callable[..., TestClient]


@pytest.fixture
def owner_api(committed_db: CommittedDb) -> Iterator[OwnerApi]:
    """owner_api(clock_or_time, raise_server_exceptions=True): a TestClient on the
    commit-test database with a live owner session, so every request passes the real
    require_owner check. The session is created directly (`create_session`), not by
    POST /api/session: a login writes an `owner.login` audit row, and many tests
    compare the audit before and after an action. Login itself is tested in
    test_api_session.py. https: a client never sends a Secure cookie over plain http.
    Each call is a separate app (its own clock); all closed at teardown."""
    opened: list[TestClient] = []

    def make(at: Clock | datetime, *, raise_server_exceptions: bool = True) -> TestClient:
        clock = FixedClock(at) if isinstance(at, datetime) else at
        client = TestClient(
            create_app(committed_db.connect, clock, TEST_AUTH),
            base_url="https://testserver",
            raise_server_exceptions=raise_server_exceptions,
        )
        opened.append(client)
        conn = committed_db.connect()
        token, _ = create_session(conn, clock.now())
        conn.commit()
        client.cookies.set(COOKIE_NAME, token)
        return client

    yield make
    for client in opened:
        client.close()


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


# --- a run's ending (audit.TERMINAL_EVENTS) ---------------------------------------------

type EndedOnce = Callable[[int], str]


@pytest.fixture
def ended_once(committed_db: CommittedDb) -> EndedOnce:
    """Check a finished run: exactly one terminal audit event (escalated, set aside,
    draft created or discarded), the email not left `processing`, and no model-call row
    in the database without an email (every cost is attributed). Returns the event."""
    from anemos.audit import TERMINAL_EVENTS

    def check(email_id: int) -> str:
        with connect(committed_db.conninfo) as conn:
            events = [e for (e,) in conn.execute(
                "SELECT event FROM audit_log WHERE email_id = %s AND event = ANY(%s)"
                " ORDER BY id", (email_id, [e.value for e in TERMINAL_EVENTS]),
            ).fetchall()]  # fmt: skip
            status = conn.execute("SELECT status FROM emails WHERE id = %s",
                                  (email_id,)).fetchone()  # fmt: skip
            orphans = conn.execute(
                "SELECT count(*) FROM audit_log WHERE event LIKE 'llm.%%' AND email_id IS NULL"
            ).fetchone()
        assert len(events) == 1, f"email {email_id}: terminal events {events}"
        assert status is not None and status[0] != "processing", status
        assert orphans == (0,), "model calls without an email"
        return str(events[0])

    return check
