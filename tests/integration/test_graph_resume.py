"""Checkpoints, interrupt and resume (D75), on a committed database with the gold client.

A run "stops" when the model client raises `Stop` (a BaseException, as a killed
process would end the run: no node catches it). The run is then resumed from its last
checkpoint with a fresh client that records every call it gets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import TupleRow
from pydantic import BaseModel

from anemos.agent import evidence
from anemos.approval import Approved, approve_draft
from anemos.clock import FixedClock
from anemos.db.scratch import truncate
from anemos.email_cases import EmailCase, load_cases
from anemos.extraction_run import process_email
from anemos.gold import GoldClient
from anemos.graph import resume_after_owner, resume_run, thread_snapshot
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, Prompt, Reply, Settings, Tool, Transcript, TurnReply
from anemos.llm.budget import Budget
from anemos.owner_actions import reject_draft
from anemos.run import agent_finish

if TYPE_CHECKING:
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

CASES = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=100)
QUOTE_CASE = "en-quote-sea-view-june"  # extraction, quote_price, write_draft
POLICY_CASE = "el-pets-policy"  # extraction, search_policies (+ selector), write_draft


class Stop(BaseException):
    """The process dies: nothing in the run catches it."""


@dataclass
class Recording:
    """The gold client, recording each call by name; raises Stop at call `stop_at`."""

    gold: GoldClient
    stop_at: int | None = None
    calls: list[str] = field(default_factory=list)

    def _count(self, name: str) -> None:
        self.calls.append(name)
        if self.stop_at is not None and len(self.calls) == self.stop_at:
            raise Stop(f"stopped at call {self.stop_at} ({name})")

    def send[T: BaseModel](self, prompt: Prompt, schema: type[T], settings: Settings) -> Reply[T]:
        self._count(schema.__name__)
        return self.gold.send(prompt, schema, settings)

    def send_turn(self, transcript: Transcript, tools: Any, settings: Settings) -> TurnReply:
        self._count("turn")
        return self.gold.send_turn(transcript, tuple[Tool, ...](tools), settings)


def llm_for(committed_db: CommittedDb, client: Recording, clock: FixedClock, tmp: Path) -> LLM:
    budget = Budget(clock, 1_000_000, tmp / "llm_spend.jsonl")
    return LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)


def store(committed_db: CommittedDb, case: EmailCase) -> tuple[int, FixedClock]:
    clock = FixedClock(case.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, case.incoming()).email_id
    conn.commit()
    return email_id, clock


def one(committed_db: CommittedDb, query: str, *params: Any) -> Any:
    conn = committed_db.connect()
    row = conn.execute(query, params).fetchone()
    conn.commit()
    return row


def run(committed_db: CommittedDb, case_id: str, tmp: Path,
        stop_at: int | None = None) -> tuple[int, FixedClock, Recording]:  # fmt: skip
    case = CASES[case_id]
    email_id, clock = store(committed_db, case)
    client = Recording(GoldClient([case]), stop_at)
    llm = llm_for(committed_db, client, clock, tmp)
    if stop_at is None:
        process_email(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    else:
        with pytest.raises(Stop):
            process_email(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    return email_id, clock, client


def resume(committed_db: CommittedDb, case_id: str, email_id: int, clock: FixedClock,
           tmp: Path) -> tuple[Any, Recording]:  # fmt: skip
    client = Recording(GoldClient([CASES[case_id]]))
    llm = llm_for(committed_db, client, clock, tmp)
    result = resume_run(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    return result, client


def email_status(committed_db: CommittedDb, email_id: int) -> tuple[str, list[str] | None]:
    status, codes = one(committed_db, "SELECT status, reason_codes FROM emails WHERE id = %s",
                        email_id)  # fmt: skip
    return status, codes


def draft_id_of(committed_db: CommittedDb, email_id: int) -> int:
    return int(one(committed_db, "SELECT id FROM drafts WHERE email_id = %s", email_id)[0])


def finished_rows(committed_db: CommittedDb, email_id: int) -> list[Any]:
    conn = committed_db.connect()
    rows = conn.execute(
        "SELECT output FROM audit_log WHERE email_id = %s AND event = 'run.finished'",
        (email_id,),
    ).fetchall()
    conn.commit()
    return [r[0] for r in rows]


# --- resume after a stop -----------------------------------------------------------------------


def test_a_run_stopped_mid_loop_resumes_without_repeating_model_calls(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    email_id, clock, first = run(committed_db, QUOTE_CASE, tmp_path, stop_at=3)
    assert first.calls == ["ExtractionOutput", "turn", "turn"]  # stopped in the 2nd turn
    assert email_status(committed_db, email_id)[0] == "processing"
    snapshot = thread_snapshot(committed_db.connect, email_id)
    assert snapshot.next == ("agent_turn",)
    result, again = resume(committed_db, QUOTE_CASE, email_id, clock, tmp_path)
    assert again.calls == ["turn"]  # only the turn that never finished: no extraction again
    assert result is not None and result.outcome == "finished"
    assert email_status(committed_db, email_id)[0] == "awaiting_approval"
    body = one(committed_db, "SELECT v.body FROM draft_versions v JOIN drafts d"
               " ON d.id = v.draft_id WHERE d.email_id = %s", email_id)[0]  # fmt: skip
    assert "€" in body and "{{QUOTE_BLOCK}}" not in body  # rendered from the rebuilt quote
    assert one(committed_db, "SELECT count(*) FROM quotes")[0] == 1  # nothing priced again
    assert thread_snapshot(committed_db.connect, email_id).interrupts  # waits for the owner


def test_a_resume_with_a_changed_policies_file_ends_system_error(
    committed_db: CommittedDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    email_id, clock, first = run(committed_db, POLICY_CASE, tmp_path, stop_at=4)
    assert first.calls == ["ExtractionOutput", "turn", "PolicySelection", "turn"]
    monkeypatch.setattr(evidence, "policies_sha256", lambda: "0" * 64)
    result, again = resume(committed_db, POLICY_CASE, email_id, clock, tmp_path)
    assert again.calls == ["EscalationSummary"]  # no agent turn; only the summary (D81)
    assert result is not None
    assert email_status(committed_db, email_id) == ("escalated", ["SYSTEM_ERROR"])
    detail = one(committed_db, "SELECT output ->> 'detail' FROM audit_log WHERE email_id = %s"
                 " AND event = 'email.escalated'", email_id)[0]  # fmt: skip
    assert "resume refused: policies.md changed" in detail


def test_a_resume_after_a_price_change_ends_system_error(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    email_id, clock, _ = run(committed_db, QUOTE_CASE, tmp_path, stop_at=3)
    conn = committed_db.connect()
    conn.execute("UPDATE room_rates SET rate_cents = rate_cents + 100")
    conn.commit()
    try:
        result, again = resume(committed_db, QUOTE_CASE, email_id, clock, tmp_path)
    finally:
        conn.execute("UPDATE room_rates SET rate_cents = rate_cents - 100")
        conn.commit()
    assert again.calls == ["EscalationSummary"] and result is not None  # no agent turn (D81)
    assert email_status(committed_db, email_id) == ("escalated", ["SYSTEM_ERROR"])
    detail = one(committed_db, "SELECT output ->> 'detail' FROM audit_log WHERE email_id = %s"
                 " AND event = 'email.escalated'", email_id)[0]  # fmt: skip
    assert "resume refused: quote" in detail and "price changed" in detail


def test_the_database_wins_a_resume_does_nothing_if_the_email_moved_on(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    email_id, clock, _ = run(committed_db, QUOTE_CASE, tmp_path, stop_at=3)
    conn = committed_db.connect()
    conn.execute("UPDATE emails SET status = 'escalated', reason_codes = '{SYSTEM_ERROR}'"
                 " WHERE id = %s", (email_id,))  # fmt: skip
    conn.commit()
    result, again = resume(committed_db, QUOTE_CASE, email_id, clock, tmp_path)
    assert result is None and again.calls == []
    assert one(committed_db, "SELECT count(*) FROM drafts")[0] == 0


def test_a_finished_or_waiting_run_is_not_resumed(committed_db: CommittedDb,
                                                 tmp_path: Path) -> None:  # fmt: skip
    email_id, clock, _ = run(committed_db, QUOTE_CASE, tmp_path)
    result, again = resume(committed_db, QUOTE_CASE, email_id, clock, tmp_path)
    assert result is None and again.calls == []  # waiting for the owner: not a stopped run


# --- interrupt and the owner's action ---------------------------------------------------------


def test_approval_resumes_the_thread_once_and_records_the_run_finished(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    email_id, clock, _ = run(committed_db, QUOTE_CASE, tmp_path)
    assert finished_rows(committed_db, email_id) == []
    assert resume_after_owner(committed_db.connect, clock, email_id) is False  # still pending
    conn = committed_db.connect()
    assert isinstance(approve_draft(conn, clock, draft_id_of(committed_db, email_id),
                                    version=1), Approved)  # fmt: skip
    conn.commit()
    assert resume_after_owner(committed_db.connect, clock, email_id) is True
    assert finished_rows(committed_db, email_id) == [{"draft_status": "approved"}]
    snapshot = thread_snapshot(committed_db.connect, email_id)
    assert not snapshot.interrupts and snapshot.next == ()
    assert snapshot.values["finished"] == "approved"
    # idempotent: a second resume does nothing
    assert resume_after_owner(committed_db.connect, clock, email_id) is False
    assert len(finished_rows(committed_db, email_id)) == 1


def test_rejection_resumes_the_thread(committed_db: CommittedDb, tmp_path: Path) -> None:
    email_id, clock, _ = run(committed_db, QUOTE_CASE, tmp_path)
    conn = committed_db.connect()
    assert reject_draft(conn, clock, draft_id_of(committed_db, email_id)) is None
    conn.commit()
    assert resume_after_owner(committed_db.connect, clock, email_id) is True
    assert finished_rows(committed_db, email_id) == [{"draft_status": "rejected"}]


def test_supersede_leaves_the_thread_interrupted(committed_db: CommittedDb,
                                                 tmp_path: Path) -> None:  # fmt: skip
    email_id, clock, _ = run(committed_db, QUOTE_CASE, tmp_path)
    case = CASES[QUOTE_CASE]
    newer = replace(case.incoming(), message_id="<newer-from-the-same-guest@example.test>",
                    received_at=case.email.received_at + timedelta(hours=1))  # fmt: skip
    conn = committed_db.connect()
    store_email(conn, clock, newer)
    conn.commit()
    assert one(committed_db, "SELECT status FROM drafts WHERE email_id = %s",
               email_id)[0] == "superseded"  # fmt: skip
    assert resume_after_owner(committed_db.connect, clock, email_id) is False
    assert thread_snapshot(committed_db.connect, email_id).interrupts  # for week 10
    assert finished_rows(committed_db, email_id) == []


# --- the checkpoints themselves ---------------------------------------------------------------


def test_the_checkpointed_state_is_plain_json(committed_db: CommittedDb, tmp_path: Path) -> None:
    email_id, _, _ = run(committed_db, QUOTE_CASE, tmp_path)
    values = thread_snapshot(committed_db.connect, email_id).values
    assert json.loads(json.dumps(values)) == values  # no object, no tuple, nothing else
    assert set(values) <= {
        "email_id", "extraction", "error", "agent", "outcome", "draft_stored", "finished",
        "transcript", "calls", "outcomes", "closed_year", "quote_ids", "policy_ids",
        "pending", "end",
    }  # fmt: skip


def test_reset_empties_the_checkpoint_tables(committed_db: CommittedDb, tmp_path: Path) -> None:
    run(committed_db, QUOTE_CASE, tmp_path)
    count = "SELECT count(*) FROM langgraph.checkpoints"
    assert one(committed_db, count)[0] > 0
    truncate(committed_db.conninfo)
    assert one(committed_db, count)[0] == 0
