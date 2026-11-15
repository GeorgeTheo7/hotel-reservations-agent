"""Extraction under D65's transaction boundaries, on committed_db, with the scripted
client answering each case's gold labels (no key, no network)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.clock import Clock, FixedClock
from anemos.email_cases import EmailCase, load_cases
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import Claimed, delimited_email, process_email
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, Prompt, Settings, TransientError
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, ScriptedClient, Step
from anemos.llm.openai_client import OpenAIClient
from anemos.prompts import EXTRACTION_V1
from anemos.reservations.datespec import resolve
from anemos.reservations.model import Stay
from anemos.states import NotFound, TransitionRefused

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

EVAL = load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")
EXTRACTED = [c for c in EVAL if c.labels.kind is not None]
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=100)  # replaced by the prompt's


def no_finish(conn: Conn, clock: Clock, claimed: Claimed, out: ExtractionOutput) -> None:
    """Step 5 routes and drafts here; extraction alone leaves the email processing."""


def stored(committed_db: CommittedDb, case: EmailCase) -> tuple[int, FixedClock]:
    clock = FixedClock(case.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, case.incoming()).email_id
    conn.commit()
    return email_id, clock


def make_llm(committed_db: CommittedDb, steps: list[Step], clock: FixedClock, tmp: Path,
             cap: int = 1_000_000) -> tuple[LLM, ScriptedClient]:  # fmt: skip
    client = ScriptedClient.of(steps)
    budget = Budget(clock, cap, tmp / "llm_spend.jsonl")
    llm = LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)
    return llm, client


def one(committed_db: CommittedDb, query: str, *params: Any) -> Any:
    conn = committed_db.connect()
    row = conn.execute(query, params).fetchone()
    conn.commit()
    return row


def events(committed_db: CommittedDb) -> list[str]:
    conn = committed_db.connect()
    found = [e for (e,) in conn.execute("SELECT event FROM audit_log ORDER BY id")]
    conn.commit()
    return found


# --- every test email, its gold answer stored ---------------------------------------


@pytest.mark.parametrize("case", EXTRACTED, ids=[c.id for c in EXTRACTED])
def test_a_gold_extraction_is_stored(
    committed_db: CommittedDb, tmp_path: Path, case: EmailCase
) -> None:
    gold = case.gold_output()
    assert gold is not None
    email_id, clock = stored(committed_db, case)
    llm, client = make_llm(committed_db, [Answer(gold)], clock, tmp_path)

    result = process_email(committed_db.connect, clock, llm, email_id, finish=no_finish)

    assert (result.outcome, result.extraction) == ("finished", gold)
    [sent] = client.sent
    assert sent.settings.max_output_tokens == EXTRACTION_V1.max_output_tokens  # the prompt's
    # D6: the model gets exactly the prompt file and the guest's subject and body;
    # nothing else, so never today's date or received_at.
    assert sent.prompt.instructions == EXTRACTION_V1.text()
    assert sent.prompt.input == delimited_email(case.email.subject, case.email.body.strip())
    assert one(committed_db, "SELECT status, language FROM emails WHERE id = %s", email_id) == (
        "processing", case.labels.language,
    )  # fmt: skip
    request = one(committed_db, "SELECT adults, children, child_ages, rooms, room_type_id,"
                  " stay, extracted FROM requests WHERE email_id = %s", email_id)  # fmt: skip
    if gold.kind != "request":
        assert request is None  # questions, other and spam have no request row
        return
    assert request[:5] == (gold.adults, gold.children, gold.child_ages, gold.rooms, gold.room_type)
    spec = gold.date_spec_value()
    resolved = resolve(spec, case.email.received_at) if spec else None
    if isinstance(resolved, Stay):
        assert (request[5].lower, request[5].upper) == (resolved.check_in, resolved.check_out)
    else:
        assert request[5] is None  # a window or dates to ask for: date_spec kept in extracted
    assert ExtractionOutput.model_validate(request[6]) == gold
    audit = one(committed_db, "SELECT prompt_version, output->'result' FROM audit_log"
                " WHERE event = 'llm.called'")  # fmt: skip
    assert audit == (EXTRACTION_V1.version, gold.model_dump(mode="json"))


# --- D65: nothing held during the call; the email may change meanwhile ---------------


CASE = next(c for c in EVAL if c.id == "en-quote-sea-view-june")


def during_call(action: Callable[[], None]) -> Step:
    def step(prompt: Prompt) -> Answer:
        action()
        gold = CASE.gold_output()
        assert gold is not None
        return Answer(gold)

    return step


def test_no_lock_is_held_while_the_model_works(committed_db: CommittedDb, tmp_path: Path) -> None:
    email_id, clock = stored(committed_db, CASE)
    checks: list[bool] = []

    def try_locks() -> None:
        other = committed_db.connect()
        row = other.execute(
            "SELECT id FROM emails WHERE id = %s FOR UPDATE NOWAIT", (email_id,)
        ).fetchone()  # raises at once if the run still held the row
        sender = other.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
            ("anna@example.test",),
        ).fetchone()
        checks.extend([row is not None, bool(sender and sender[0])])
        other.rollback()

    llm, _ = make_llm(committed_db, [during_call(try_locks)], clock, tmp_path)
    assert (
        process_email(committed_db.connect, clock, llm, email_id, finish=no_finish).outcome
        == "finished"
    )
    assert checks == [True, True]


def test_an_email_changed_during_the_call_discards_the_run(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """The owner handled the email while the model was working: the result is not
    written, and a run.discarded row records it; the call itself stays on record."""
    email_id, clock = stored(committed_db, CASE)

    def owner_closes() -> None:
        other = committed_db.connect()
        other.execute("UPDATE emails SET status = 'closed' WHERE id = %s", (email_id,))
        other.commit()

    llm, _ = make_llm(committed_db, [during_call(owner_closes)], clock, tmp_path)
    result = process_email(committed_db.connect, clock, llm, email_id, finish=no_finish)
    assert result.outcome == "discarded"
    assert one(committed_db, "SELECT count(*) FROM requests") == (0,)
    assert one(committed_db, "SELECT status, language FROM emails WHERE id = %s", email_id) == (
        "closed", None,
    )  # fmt: skip
    assert events(committed_db)[-2:] == ["llm.called", "run.discarded"]
    assert one(committed_db, "SELECT output FROM audit_log WHERE event = 'run.discarded'") == (
        {"step": "extract", "email_status": "closed"},
    )  # fmt: skip


def test_finish_runs_in_the_final_transaction(committed_db: CommittedDb, tmp_path: Path) -> None:
    """If finish fails, the stored extraction goes with it: one transaction."""
    email_id, clock = stored(committed_db, CASE)
    gold = CASE.gold_output()
    assert gold is not None

    def broken(conn: Conn, clock: Clock, claimed: Claimed, out: ExtractionOutput) -> None:
        raise RuntimeError("drafting failed")

    llm, _ = make_llm(committed_db, [Answer(gold)], clock, tmp_path)
    with pytest.raises(RuntimeError, match="drafting failed"):
        process_email(committed_db.connect, clock, llm, email_id, finish=broken)
    assert one(committed_db, "SELECT count(*) FROM requests") == (0,)


# --- failures escalate SYSTEM_ERROR (D49) ---------------------------------------------


def test_a_reply_cut_off_at_the_output_limit_escalates_and_stores_nothing(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """Through the real OpenAI client (simulated endpoint): a reply that hit
    max_output_tokens is `incomplete`; it is billed, not retried, and the email is
    escalated SYSTEM_ERROR with no request row and no language."""
    email_id, clock = stored(committed_db, CASE)
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={
            "id": "r", "object": "response", "created_at": 1, "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"}, "model": "gpt-6-luna",
            "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            "output": [{"type": "message", "id": "m", "status": "incomplete",
                        "role": "assistant", "content": [{"type": "output_text",
                        "text": '{"kind": "request", "langu', "annotations": []}]}],
            "usage": {"input_tokens": 1500, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": 600, "output_tokens_details": {"reasoning_tokens": 420},
                      "total_tokens": 2100}})  # fmt: skip

    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    client = OpenAIClient(api_key="k", base_url="http://openai.invalid/v1", http_client=http)
    budget = Budget(clock, 1_000_000, tmp_path / "llm_spend.jsonl")
    llm = LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)

    result = process_email(committed_db.connect, clock, llm, email_id, finish=no_finish)

    assert result.outcome == "escalated"
    assert len(seen) == 1  # permanent: not retried
    assert one(committed_db, "SELECT status, reason_codes, language FROM emails WHERE id = %s",
               email_id) == ("escalated", ["SYSTEM_ERROR"], None)  # fmt: skip
    assert one(committed_db, "SELECT count(*) FROM requests") == (0,)
    failed = one(committed_db, "SELECT error, output_tokens, cost_micro_usd FROM audit_log"
                 " WHERE event = 'llm.failed'")  # fmt: skip
    assert "incomplete" in failed[0] and "max_output_tokens" in failed[0]
    assert failed[1] == 600 and failed[2] > 0  # the cut-off reply was paid for


def test_a_failed_model_call_escalates_system_error(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    email_id, clock = stored(committed_db, CASE)
    llm, _ = make_llm(committed_db, [TransientError("timeout"), TransientError("timeout")],
                      clock, tmp_path)  # fmt: skip
    result = process_email(committed_db.connect, clock, llm, email_id, finish=no_finish)
    assert result.outcome == "escalated"
    assert one(committed_db, "SELECT status, reason_codes FROM emails WHERE id = %s", email_id) == (
        "escalated", ["SYSTEM_ERROR"],
    )  # fmt: skip
    detail = one(
        committed_db, "SELECT output->>'detail' FROM audit_log WHERE event = 'email.escalated'"
    )
    assert detail is not None and "LLMError" in detail[0]


def test_an_exhausted_budget_escalates_without_a_call(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    email_id, clock = stored(committed_db, CASE)
    llm, client = make_llm(committed_db, [], clock, tmp_path, cap=0)
    result = process_email(committed_db.connect, clock, llm, email_id, finish=no_finish)
    assert result.outcome == "escalated"
    assert client.sent == []
    assert "llm.refused_budget" in events(committed_db)


def test_only_a_received_email_is_claimed(committed_db: CommittedDb, tmp_path: Path) -> None:
    email_id, clock = stored(committed_db, CASE)
    llm, client = make_llm(committed_db, [], clock, tmp_path)
    conn = committed_db.connect()
    conn.execute("UPDATE emails SET status = 'set_aside' WHERE id = %s", (email_id,))
    conn.commit()
    with pytest.raises(TransitionRefused):
        process_email(committed_db.connect, clock, llm, email_id, finish=no_finish)
    with pytest.raises(NotFound):
        process_email(committed_db.connect, clock, llm, 999_999, finish=no_finish)
    assert client.sent == []
