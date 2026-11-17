"""run_cost (audit): one email's model cost from its audit rows, failed attempts
included, so a messy run is not under-costed."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from anemos.agent.tools import TOOLS_BY_ROUTE
from anemos.audit import StepCost, run_cost
from anemos.llm.adapter import (
    LLM,
    IncompleteReply,
    Prompt,
    ReplyCutOff,
    Settings,
    Transcript,
    TransientError,
    UserText,
)
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, ScriptedClient, Step
from anemos.llm.pricing import Usage, cost

if TYPE_CHECKING:
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

MODEL = "gpt-6-luna"
FIRST = Usage(1_000, 0, 50)  # billed even though the attempt failed
SECOND = Usage(1_000, 800, 40)


class Small(BaseModel):
    adults: int


def llm_on(committed_db: CommittedDb, steps: list[Step], tmp: Path) -> LLM:
    clock = committed_db.clock
    budget = Budget(clock, 1_000_000, tmp / "ledger.jsonl")
    return LLM(ScriptedClient.of(steps), Settings(MODEL, 100), budget, committed_db.connect,
               clock, sleep=lambda s: None)  # fmt: skip


def an_email(committed_db: CommittedDb) -> int:
    conn = committed_db.connect()
    email_id = committed_db.rows(conn).email()
    conn.commit()
    return email_id


def test_a_retried_call_counts_both_attempts(committed_db: CommittedDb, tmp_path: Path) -> None:
    email_id = an_email(committed_db)
    llm = llm_on(committed_db, [TransientError("HTTP 500", FIRST), Answer({"adults": 2}, SECOND)],
                 tmp_path)  # fmt: skip
    llm.structured(Prompt("x", "y"), Small, step="extract", prompt_version="p@1",
                   email_id=email_id)  # fmt: skip
    found = run_cost(committed_db.connect(), email_id)
    assert found.steps == {"extract": StepCost(
        attempts=2, failed=1, input_tokens=2_000, output_tokens=90,
        cost_micro_usd=cost(MODEL, FIRST) + cost(MODEL, SECOND), latency_ms=0,
    )}  # fmt: skip
    assert found.cost_micro_usd > cost(MODEL, SECOND)  # the failed try is not free


def test_a_cut_off_reply_counts_its_billed_usage(committed_db: CommittedDb,
                                                 tmp_path: Path) -> None:  # fmt: skip
    email_id = an_email(committed_db)
    llm = llm_on(committed_db, [IncompleteReply("max_output_tokens", FIRST)], tmp_path)
    with pytest.raises(ReplyCutOff):
        llm.tool_turn(Transcript("x", (UserText("y"),)), TOOLS_BY_ROUTE["policy"], step="agent",
                      prompt_version="a@1", email_id=email_id)  # fmt: skip
    found = run_cost(committed_db.connect(), email_id)
    assert found.steps["agent"].failed == 1
    assert found.cost_micro_usd == cost(MODEL, FIRST) > 0


def test_only_this_emails_billed_rows_count(committed_db: CommittedDb) -> None:
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    mine, other = rows.email(), rows.email()
    for email_id, event, step, micro_usd in [
        (mine, "llm.called", "extract", 100), (mine, "llm.failed", "agent", 40),
        (mine, "llm.called", "agent", 200), (mine, "llm.refused_budget", "summary", None),
        (other, "llm.called", "extract", 999),
    ]:  # fmt: skip
        rows.audit(email_id=email_id, actor="agent", event=event, step=step,
                   model=MODEL, cost_micro_usd=micro_usd)  # fmt: skip
    conn.commit()
    found = run_cost(conn, mine)
    assert (found.cost_micro_usd, found.attempts) == (340, 3)
    assert set(found.steps) == {"extract", "agent"}  # a budget refusal sent nothing
