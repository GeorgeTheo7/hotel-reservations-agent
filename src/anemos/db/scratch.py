"""Throwaway databases for tests and evaluations: never the dev database.

A scratch database is named `<dev db>_..._test`, dropped and created again, migrated to
head and seeded (2027). Between cases only the operational tables are emptied; the seed
stays. Used by the integration-test harness (tests/integration/conftest.py) and the
extraction eval (eval/extraction_eval.py), which runs every case on its own (docs/
email-format.md: each file is one email on its own).
"""

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import TupleRow

from anemos.checkpoints import CHECKPOINT_TABLES
from anemos.checkpoints import SCHEMA as CHECKPOINT_SCHEMA
from anemos.db.connection import connect, database_url
from anemos.db.migrate import upgrade
from anemos.db.seed import seed

type Conn = psycopg.Connection[TupleRow]

# Every table the system writes while running; inventory and seed tables are kept.
# policy_chunks is seed data (D15): written by `seed`, never by a run, so it stays.
# Listed explicitly (no CASCADE): a new table that references one of these makes the
# TRUNCATE fail loudly, and a harness test checks this list covers every non-seed table.
OPERATIONAL_TABLES = (
    "audit_log", "outbox", "draft_offers", "draft_versions", "drafts", "quotes",
    "requests", "emails", "bookings", "owner_sessions",
)  # fmt: skip
SEED_TABLES = (
    "room_types", "rooms", "seasons", "season_periods", "room_rates", "min_stay_rules",
    "extra_bed_rates", "policy_chunks",
)  # fmt: skip


def dev_database_name() -> str:
    return str(conninfo_to_dict(database_url())["dbname"])


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


def prepare(suffix: str) -> str:
    """`<dev db>_<suffix>_test`, recreated, migrated and seeded; returns its conninfo."""
    conninfo = recreate_database(f"{dev_database_name()}_{suffix}_test")
    upgrade(conninfo)
    with connect(conninfo) as conn:
        seed(conn)
    return conninfo


def truncate(conninfo: str) -> None:
    """Empty the operational tables and the checkpoint tables (D75: a thread is keyed
    by the email id, which RESTART IDENTITY reuses). TRUNCATE skips row triggers, so
    even frozen rows (D51) go; RESTART IDENTITY starts the ids again."""
    tables = [sql.Identifier(t) for t in OPERATIONAL_TABLES]
    tables += [sql.Identifier(CHECKPOINT_SCHEMA, t) for t in CHECKPOINT_TABLES]
    with connect(conninfo, autocommit=True) as admin:
        admin.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(sql.SQL(", ").join(tables)))


def is_empty(conn: Conn) -> bool:
    """No row in any operational table."""
    for table in OPERATIONAL_TABLES:
        row = conn.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.Identifier(table))
        ).fetchone()
        if row is not None and row[0]:
            return False
    return True
