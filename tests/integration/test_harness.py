"""Tests of the test harness itself: rollback isolation and savepoints."""

from collections.abc import Callable
from contextlib import AbstractContextManager

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow

from anemos.db.connection import connect

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]


def test_writes_are_rolled_back(
    open_rolled_back: Callable[[], AbstractContextManager[Conn]], test_db: str
) -> None:
    with open_rolled_back() as conn:
        conn.execute("CREATE TABLE leak_check (id int)")
        conn.execute("INSERT INTO leak_check VALUES (1)")
    with connect(test_db) as other:
        assert other.execute("SELECT to_regclass('leak_check')").fetchone() == (None,)


def test_commit_is_forbidden_inside_a_test(db: Conn) -> None:
    with pytest.raises(psycopg.ProgrammingError):
        db.commit()


def test_expected_violation_inside_savepoint_keeps_transaction_usable(db: Conn) -> None:
    db.execute("CREATE TABLE price (cents int CHECK (cents >= 0))")
    db.execute("INSERT INTO price VALUES (100)")

    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():  # = SAVEPOINT
        db.execute("INSERT INTO price VALUES (-1)")

    # Rolled back to the savepoint only: earlier work survives, new queries still run.
    assert db.execute("SELECT cents FROM price").fetchall() == [(100,)]


def test_without_savepoint_the_whole_transaction_is_aborted(db: Conn) -> None:
    """Why the savepoint is needed: after an error, Postgres refuses every further query."""
    db.execute("CREATE TABLE price (cents int CHECK (cents >= 0))")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO price VALUES (-1)")
    with pytest.raises(psycopg.errors.InFailedSqlTransaction):
        db.execute("SELECT 1")


def test_every_db_test_gets_the_same_connection_and_a_clean_transaction(
    db: Conn, session_conn: Conn
) -> None:
    assert db is session_conn
    assert db.info.transaction_status is TransactionStatus.INTRANS
    db.execute("CREATE TABLE only_in_this_test (id int)")  # gone after the test


def test_the_table_of_the_previous_test_is_gone(db: Conn) -> None:
    assert db.execute("SELECT to_regclass('only_in_this_test')").fetchone() == (None,)


def test_a_raw_commit_under_the_rolled_back_fixture_is_caught(
    test_db: str, rolled_back_check: Callable[[Conn], None]
) -> None:
    """psycopg refuses db.commit(); a raw COMMIT gets past it, so the fixture checks the
    connection is still inside its transaction after every test. (No write here: the
    COMMIT really ends the transaction.)"""
    with connect(test_db) as conn, conn.transaction(force_rollback=True):
        rolled_back_check(conn)  # inside: fine
        conn.execute("COMMIT")
        with pytest.raises(AssertionError, match="leak data between tests"):
            rolled_back_check(conn)


def test_advisory_locks_end_with_each_test(db: Conn, test_db: str) -> None:
    """A transaction-level advisory lock (D61) taken in a test is released when the
    test's transaction ends; another connection can then take it at once."""
    db.execute("SELECT pg_advisory_xact_lock(424242)")
    with connect(test_db) as other:
        assert other.execute("SELECT pg_try_advisory_xact_lock(424242)").fetchone() == (False,)


def test_the_advisory_lock_of_the_previous_test_is_released(db: Conn, test_db: str) -> None:
    with connect(test_db) as other:
        assert other.execute("SELECT pg_try_advisory_xact_lock(424242)").fetchone() == (True,)
