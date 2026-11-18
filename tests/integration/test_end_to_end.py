"""One email end to end: stored, run by the graph (gold client: no network), waiting at
the interrupt, approved through the API, the thread resumed, the reply sent (simulated).
Every status on the way, and the audit trail in order, from email.stored to
outbox.sent."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from anemos.api import create_app
from anemos.audit import run_cost
from anemos.clock import FixedClock
from anemos.email_cases import load_cases
from anemos.gold import GoldClient
from anemos.graph import thread_snapshot
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, Settings
from anemos.llm.budget import Budget
from anemos.run import process_inbox
from anemos.sender import FakeTransport, send_due

if TYPE_CHECKING:
    from conftest import CommittedDb, EndedOnce

pytestmark = pytest.mark.integration

CASE = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}[
    "en-quote-sea-view-june"
]


def statuses(committed_db: CommittedDb, email_id: int) -> tuple[str, str | None, list[str]]:
    conn = committed_db.connect()
    email, draft = conn.execute(
        "SELECT e.status, d.status FROM emails e LEFT JOIN drafts d ON d.email_id = e.id"
        " WHERE e.id = %s", (email_id,),
    ).fetchone() or (None, None)  # fmt: skip
    bookings = [s for (s,) in conn.execute("SELECT status FROM bookings WHERE email_id = %s",
                                            (email_id,)).fetchall()]  # fmt: skip
    conn.commit()
    return email, draft, bookings


def test_an_email_from_arrival_to_the_sent_reply(
    committed_db: CommittedDb, tmp_path: Path, ended_once: EndedOnce
) -> None:
    clock = FixedClock(CASE.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, CASE.incoming()).email_id
    conn.commit()
    assert statuses(committed_db, email_id) == ("received", None, [])

    budget = Budget(clock, 1_000_000, tmp_path / "llm_spend.jsonl")
    llm = LLM(GoldClient([CASE]), Settings(model="gpt-6-luna", max_output_tokens=100), budget,
              committed_db.connect, clock, sleep=lambda s: None)  # fmt: skip
    [result] = process_inbox(committed_db.connect, clock, llm)
    assert result.outcome == "finished"
    assert statuses(committed_db, email_id) == ("awaiting_approval", "pending_approval", [])
    assert ended_once(email_id) == "draft.created"
    assert thread_snapshot(committed_db.connect, email_id).interrupts  # waits for the owner

    [draft_id] = conn.execute("SELECT id FROM drafts WHERE email_id = %s",
                              (email_id,)).fetchone()  # type: ignore[misc]  # fmt: skip
    conn.commit()
    with TestClient(create_app(committed_db.connect, clock)) as api:
        approved = api.post(f"/api/drafts/{draft_id}/approve", json={"version": 1})
    assert approved.status_code == 200
    assert statuses(committed_db, email_id) == ("answered", "approved", ["hold"])
    assert not thread_snapshot(committed_db.connect, email_id).interrupts  # resumed, ended

    transport = FakeTransport()
    report = send_due(committed_db.connect, clock, transport)
    assert len(report.sent) == 1 and len(transport.sent) == 1
    sent = transport.sent[0]
    assert (sent.to, sent.in_reply_to) == (CASE.incoming().from_address, CASE.incoming().message_id)

    query = "SELECT event FROM audit_log WHERE email_id = %s ORDER BY id"
    trail = [e for (e,) in conn.execute(query, (email_id,)).fetchall()]
    conn.commit()
    assert trail == [
        "email.stored", "email.processing", "llm.called", "email.routed",
        "llm.called", "agent.tool_called", "llm.called", "draft.created",
        "draft.approved", "run.finished", "outbox.sent",
    ]  # fmt: skip
    cost = run_cost(conn, email_id)
    assert set(cost.steps) == {"extract", "agent"} and cost.attempts == 3  # gold: no usage
