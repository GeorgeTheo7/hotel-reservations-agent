"""The `committed_db` harness itself: real commits, isolation, reset, lock-wait detection.

The approval and race tests (later steps) rely on each of these properties.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg.rows import TupleRow

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # table names are constants
    assert row is not None
    return int(row[0])


def test_a_commit_is_visible_on_a_second_connection(committed_db: CommittedDb) -> None:
    writer, reader = committed_db.connect(), committed_db.connect()
    committed_db.rows(writer).email()
    assert count(reader, "emails") == 0  # not committed yet: invisible to others
    writer.commit()
    assert count(reader, "emails") == 1


def test_every_test_starts_empty(committed_db: CommittedDb) -> None:
    conn = committed_db.connect()
    assert {
        table: count(conn, table) for table in committed_db.OPERATIONAL_TABLES
    } == dict.fromkeys(committed_db.OPERATIONAL_TABLES, 0)


def test_reset_removes_even_frozen_rows_and_restarts_ids(committed_db: CommittedDb) -> None:
    """TRUNCATE skips the D51 row triggers, which refuse DELETE of final rows."""
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    rows.email(status="closed")  # final: DELETE would be refused
    rows.audit()  # write-once
    conn.commit()

    committed_db.reset()

    assert count(conn, "emails") == 0
    assert count(conn, "audit_log") == 0
    assert rows.email() == 1  # RESTART IDENTITY: ids start again at 1
    conn.rollback()


def test_reset_keeps_the_2027_seed(committed_db: CommittedDb) -> None:
    committed_db.reset()
    conn = committed_db.connect()
    assert count(conn, "rooms") == 14
    assert count(conn, "season_periods") > 0


def test_every_table_is_either_reset_or_seed(committed_db: CommittedDb) -> None:
    """A table added by a later migration must be put in one list, or its rows would
    leak from one committed test into the next."""
    conn = committed_db.connect()
    tables = {
        name
        for (name,) in conn.execute(
            "SELECT tablename FROM pg_tables"
            " WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
        ).fetchall()
    }
    assert tables == set(committed_db.OPERATIONAL_TABLES) | set(committed_db.SEED_TABLES)
    assert not set(committed_db.OPERATIONAL_TABLES) & set(committed_db.SEED_TABLES)


def test_separate_from_the_rolled_back_test_database(committed_db: CommittedDb, db: Conn) -> None:
    conn = committed_db.connect()
    committed_db.rows(conn).email(subject="only in the commit-test database")
    conn.commit()
    assert committed_db.conninfo != db.info.dsn
    assert conn.info.dbname.endswith("_commit_test")
    assert db.info.dbname.endswith("_test")
    assert db.info.dbname != conn.info.dbname
    assert count(db, "emails") == 0


def test_connections_are_real_not_autocommit(committed_db: CommittedDb) -> None:
    """Statements run in a transaction until commit, as in the application."""
    conn = committed_db.connect()
    assert conn.autocommit is False
    committed_db.rows(conn).email()
    conn.rollback()
    assert count(conn, "emails") == 0


def test_clock_starts_at_t0_and_is_per_test(committed_db: CommittedDb) -> None:
    assert committed_db.clock.now() == committed_db.T0


def test_wait_until_blocked_sees_a_lock_wait(committed_db: CommittedDb) -> None:
    """The pattern every race test uses: A holds a lock, B is started in a thread and
    blocks on it, the test waits until B is really blocked, then lets A finish."""
    setup = committed_db.connect()
    email_id = committed_db.rows(setup).email()
    setup.commit()

    holder, waiter = committed_db.connect(), committed_db.connect()
    holder.execute("SELECT id FROM emails WHERE id = %s FOR UPDATE", (email_id,))

    result: list[str] = []

    def take_the_same_lock() -> None:
        row = waiter.execute(
            "SELECT status FROM emails WHERE id = %s FOR UPDATE", (email_id,)
        ).fetchone()
        assert row is not None
        result.append(str(row[0]))
        waiter.commit()

    thread = threading.Thread(target=take_the_same_lock)
    thread.start()
    committed_db.wait_until_blocked(waiter)
    assert result == []  # still waiting: proven, not guessed with a sleep

    holder.execute("UPDATE emails SET status = 'processing' WHERE id = %s", (email_id,))
    holder.commit()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result == ["processing"]  # READ COMMITTED: B sees A's committed change


def test_wait_until_blocked_times_out_when_nothing_blocks(committed_db: CommittedDb) -> None:
    idle = committed_db.connect()
    idle.execute("SELECT 1")
    with pytest.raises(TimeoutError):
        committed_db.wait_until_blocked(idle, timeout_s=0.2)
