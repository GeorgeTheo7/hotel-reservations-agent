"""Tests of the test harness itself: rollback isolation and savepoints."""

from collections.abc import Callable
from contextlib import AbstractContextManager

import psycopg
import pytest
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
