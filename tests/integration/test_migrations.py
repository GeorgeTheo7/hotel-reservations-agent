from collections.abc import Callable

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.db.connection import connect
from anemos.db.migrate import downgrade, head_revision, upgrade

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

EXTENSIONS = "SELECT extname FROM pg_extension WHERE extname IN ('btree_gist', 'vector') ORDER BY 1"
APP_TABLES = (
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    " AND tablename <> 'alembic_version' ORDER BY 1"
)
INVENTORY_TABLES = [
    ("bookings",),
    ("min_stay_rules",),
    ("room_rates",),
    ("room_types",),
    ("rooms",),
    ("season_periods",),
    ("seasons",),
]


def test_test_db_is_at_head(db: Conn) -> None:
    assert db.execute("SELECT version_num FROM alembic_version").fetchall() == [(head_revision(),)]


def test_extensions_installed(db: Conn) -> None:
    assert db.execute(EXTENSIONS).fetchall() == [("btree_gist",), ("vector",)]


def test_upgrade_downgrade_upgrade_on_fresh_database(
    make_test_database: Callable[[str], str],
) -> None:
    """Every migration's downgrade() undoes its upgrade(), so the chain can be replayed."""
    conninfo = make_test_database("anemos_migrations_test")
    upgrade(conninfo)
    downgrade(conninfo)
    with connect(conninfo) as conn:
        assert conn.execute(EXTENSIONS).fetchall() == []
        assert conn.execute(APP_TABLES).fetchall() == []
        assert conn.execute("SELECT count(*) FROM alembic_version").fetchone() == (0,)
    upgrade(conninfo)
    with connect(conninfo) as conn:
        assert conn.execute(EXTENSIONS).fetchall() == [("btree_gist",), ("vector",)]
        assert conn.execute(APP_TABLES).fetchall() == INVENTORY_TABLES
