"""The redraft (D76) through the production entry point: process_email runs the graph,
whose agent nodes always check write_draft with the run's facts. If the graph lost its
facts, the first refused draft would reach the final write and end GUARDRAIL_FAIL, and
these tests would fail."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from anemos.clock import FixedClock
from anemos.email_cases import load_cases
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import process_email
from anemos.graph import resume_run
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, Settings, Transcript
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, OnTranscript, ScriptedClient, Step, ToolCallStep
from anemos.run import agent_finish

if TYPE_CHECKING:
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

CASE = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}[
    "en-next-weekend-june"
]
CLOCK = FixedClock(CASE.email.received_at)
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=100)
# Party missing: route ask_info, and "next weekend" is relative, so guardrail 3 requires
# the summary (D80).
_GOLD = CASE.gold_output()
assert _GOLD is not None
EXTRACTION = ExtractionOutput.model_validate(
    _GOLD.model_dump() | {"adults": None, "children": None}
)
BAD = "Dear guest,\n\nHow many guests will stay?\n\nHotel Anemos"
GOOD = "Dear guest,\n\n{{REQUEST_SUMMARY}}\n\nHow many guests will stay?\n\nHotel Anemos"


class Stop(BaseException):
    """The process dies between turns."""


def write(text: str, n: int) -> ToolCallStep:
    return ToolCallStep("write_draft", {"text": text, "offer_quote_ids": [], "policy_ids": []},
                        f"call_draft_{n}")  # fmt: skip


def llm_with(committed_db: CommittedDb, steps: list[Step], tmp: Path) -> LLM:
    budget = Budget(CLOCK, 1_000_000, tmp / "llm_spend.jsonl")
    return LLM(ScriptedClient.of(steps), SETTINGS, budget, committed_db.connect, CLOCK,
               sleep=lambda s: None)  # fmt: skip


def stored(committed_db: CommittedDb) -> int:
    conn = committed_db.connect()
    email_id = store_email(conn, CLOCK, CASE.incoming()).email_id
    conn.commit()
    return email_id


def rows(committed_db: CommittedDb, query: str, *params: Any) -> list[Any]:
    conn = committed_db.connect()
    found = conn.execute(query, params).fetchall()
    conn.commit()
    return found


def email(committed_db: CommittedDb, email_id: int) -> tuple[str, list[str] | None]:
    status, codes = rows(committed_db, "SELECT status, reason_codes FROM emails WHERE id = %s",
                         email_id)[0]  # fmt: skip
    return status, codes


def refusals(committed_db: CommittedDb, email_id: int) -> list[Any]:
    query = ("SELECT output FROM audit_log WHERE email_id = %s AND event = 'agent.tool_called'"
             " AND output ->> 'tool' = 'write_draft' ORDER BY id")  # fmt: skip
    return [r[0] for r in rows(committed_db, query, email_id)]


def test_a_refused_draft_is_redrafted_through_process_email(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    email_id = stored(committed_db)
    llm = llm_with(committed_db, [Answer(EXTRACTION), write(BAD, 1), write(GOOD, 2)], tmp_path)
    process_email(committed_db.connect, CLOCK, llm, email_id, finish=agent_finish(llm))
    assert email(committed_db, email_id)[0] == "awaiting_approval"
    body = rows(committed_db, "SELECT v.body FROM draft_versions v JOIN drafts d ON"
                " d.id = v.draft_id WHERE d.email_id = %s", email_id)[0][0]  # fmt: skip
    assert "Friday 11 June 2027" in body  # the fixed draft, with the summary rendered
    assert refusals(committed_db, email_id) == [{
        "tool": "write_draft", "redraft": 1,
        "refused": ["{{REQUEST_SUMMARY}} missing: required for a relative or holiday date"],
    }]  # fmt: skip


def test_refused_twice_through_process_email_is_guardrail_fail(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    email_id = stored(committed_db)
    llm = llm_with(committed_db, [Answer(EXTRACTION), write(BAD, 1), write(BAD, 2)], tmp_path)
    process_email(committed_db.connect, CLOCK, llm, email_id, finish=agent_finish(llm))
    assert email(committed_db, email_id) == ("escalated", ["GUARDRAIL_FAIL"])
    assert len(refusals(committed_db, email_id)) == 2
    assert rows(committed_db, "SELECT count(*) FROM drafts")[0][0] == 0


def test_the_used_redraft_survives_a_stop_and_resume(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """The refusal is in the checkpoint (`redrafts`): after a resume, a second bad draft
    is GUARDRAIL_FAIL, not another chance."""

    def stop(_: Transcript) -> ToolCallStep:
        raise Stop

    email_id = stored(committed_db)
    llm = llm_with(committed_db, [Answer(EXTRACTION), write(BAD, 1), OnTranscript(stop)],
                   tmp_path)  # fmt: skip
    with pytest.raises(Stop):
        process_email(committed_db.connect, CLOCK, llm, email_id, finish=agent_finish(llm))
    again = llm_with(committed_db, [write(BAD, 2)], tmp_path)
    resume_run(committed_db.connect, CLOCK, again, email_id, finish=agent_finish(again))
    assert email(committed_db, email_id) == ("escalated", ["GUARDRAIL_FAIL"])
