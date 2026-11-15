"""The local-inbox CLI (python -m anemos.demo_inbox) on committed_db: it fills an inbox
the API can show, and a second run stores nothing twice."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from anemos.api import create_app
from anemos.clock import FixedClock
from anemos.demo_inbox import fill, samples

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

AT = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)


def test_every_sample_is_fictional() -> None:
    """All data is fictional (CLAUDE.md): reserved example.test addresses only."""
    for sample in samples():
        assert sample.sender.endswith("@example.test>"), sample.key


def test_fills_an_inbox_and_a_second_run_stores_nothing(committed_db: CommittedDb) -> None:
    report = fill(committed_db.connect, FixedClock(AT))
    assert [line.split(":")[0] for line in report] == [s.key for s in samples()]
    assert all("skipped" not in line for line in report)

    with TestClient(create_app(committed_db.connect, FixedClock(AT))) as api:
        inbox = api.get("/emails").json()["items"]
        routes = {
            item["subject"]: api.get(f"/emails/{item['id']}").json()["draft"]["route"]
            for item in inbox
        }
        set_aside = api.get("/emails", params={"status": "set_aside"}).json()["items"]
    assert sorted(routes.values()) == ["ask_info", "decline", "quote", "quote", "quote"]
    assert {item["status"] for item in inbox} == {"awaiting_approval"}
    assert [item["subject"] for item in set_aside] == ["You have won a free cruise"]

    again = fill(committed_db.connect, FixedClock(AT))
    assert all("already stored" in line for line in again)
    conn = committed_db.connect()
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM emails), (SELECT count(*) FROM requests),"
        " (SELECT count(*) FROM drafts)"
    ).fetchone()
    assert counts == (6, 5, 5)
