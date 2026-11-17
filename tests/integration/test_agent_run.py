"""A run on an agent route end to end (D71, D72, D74), on a committed database: gold
extraction and scripted agent turns (ScriptedClient), the real tools, the real final
write. No network.

Covers the three phases (nothing held while the model works; discarded or born
superseded when the email changed meanwhile), what the final write decides (route,
hold, rendering, guardrails 1, 6 and 7) and every way a run ends.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.clock import FixedClock
from anemos.email_cases import EmailCase, load_cases
from anemos.extraction_run import process_email
from anemos.inbox import lock_sender, store_email
from anemos.llm.adapter import LLM, Settings, ToolOutput, Transcript
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, OnTranscript, ScriptedClient, Step, ToolCallStep
from anemos.run import agent_finish, process_inbox

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

CASES = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=100)
QUOTE_TEXT = "Dear guest,\n\n{{REQUEST_SUMMARY}}\n\n{{QUOTE_BLOCK}}\n\nHotel Anemos"


# --- helpers ---------------------------------------------------------------------------------


def call(name: str, arguments: dict[str, Any] | None = None) -> ToolCallStep:
    return ToolCallStep(name, arguments or {})


def last_output(transcript: Transcript) -> dict[str, Any]:
    item = transcript.items[-1]
    assert isinstance(item, ToolOutput)
    result: dict[str, Any] = json.loads(item.output)
    return result


def draft_offering_what_was_quoted(text: str = QUOTE_TEXT) -> OnTranscript:
    """write_draft offering the quote ids of the last tool answer (the gold agent's way)."""

    def write(transcript: Transcript) -> ToolCallStep:
        ids = [q["quote_id"] for q in last_output(transcript)["quotes"]]
        return call("write_draft", {"text": text, "offer_quote_ids": ids, "policy_ids": []})

    return OnTranscript(write)


def run(committed_db: CommittedDb, case: EmailCase, agent: list[Step], tmp: Path,
        *, clock: FixedClock | None = None) -> tuple[int, str]:  # fmt: skip
    clock = clock or FixedClock(case.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, case.incoming()).email_id
    conn.commit()
    gold = case.gold_output()
    assert gold is not None
    client = ScriptedClient.of([Answer(gold), *agent])
    budget = Budget(clock, 1_000_000, tmp / "llm_spend.jsonl")
    llm = LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)
    result = process_email(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    assert client.script == [], "the script was not used up"
    return email_id, result.outcome


def one(committed_db: CommittedDb, query: str, *params: Any) -> Any:
    conn = committed_db.connect()
    row = conn.execute(query, params).fetchone()
    conn.commit()
    return row


def email_row(committed_db: CommittedDb, email_id: int) -> tuple[str, list[str] | None]:
    status, codes = one(committed_db, "SELECT status, reason_codes FROM emails WHERE id = %s",
                        email_id)  # fmt: skip
    return status, codes


def draft_row(committed_db: CommittedDb, email_id: int) -> dict[str, Any]:
    row = one(
        committed_db,
        "SELECT d.route, d.status, d.request_id, v.body, v.guardrail_results,"
        " (SELECT count(*) FROM draft_offers o WHERE o.draft_id = d.id),"
        " (SELECT count(*) FROM draft_offers o WHERE o.draft_id = d.id AND o.hold)"
        " FROM drafts d JOIN draft_versions v ON v.draft_id = d.id WHERE d.email_id = %s",
        email_id,
    )
    assert row is not None, "no draft"
    keys = ("route", "status", "request_id", "body", "results", "offers", "holds")
    return dict(zip(keys, row, strict=True))


def audit_output(committed_db: CommittedDb, email_id: int, event: str) -> Any:
    row = one(committed_db, "SELECT output FROM audit_log WHERE email_id = %s AND event = %s"
              " ORDER BY id DESC LIMIT 1", email_id, event)  # fmt: skip
    return None if row is None else row[0]


# --- drafts --------------------------------------------------------------------------------


def test_a_quote_with_a_hold(committed_db: CommittedDb, tmp_path: Path) -> None:
    case = CASES["en-quote-sea-view-june"]
    agent: list[Step] = [call("quote_price"), draft_offering_what_was_quoted()]
    email_id, outcome = run(committed_db, case, agent, tmp_path)
    assert outcome == "finished"
    assert email_row(committed_db, email_id)[0] == "awaiting_approval"
    draft = draft_row(committed_db, email_id)
    assert (draft["route"], draft["status"], draft["offers"], draft["holds"]) == (
        "quote", "pending_approval", 1, 1)  # fmt: skip
    body = draft["body"]
    assert "{{QUOTE_BLOCK}}" not in body and "{{REQUEST_SUMMARY}}" not in body
    assert "€" in body and "{{HOLD_UNTIL}}" in body  # the held block keeps it for approval
    assert all(r is True for r in draft["results"].values())
    assert set(draft["results"]) == {
        "g1_quote_block_once", "g2_numbers_from_policies", "g3_summary_present",
        "g4_reply_script", "g5_no_forbidden_phrases", "g6_policies_returned",
        "g7_holds_valid", "placeholders",
    }  # fmt: skip


def test_a_closed_stay_is_declined_with_the_open_season(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    case = CASES["el-christmas-2027"]
    text = "Αγαπητέ επισκέπτη,\n\n{{REQUEST_SUMMARY}}\n\nΕίμαστε ανοιχτά {{OPEN_SEASON}}."
    email_id, _ = run(committed_db, case, [
        call("quote_price"),
        call("write_draft", {"text": text, "offer_quote_ids": [], "policy_ids": []}),
    ], tmp_path)  # fmt: skip
    draft = draft_row(committed_db, email_id)
    assert (draft["route"], draft["offers"]) == ("decline", 0)
    assert "{{OPEN_SEASON}}" not in draft["body"] and "από " in draft["body"]
    assert "2027" in draft["body"]


def test_a_policy_answer_cites_what_search_policies_returned(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    case = CASES["el-pets-policy"]
    text = "Αγαπητέ επισκέπτη,\n\nΔεν δεχόμαστε κατοικίδια.\n\nΞενοδοχείο Άνεμος"
    email_id, _ = run(committed_db, case, [
        call("search_policies", {"query": "Are pets allowed?"}),
        Answer({"policy_ids": ["pets-assistance-dogs"]}),  # the selector's call
        call("write_draft", {"text": text, "offer_quote_ids": [],
                             "policy_ids": ["pets-assistance-dogs"]}),
    ], tmp_path)  # fmt: skip
    draft = draft_row(committed_db, email_id)
    assert (draft["route"], draft["request_id"], draft["body"]) == ("policy", None, text)
    created = audit_output(committed_db, email_id, "draft.created")
    assert created["policy_ids"] == ["pets-assistance-dogs"]


# --- escalations ---------------------------------------------------------------------------


def test_no_match_escalates_no_policy(committed_db: CommittedDb, tmp_path: Path) -> None:
    email_id, _ = run(committed_db, CASES["en-nomatch-pool-gym"], [
        call("search_policies", {"query": "Do you have a pool or a gym?"}),
        Answer({"policy_ids": []}),
    ], tmp_path)  # fmt: skip
    assert email_row(committed_db, email_id) == ("escalated", ["NO_POLICY"])


def test_the_model_escalates_kind_other(committed_db: CommittedDb, tmp_path: Path) -> None:
    email_id, _ = run(committed_db, CASES["el-cancellation"], [
        call("escalate", {"reason_code": "CANCELLATION", "note": "Wants to cancel."}),
    ], tmp_path)  # fmt: skip
    assert email_row(committed_db, email_id) == ("escalated", ["CANCELLATION"])
    escalated = audit_output(committed_db, email_id, "email.escalated")
    assert escalated["detail"] == "Wants to cancel."


@pytest.mark.parametrize(("draft", "problem"), [
    ({"text": "Dear guest, see below.", "offer_quote_ids": [], "policy_ids": ["wifi"]},
     "cites policies not returned"),  # guardrail 6: wifi was never returned
    ({"text": "Dear guest, our price is below.", "offer_quote_ids": [999_999],
      "policy_ids": []}, "not priced in this run"),  # guardrail 7, and guardrail 1
], ids=["g6", "g7"])  # fmt: skip
def test_a_failed_check_escalates_guardrail_fail(
    committed_db: CommittedDb, tmp_path: Path, draft: dict[str, Any], problem: str
) -> None:
    """Failing twice: the first failure goes back to the model (D76)."""
    email_id, _ = run(committed_db, CASES["en-quote-sea-view-june"],
                      [call("write_draft", draft), call("write_draft", draft)],
                      tmp_path)  # fmt: skip
    assert email_row(committed_db, email_id) == ("escalated", ["GUARDRAIL_FAIL"])
    assert problem in audit_output(committed_db, email_id, "email.escalated")["detail"]
    assert one(committed_db, "SELECT count(*) FROM drafts")[0] == 0


# --- D71: the three phases ---------------------------------------------------------------


def test_nothing_is_held_while_the_model_works(committed_db: CommittedDb, tmp_path: Path) -> None:
    """During a model turn, no connection is idle in a transaction, and another session
    takes the guest's sender lock at once (D61, D71)."""
    case = CASES["en-quote-sea-view-june"]
    seen: list[tuple[int, bool]] = []

    def look(transcript: Transcript) -> ToolCallStep:
        other = committed_db.connect()
        [open_transactions] = other.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND state = 'idle in transaction' AND pid <> pg_backend_pid()"
        ).fetchone()  # type: ignore[misc]
        [address] = other.execute("SELECT from_address FROM emails").fetchone()  # type: ignore[misc]
        other.execute("SET lock_timeout = '1s'")
        lock_sender(other, address)  # raises LockNotAvailable if held
        other.rollback()
        seen.append((open_transactions, True))
        return call("quote_price")

    run(committed_db, case, [OnTranscript(look), draft_offering_what_was_quoted()], tmp_path)
    assert seen == [(0, True)]


def test_an_email_closed_during_the_loop_is_discarded(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    case = CASES["en-quote-sea-view-june"]
    clock = FixedClock(case.email.received_at)

    def owner_closes_it(transcript: Transcript) -> ToolCallStep:
        """The email changes while the model works (as the extraction run's test does:
        no owner action closes a processing email today, D65 guards against it anyway)."""
        other = committed_db.connect()
        other.execute("UPDATE emails SET status = 'closed'")
        other.commit()
        return call("quote_price")

    email_id, outcome = run(committed_db, case, [OnTranscript(owner_closes_it),
                                                 draft_offering_what_was_quoted()], tmp_path,
                            clock=clock)  # fmt: skip
    assert outcome == "discarded"
    assert email_row(committed_db, email_id)[0] == "closed"
    assert one(committed_db, "SELECT count(*) FROM drafts")[0] == 0
    assert audit_output(committed_db, email_id, "run.discarded") == {
        "step": "agent", "email_status": "closed"}  # fmt: skip


def test_a_newer_email_during_the_loop_makes_the_draft_born_superseded(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """Storing the guest's next email does not wait for the loop (nothing is held), and
    the final write sees it (D48)."""
    case = CASES["en-quote-sea-view-june"]
    clock = FixedClock(case.email.received_at + timedelta(hours=1))

    def guest_writes_again(transcript: Transcript) -> ToolCallStep:
        other = committed_db.connect()
        newer = replace(case.incoming(), message_id="<again@cases.example.test>",
                        received_at=case.email.received_at + timedelta(minutes=30))  # fmt: skip
        store_email(other, clock, newer)
        other.commit()
        return call("quote_price")

    email_id, _ = run(committed_db, case, [OnTranscript(guest_writes_again),
                                           draft_offering_what_was_quoted()], tmp_path,
                      clock=clock)  # fmt: skip
    assert email_row(committed_db, email_id)[0] == "superseded"
    assert draft_row(committed_db, email_id)["status"] == "superseded"


def test_process_inbox_runs_the_agent_by_default(committed_db: CommittedDb, tmp_path: Path) -> None:
    case = CASES["el-cancellation"]
    clock = FixedClock(case.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, case.incoming()).email_id
    conn.commit()
    gold = case.gold_output()
    assert gold is not None
    client = ScriptedClient.of([Answer(gold), call("escalate", {"reason_code": "CANCELLATION",
                                                                "note": "Cancel."})])  # fmt: skip
    budget = Budget(clock, 1_000_000, tmp_path / "llm_spend.jsonl")
    llm = LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)
    process_inbox(committed_db.connect, clock, llm)
    assert email_row(committed_db, email_id) == ("escalated", ["CANCELLATION"])
