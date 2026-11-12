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
