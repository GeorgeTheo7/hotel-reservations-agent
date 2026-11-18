"""The local-inbox CLI (python -m anemos.demo_inbox) on committed_db, and the fake
mailbox built from test-email cases: one format, one loader (docs/email-format.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from anemos.clock import FixedClock
from anemos.demo_inbox import fill
from anemos.email_cases import demo_cases, load_cases
from anemos.inbox import ingest
from anemos.mailbox import FakeMailbox

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb, OwnerApi

pytestmark = pytest.mark.integration

AT = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
EVAL_EMAILS = Path(__file__).resolve().parents[2] / "eval" / "emails"


def test_fills_an_inbox_and_a_second_run_stores_nothing(
    committed_db: CommittedDb, owner_api: OwnerApi
) -> None:
    report = fill(committed_db.connect, FixedClock(AT))
    assert [line.split(":")[0] for line in report] == [case.id for case in demo_cases()]
    assert all("skipped" not in line for line in report)

    with owner_api(AT) as api:
        inbox = api.get("/api/emails").json()["items"]
        routes = {
            item["subject"]: api.get(f"/api/emails/{item['id']}").json()["draft"]["route"]
            for item in inbox
        }
        set_aside = api.get("/api/emails", params={"status": "set_aside"}).json()["items"]
    # Each drafted case gets the route its label names (through the agent).
    labelled = {
        c.email.subject: c.labels.route for c in demo_cases() if c.labels.route != "set_aside"
    }
    assert routes == labelled
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
    # Every email went through the production run: extracted, routed in code, and the
    # five that are not spam drafted by the agent; every model call answered by the gold
    # client (named in the audit, at no cost).
    routed, calls, gold_calls, agents = conn.execute(
        "SELECT (SELECT count(*) FROM audit_log WHERE event = 'email.routed'),"
        " (SELECT count(*) FROM audit_log WHERE event = 'llm.called'),"
        " (SELECT count(*) FROM audit_log WHERE event = 'llm.called'"
        "  AND output->>'response_model' = 'gold-labels' AND cost_micro_usd = 0),"
        " (SELECT count(DISTINCT email_id) FROM audit_log WHERE event = 'llm.called'"
        "  AND step = 'agent')"
    ).fetchone()  # type: ignore[misc]
    conn.commit()
    assert (routed, agents) == (6, 5)
    assert calls == gold_calls > 6


def test_a_mailbox_of_eval_cases_is_stored_once(committed_db: CommittedDb) -> None:
    """The fake mailbox delivers the eval cases as the adapter would; ingest stores
    each once, with the case's pinned received_at, and a second fetch stores nothing."""
    cases = load_cases(EVAL_EMAILS)
    mailbox = FakeMailbox.from_cases(cases)
    first = ingest(committed_db.connect, FixedClock(AT), mailbox)
    assert [r.stored for r in first] == [True] * len(cases)
    second = ingest(committed_db.connect, FixedClock(AT), mailbox)
    assert [r.stored for r in second] == [False] * len(cases)
    conn = committed_db.connect()
    stored = conn.execute(
        "SELECT message_id, received_at, status FROM emails ORDER BY message_id"
    ).fetchall()
    assert stored == sorted((c.message_id, c.email.received_at, "received") for c in cases)
