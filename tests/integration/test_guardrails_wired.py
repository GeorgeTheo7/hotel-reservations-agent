"""Guardrails 2-5 in a run: a draft that fails one twice is GUARDRAIL_FAIL (the first
failure goes back to the model, D76), with the failed check in the escalation's
detail."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from anemos.clock import FixedClock
from anemos.email_cases import load_cases
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import process_email
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, Settings
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, ScriptedClient, ToolCallStep
from anemos.run import agent_finish

if TYPE_CHECKING:
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

CASES = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=100)


def outcome(committed_db: CommittedDb, case_id: str, text: str, tmp: Path,
            extraction: ExtractionOutput | None = None) -> tuple[str, Any, str | None]:  # fmt: skip
    """(email status, reason codes, escalation detail) after write_draft(text), sent
    twice if the first is refused (the script then has the same draft again)."""
    case = CASES[case_id]
    clock = FixedClock(case.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, case.incoming()).email_id
    conn.commit()
    gold = extraction or case.gold_output()
    assert gold is not None
    write = ToolCallStep("write_draft", {"text": text, "offer_quote_ids": [], "policy_ids": []})
    client = ScriptedClient.of([Answer(gold), write, write])
    budget = Budget(clock, 1_000_000, tmp / "llm_spend.jsonl")
    llm = LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)
    process_email(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    status, codes = conn.execute("SELECT status, reason_codes FROM emails WHERE id = %s",
                                 (email_id,)).fetchone() or (None, None)  # fmt: skip
    row = conn.execute("SELECT output ->> 'detail' FROM audit_log WHERE email_id = %s"
                       " AND event = 'email.escalated'", (email_id,)).fetchone()  # fmt: skip
    conn.commit()
    return status, codes, None if row is None else row[0]


def party_missing(case_id: str) -> ExtractionOutput:
    gold = CASES[case_id].gold_output()
    assert gold is not None
    return ExtractionOutput.model_validate(gold.model_dump() | {"adults": None, "children": None})


def test_g3_a_relative_date_ask_info_without_the_summary_gets_it_from_code(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """D80 (owner, week 6): code inserts the summary, so the reply is a draft; it was
    GUARDRAIL_FAIL before."""
    text = "Dear guest,\n\nHow many guests will stay?\n\nHotel Anemos"
    status, codes, detail = outcome(committed_db, "en-next-weekend-june", text, tmp_path,
                                    party_missing("en-next-weekend-june"))  # fmt: skip
    assert (status, codes, detail) == ("awaiting_approval", [], None)


def test_g3_the_same_reply_with_the_summary_is_a_draft(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    text = "Dear guest,\n\n{{REQUEST_SUMMARY}}\n\nHow many guests will stay?\n\nHotel Anemos"
    status, _, _ = outcome(committed_db, "en-next-weekend-june", text, tmp_path,
                           party_missing("en-next-weekend-june"))  # fmt: skip
    assert status == "awaiting_approval"


@pytest.mark.parametrize("text, check", [
    ("Dear guest,\n\n{{REQUEST_SUMMARY}}\n\nHow many guests? We have 3 rooms left.\n\nAnemos",
     "numbers outside"),  # g2
    ("Dear guest,\n\n{{REQUEST_SUMMARY}}\n\nΠόσοι θα είστε;\n\nHotel Anemos",
     "English reply with"),  # g4
    ("Dear guest,\n\n{{REQUEST_SUMMARY}}\n\nHow many guests? A discount is possible.\n\nAnemos",
     "forbidden phrases"),  # g5
])  # fmt: skip
def test_g2_g4_g5_fail_twice_is_guardrail_fail(
    committed_db: CommittedDb, tmp_path: Path, text: str, check: str
) -> None:
    status, codes, detail = outcome(committed_db, "en-next-weekend-june", text, tmp_path,
                                    party_missing("en-next-weekend-june"))  # fmt: skip
    assert (status, codes) == ("escalated", ["GUARDRAIL_FAIL"])
    assert detail is not None and check in detail


def test_g2_a_number_from_a_returned_policy_passes(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """el-greeklish-transfer-policy: the selector returns `transfer` (€25.00)."""
    case_id = "el-greeklish-transfer-policy"
    search = ToolCallStep("search_policies", {"query": "airport transfer price"})
    selected = Answer({"policy_ids": ["transfer"]})
    text = "Καλησπέρα,\n\nΗ μεταφορά από το αεροδρόμιο κοστίζει 25 € ανά αυτοκίνητο.\n\nAnemos"
    write_args = {"text": text, "offer_quote_ids": [], "policy_ids": ["transfer"]}
    case = CASES[case_id]
    clock = FixedClock(case.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, case.incoming()).email_id
    conn.commit()
    gold = case.gold_output()
    assert gold is not None
    client = ScriptedClient.of([Answer(gold), search, selected,
                                ToolCallStep("write_draft", write_args)])  # fmt: skip
    budget = Budget(clock, 1_000_000, tmp_path / "llm_spend.jsonl")
    llm = LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)
    process_email(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    row = conn.execute("SELECT status FROM emails WHERE id = %s", (email_id,)).fetchone()
    conn.commit()
    assert row == ("awaiting_approval",)
