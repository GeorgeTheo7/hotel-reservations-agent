import pytest

from anemos.db.connection import database_url

pytestmark = pytest.mark.unit


def test_database_url_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5433/db")
    assert database_url() == "postgresql://u:p@localhost:5433/db"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_database_url_missing_gives_clear_error(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", value)
    with pytest.raises(RuntimeError, match=r"DATABASE_URL is not set"):
        database_url()
