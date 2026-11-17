"""Every way a run ends writes exactly one terminal audit event and never leaves the
email `processing` (audit.TERMINAL_EVENTS): the endings the gold run does not reach.
Scripted model calls, a committed database, no network."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from anemos import run as run_module
from anemos.clock import FixedClock
from anemos.email_cases import load_cases
from anemos.extraction_run import process_email
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, PermanentError, Settings, Transcript
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, OnTranscript, ScriptedClient, Step, ToolCallStep
from anemos.run import agent_finish

if TYPE_CHECKING:
    from conftest import CommittedDb, EndedOnce

pytestmark = pytest.mark.integration

CASES = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=100)
SUMMARY = Answer({"summary": "For the owner.", "add_codes": []})
BAD = {"text": "Dear guest, 26 € and guaranteed.", "offer_quote_ids": [], "policy_ids": []}
# el-pets-policy is Greek: a reply that passes every check (guardrail 4 included).
FINE = {"text": "Αγαπητέ επισκέπτη, σας ευχαριστούμε.", "offer_quote_ids": [], "policy_ids": []}


def start(committed_db: CommittedDb, case_id: str) -> tuple[int, FixedClock]:
    case = CASES[case_id]
    clock = FixedClock(case.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, case.incoming()).email_id
    conn.commit()
    return email_id, clock


def go(committed_db: CommittedDb, case_id: str, steps: Callable[[int], list[Step]],
       tmp: Path, *, extraction: bool = True) -> tuple[int, str]:  # fmt: skip
    email_id, clock = start(committed_db, case_id)
    gold = CASES[case_id].gold_output()
    script = ([Answer(gold)] if extraction and gold is not None else []) + steps(email_id)
    client = ScriptedClient.of(script)
    budget = Budget(clock, 1_000_000, tmp / "llm_spend.jsonl")
    llm = LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)
    result = process_email(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    assert client.script == [], "the script was not used up"
    return email_id, result.outcome


def codes(committed_db: CommittedDb, email_id: int) -> Any:
    conn = committed_db.connect()
    row = conn.execute("SELECT status, reason_codes FROM emails WHERE id = %s",
                       (email_id,)).fetchone()  # fmt: skip
    conn.commit()
    return row


def test_a_thread_reply(committed_db: CommittedDb, tmp_path: Path, ended_once: EndedOnce) -> None:
    email_id, _ = go(committed_db, "en-thread-reply", lambda _: [], tmp_path, extraction=False)
    assert ended_once(email_id) == "email.escalated"


def test_a_failed_extraction(committed_db: CommittedDb, tmp_path: Path,
                             ended_once: EndedOnce) -> None:  # fmt: skip
    email_id, _ = go(committed_db, "en-quote-sea-view-june",
                     lambda _: [PermanentError("down")], tmp_path, extraction=False)  # fmt: skip
    assert ended_once(email_id) == "email.escalated"
    assert codes(committed_db, email_id) == ("escalated", ["SYSTEM_ERROR"])


def test_the_loop_limit(committed_db: CommittedDb, tmp_path: Path, ended_once: EndedOnce) -> None:
    unknown = [ToolCallStep("no_such_tool", {}, f"call_{n}") for n in range(8)]
    email_id, _ = go(committed_db, "el-pets-policy", lambda _: [*unknown, SUMMARY], tmp_path)
    assert ended_once(email_id) == "email.escalated"
    assert codes(committed_db, email_id) == ("escalated", ["LOOP_LIMIT"])


def test_guardrail_fail_after_the_redraft(committed_db: CommittedDb, tmp_path: Path,
                                          ended_once: EndedOnce) -> None:  # fmt: skip
    bad = [ToolCallStep("write_draft", BAD, f"call_{n}") for n in range(2)]
    email_id, _ = go(committed_db, "el-pets-policy", lambda _: [*bad, SUMMARY], tmp_path)
    assert ended_once(email_id) == "email.escalated"
    assert codes(committed_db, email_id) == ("escalated", ["GUARDRAIL_FAIL"])


def test_a_bug_in_the_final_write(
    committed_db: CommittedDb, tmp_path: Path, ended_once: EndedOnce,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(run_module, "finish_agent", broken)
    email_id, _ = go(committed_db, "el-pets-policy",
                     lambda _: [ToolCallStep("write_draft", FINE)], tmp_path)  # fmt: skip
    assert ended_once(email_id) == "email.escalated"
    assert codes(committed_db, email_id) == ("escalated", ["SYSTEM_ERROR"])
    conn = committed_db.connect()
    [detail] = conn.execute("SELECT output ->> 'detail' FROM audit_log WHERE email_id = %s"
                            " AND event = 'email.escalated'", (email_id,)).fetchone()  # type: ignore[misc]  # fmt: skip
    conn.commit()
    assert detail.startswith("final write failed after the loop ended (draft)")


def test_a_discard_during_the_loop(committed_db: CommittedDb, tmp_path: Path,
                                   ended_once: EndedOnce) -> None:  # fmt: skip
    def owner_moved_it(email_id: int) -> list[Step]:
        def write(_: Transcript) -> ToolCallStep:
            other = committed_db.connect()
            other.execute("UPDATE emails SET status = 'escalated', reason_codes = '{MODEL}'"
                          " WHERE id = %s", (email_id,))  # fmt: skip
            other.commit()
            return ToolCallStep("write_draft", FINE)

        return [OnTranscript(write)]

    email_id, outcome = go(committed_db, "el-pets-policy", owner_moved_it, tmp_path)
    assert outcome == "discarded"
    assert ended_once(email_id) == "run.discarded"


def test_a_draft_born_superseded(committed_db: CommittedDb, tmp_path: Path,
                                 ended_once: EndedOnce) -> None:  # fmt: skip
    case = CASES["el-pets-policy"]

    def newer_arrives(_: int) -> list[Step]:
        def write(_: Transcript) -> ToolCallStep:
            other = committed_db.connect()
            first = case.incoming()
            newer = replace(first, message_id="<newer@example.test>",
                            received_at=first.received_at + timedelta(hours=1))  # fmt: skip
            store_email(other, FixedClock(case.email.received_at), newer)
            other.commit()
            return ToolCallStep("write_draft", FINE)

        return [OnTranscript(write)]

    email_id, _ = go(committed_db, "el-pets-policy", newer_arrives, tmp_path)
    assert ended_once(email_id) == "draft.created"
    assert codes(committed_db, email_id)[0] == "superseded"


def test_a_failed_summary(committed_db: CommittedDb, tmp_path: Path,
                          ended_once: EndedOnce) -> None:  # fmt: skip
    email_id, _ = go(committed_db, "en-group-nine-adults",
                     lambda _: [PermanentError("summary down")], tmp_path)  # fmt: skip
    assert ended_once(email_id) == "email.escalated"
    assert codes(committed_db, email_id) == ("escalated", ["GROUP"])
