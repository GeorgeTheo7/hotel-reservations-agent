"""The API resumes a run's thread after the owner's action has committed (D75).

The draft comes from a real run (the gold client), so its thread is waiting at the
interrupt. A failed resume never fails the owner's request: it is logged and audited
(`run.resume_failed`), and the thread stays interrupted for the week-10 tidy-up.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from anemos.api import actions
from anemos.clock import FixedClock
from anemos.email_cases import load_cases
from anemos.extraction_run import process_email
from anemos.gold import GoldClient
from anemos.graph import thread_snapshot
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, Settings
from anemos.llm.budget import Budget
from anemos.run import agent_finish

if TYPE_CHECKING:
    from conftest import CommittedDb, OwnerApi

pytestmark = pytest.mark.integration

CASE = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}[
    "en-quote-sea-view-june"
]
CLOCK = FixedClock(CASE.email.received_at)


def drafted_by_a_run(committed_db: CommittedDb, tmp: Path) -> tuple[int, int]:
    conn = committed_db.connect()
    email_id = store_email(conn, CLOCK, CASE.incoming()).email_id
    conn.commit()
    budget = Budget(CLOCK, 1_000_000, tmp / "llm_spend.jsonl")
    llm = LLM(GoldClient([CASE]), Settings(model="gpt-6-luna", max_output_tokens=100), budget,
              committed_db.connect, CLOCK, sleep=lambda s: None)  # fmt: skip
    process_email(committed_db.connect, CLOCK, llm, email_id, finish=agent_finish(llm))
    row = conn.execute("SELECT id FROM drafts WHERE email_id = %s", (email_id,)).fetchone()
    conn.commit()
    assert row is not None
    return email_id, int(row[0])


def audit(committed_db: CommittedDb, email_id: int, event: str) -> list[Any]:
    conn = committed_db.connect()
    rows = conn.execute(
        "SELECT actor, output FROM audit_log WHERE email_id = %s AND event = %s ORDER BY id",
        (email_id, event),
    ).fetchall()
    conn.commit()
    return rows


def test_api_approve_resumes_the_thread(
    committed_db: CommittedDb, owner_api: OwnerApi, tmp_path: Path
) -> None:
    email_id, draft_id = drafted_by_a_run(committed_db, tmp_path)
    with owner_api(CLOCK) as api:
        response = api.post(f"/api/drafts/{draft_id}/approve", json={"version": 1})
    assert response.status_code == 200
    assert audit(committed_db, email_id, "run.finished") == [
        ("system", {"draft_status": "approved"})
    ]
    assert not thread_snapshot(committed_db.connect, email_id).interrupts


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_api_a_failed_resume_still_answers_success_and_is_audited(
    committed_db: CommittedDb,
    owner_api: OwnerApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    email_id, draft_id = drafted_by_a_run(committed_db, tmp_path)

    def broken(*_: object) -> bool:
        raise RuntimeError("checkpointer unreachable")

    monkeypatch.setattr(actions, "resume_after_owner", broken)
    body = {"version": 1} if action == "approve" else None
    with owner_api(CLOCK) as api:
        response = api.post(f"/api/drafts/{draft_id}/{action}", json=body)
    assert response.status_code == 200  # the owner's action stands
    expected = "approved" if action == "approve" else "rejected"
    conn = committed_db.connect()
    assert conn.execute("SELECT status FROM drafts WHERE id = %s",
                        (draft_id,)).fetchone() == (expected,)  # fmt: skip
    conn.commit()
    assert audit(committed_db, email_id, "run.resume_failed") == [
        ("system", {"error": "RuntimeError: checkpointer unreachable"})
    ]
    assert audit(committed_db, email_id, "run.finished") == []
    assert thread_snapshot(committed_db.connect, email_id).interrupts  # left for week 10
