"""LangGraph checkpoints in Postgres (D17, D75).

The checkpointer writes on **its own autocommit connection** (D71): never inside a run
transaction, and it holds no lock on our tables. Its tables live in their own schema,
`langgraph`, created by `PostgresSaver.setup()` from our migrate command
(`anemos.db.migrate.upgrade`), not copied into an Alembic migration, which would drift
from the library's own schema on upgrades. A separate schema keeps them apart from the
application tables (the week-9 read-only role is not granted it: checkpoints hold
transcripts with email text).

The serializer allows no Python class in a checkpoint (`allowed_msgpack_modules=None`):
the graph state is plain JSON values only (D75), so nothing in the database can make a
load construct an object of ours, or anyone's.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import DictRow, TupleRow, dict_row

from anemos.db.connection import connect

type Conn = psycopg.Connection[TupleRow]

SCHEMA = "langgraph"
# Emptied by the test harness's reset with the operational tables; checkpoint_migrations
# is not: it records which of the library's migrations have run.
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _saver(conn: psycopg.Connection[DictRow]) -> PostgresSaver:
    return PostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=None))


def _own_connection(conn: Conn) -> psycopg.Connection[DictRow]:
    """Turn a fresh connection into the checkpointer's: autocommit, dict rows, and the
    `langgraph` schema first on the search path."""
    conn.autocommit = True
    conn.execute(f"SET search_path TO {SCHEMA}")
    conn.row_factory = cast(Any, dict_row)
    return cast(psycopg.Connection[DictRow], conn)


@contextmanager
def checkpointer(connect_: Callable[[], Conn]) -> Iterator[PostgresSaver]:
    """A checkpointer on a new connection from `connect_`, closed afterwards."""
    conn = _own_connection(connect_())
    try:
        yield _saver(conn)
    finally:
        conn.close()


def setup_checkpoints(conninfo: str | None = None) -> None:
    """Create the schema and let the library create or upgrade its tables (idempotent)."""
    with connect(conninfo, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    with checkpointer(lambda: connect(conninfo)) as saver:
        saver.setup()
