"""Escalation summaries and secondary codes (D81) through process_email, on a committed
database. Every model call is scripted or answered by the gold client: no network."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from anemos import graph
from anemos.api import create_app
from anemos.clock import FixedClock
from anemos.email_cases import load_cases
from anemos.extraction_run import process_email
from anemos.gold import GoldClient
from anemos.graph import resume_run
from anemos.inbox import lock_sender, store_email
from anemos.llm.adapter import LLM, PermanentError, Prompt, Settings
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, ScriptedClient, Step, ToolCallStep
from anemos.run import agent_finish

if TYPE_CHECKING:
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

CASES = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=100)


class Stop(BaseException):
    """The process dies."""


def run(committed_db: CommittedDb, case_id: str, client: Any, tmp: Path) -> int:
    case = CASES[case_id]
    clock = FixedClock(case.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, case.incoming()).email_id
    conn.commit()
    budget = Budget(clock, 1_000_000, tmp / "llm_spend.jsonl")
    llm = LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)
    process_email(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    return email_id


def scripted(case_id: str, *steps: Step) -> ScriptedClient:
    gold = CASES[case_id].gold_output()
    assert gold is not None
    return ScriptedClient.of([Answer(gold), *steps])


def email(committed_db: CommittedDb, email_id: int) -> tuple[str, list[str], str | None]:
    conn = committed_db.connect()
    row = conn.execute("SELECT status, reason_codes, escalation_summary FROM emails"
                       " WHERE id = %s", (email_id,)).fetchone()  # fmt: skip
    conn.commit()
    assert row is not None
    return row[0], row[1], row[2]


def escalated_audit(committed_db: CommittedDb, email_id: int) -> dict[str, Any]:
    conn = committed_db.connect()
    row = conn.execute("SELECT output FROM audit_log WHERE email_id = %s"
                       " AND event = 'email.escalated'", (email_id,)).fetchone()  # fmt: skip
    conn.commit()
    assert row is not None
    output: dict[str, Any] = row[0]
    return output


def test_a_code_escalation_gets_a_summary_and_its_second_code(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """el-group-wedding: GROUP by code; the summary adds the DISCOUNT the guest asks."""
    email_id = run(committed_db, "el-group-wedding", GoldClient([CASES["el-group-wedding"]]),
                   tmp_path)  # fmt: skip
    assert email(committed_db, email_id) == (
        "escalated", ["GROUP", "DISCOUNT"], "Gold summary of el-group-wedding.")  # fmt: skip
    audit = escalated_audit(committed_db, email_id)
    assert audit["added_by_model"] == ["DISCOUNT"] and audit["reason"] == "GROUP"
    with TestClient(
        create_app(committed_db.connect, FixedClock(CASES["el-group-wedding"].email.received_at))
    ) as api:
        detail = api.get(f"/emails/{email_id}").json()
    assert detail["escalation_summary"] == "Gold summary of el-group-wedding."


def test_an_agent_escalation_gets_a_summary(committed_db: CommittedDb, tmp_path: Path) -> None:
    client = scripted(
        "el-cancellation",
        ToolCallStep("escalate", {"reason_code": "CANCELLATION", "note": "Wants to cancel."}),
        Answer({"summary": "The guest wants to cancel.", "add_codes": []}),
    )
    email_id = run(committed_db, "el-cancellation", client, tmp_path)
    assert email(committed_db, email_id) == (
        "escalated", ["CANCELLATION"], "The guest wants to cancel.")  # fmt: skip


def test_codes_are_only_added_never_repeated_or_removed(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    client = scripted(
        "en-complaint-air-conditioning",
        ToolCallStep("escalate", {"reason_code": "COMPLAINT", "note": "Air conditioning."}),
        Answer({"summary": "A complaint.", "add_codes": ["COMPLAINT", "DISCOUNT"]}),
    )
    email_id = run(committed_db, "en-complaint-air-conditioning", client, tmp_path)
    status, codes, _ = email(committed_db, email_id)
    assert (status, codes) == ("escalated", ["COMPLAINT", "DISCOUNT"])
    assert escalated_audit(committed_db, email_id)["added_by_model"] == ["DISCOUNT"]


def test_a_failed_summary_call_still_escalates_without_a_summary(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    client = scripted("en-group-nine-adults", PermanentError("summary model down"))
    email_id = run(committed_db, "en-group-nine-adults", client, tmp_path)
    assert email(committed_db, email_id) == ("escalated", ["GROUP"], None)  # not SYSTEM_ERROR
    conn = committed_db.connect()
    failed = conn.execute("SELECT step FROM audit_log WHERE email_id = %s AND event ="
                          " 'llm.failed'", (email_id,)).fetchall()  # fmt: skip
    conn.commit()
    assert failed == [("summary",)]


def test_injection_gets_no_summary_call(committed_db: CommittedDb, tmp_path: Path) -> None:
    client = scripted("en-injection-confirm-free")  # the extraction only: a 2nd call fails
    email_id = run(committed_db, "en-injection-confirm-free", client, tmp_path)
    assert email(committed_db, email_id) == ("escalated", ["INJECTION"], None)
    assert client.script == [] and len(client.sent) == 1


def test_nothing_is_held_during_the_summary_call(committed_db: CommittedDb, tmp_path: Path) -> None:
    """D71: the escalation by code is not written yet; no connection is idle in a
    transaction, and another session takes the guest's sender lock at once."""
    seen: list[tuple[int, str]] = []

    def look(prompt: Prompt) -> Answer:
        other = committed_db.connect()
        [open_transactions] = other.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND state = 'idle in transaction' AND pid <> pg_backend_pid()"
        ).fetchone()  # type: ignore[misc]
        [address, status] = other.execute("SELECT from_address, status FROM emails").fetchone()  # type: ignore[misc]
        other.execute("SET lock_timeout = '1s'")
        lock_sender(other, address)  # raises LockNotAvailable if held
        other.rollback()
        seen.append((open_transactions, status))
        return Answer({"summary": "Nine adults.", "add_codes": []})

    email_id = run(committed_db, "en-group-nine-adults", scripted("en-group-nine-adults", look),
                   tmp_path)  # fmt: skip
    assert seen == [(0, "processing")]
    assert email(committed_db, email_id)[0] == "escalated"


def test_a_resume_after_the_summary_does_not_call_again(
    committed_db: CommittedDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stop(*_: object) -> str:
        raise Stop

    monkeypatch.setattr(graph, "write_escalation", stop)
    client = scripted("en-group-nine-adults", Answer({"summary": "Nine.", "add_codes": []}))
    with pytest.raises(Stop):
        run(committed_db, "en-group-nine-adults", client, tmp_path)
    monkeypatch.undo()
    case = CASES["en-group-nine-adults"]
    clock = FixedClock(case.email.received_at)
    again = ScriptedClient.of([])  # any model call would fail: the script is empty
    budget = Budget(clock, 1_000_000, tmp_path / "llm_spend.jsonl")
    llm = LLM(again, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)
    conn = committed_db.connect()
    [email_id] = conn.execute("SELECT id FROM emails").fetchone()  # type: ignore[misc]
    conn.commit()
    resume_run(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    assert email(committed_db, email_id) == ("escalated", ["GROUP"], "Nine.")
    assert again.sent == []
