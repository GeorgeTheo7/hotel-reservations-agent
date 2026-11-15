import psycopg
import pytest

from anemos.db.connection import connect

pytestmark = pytest.mark.integration


def test_select_one() -> None:
    with connect() as conn:
        row = conn.execute("SELECT 1").fetchone()
    assert row == (1,)


def test_server_is_postgres_17() -> None:
    with connect() as conn:
        assert conn.info.server_version // 10000 == 17


def test_every_connection_gives_up_on_locks_and_long_statements() -> None:
    """A leaked lock must fail the next run fast, not hang it (D65)."""
    with connect() as conn:
        settings = conn.execute(
            "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
        ).fetchone()
    assert settings == ("5s", "30s")


def test_a_held_lock_fails_the_waiter_instead_of_hanging_it() -> None:
    """The behaviour behind the setting, with a short timeout so the test is quick: a
    second connection waiting for a lock the first holds gets LockNotAvailable."""
    with connect() as holder, connect(lock_timeout_ms=200) as waiter:
        holder.execute("SELECT pg_advisory_xact_lock(424242)")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            waiter.execute("SELECT pg_advisory_xact_lock(424242)")
        holder.rollback()


def test_a_long_statement_is_cancelled() -> None:
    with connect(statement_timeout_ms=200) as conn, pytest.raises(psycopg.errors.QueryCanceled):
        conn.execute("SELECT pg_sleep(2)")
