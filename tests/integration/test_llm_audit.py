"""Model-call audit rows in Postgres: each attempt is one committed row, written in its
own transaction, with model, prompt_version, tokens, cost, latency and error."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel

from anemos.audit import LLM_EVENTS
from anemos.clock import FixedClock
from anemos.llm.adapter import LLM, LLMError, Prompt, Settings, TransientError
from anemos.llm.budget import Budget, BudgetExceeded
from anemos.llm.fake import Answer, ScriptedClient
from anemos.llm.pricing import Usage, cost

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=500)
PROMPT = Prompt(instructions="Extract.", input="<email>Hi</email>")
USAGE = Usage(1_000, 100, 250)


class Out(BaseModel):
    adults: int


def rows(committed_db: CommittedDb) -> list[dict[str, Any]]:
    conn = committed_db.connect()
    cur = conn.execute(
        "SELECT actor, event, email_id, step, model, prompt_version, input_tokens,"
        " output_tokens, cost_micro_usd, latency_ms, error, output->'result' AS result"
        " FROM audit_log ORDER BY id"
    )
    assert cur.description is not None
    names = [c.name for c in cur.description]
    found = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    conn.commit()
    return found


def llm(committed_db: CommittedDb, client: ScriptedClient, budget: Budget) -> LLM:
    ticks = iter([0.0, 0.8, 1.0, 1.4])
    return LLM(client, SETTINGS, budget, committed_db.connect, FixedClock(NOW),
               sleep=lambda s: None, timer=lambda: next(ticks))  # fmt: skip


def test_every_attempt_is_a_committed_audit_row(committed_db: CommittedDb, tmp_path: Path) -> None:
    conn = committed_db.connect()
    email_id = committed_db.rows(conn).email()
    conn.commit()
    budget = Budget(FixedClock(NOW), 1_000_000, tmp_path / "llm_spend.jsonl")
    client = ScriptedClient.of([TransientError("HTTP 503"), Answer({"adults": 2}, USAGE)])
    llm(committed_db, client, budget).structured(
        PROMPT, Out, step="extract", prompt_version="extraction_v1@abc", email_id=email_id
    )
    common = {"actor": "agent", "email_id": email_id, "step": "extract", "model": "gpt-6-luna",
              "prompt_version": "extraction_v1@abc"}  # fmt: skip
    assert rows(committed_db) == [
        common | {"event": "llm.failed", "input_tokens": None, "output_tokens": None,
                  "cost_micro_usd": 0, "latency_ms": 800, "error": "TransientError: HTTP 503",
                  "result": None},
        common | {"event": "llm.called", "input_tokens": 1_000, "output_tokens": 250,
                  "cost_micro_usd": cost("gpt-6-luna", USAGE), "latency_ms": 400,
                  "error": None, "result": {"adults": 2}},
    ]  # fmt: skip


def test_rows_stay_when_the_callers_transaction_rolls_back(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """D65: the run's own transaction may be rolled back (a newer email arrived); the
    call was still made and paid for, so its row must survive."""
    run = committed_db.connect()
    run.execute("SELECT 1")  # the run has a transaction open
    budget = Budget(FixedClock(NOW), 1_000_000, tmp_path / "llm_spend.jsonl")
    llm(committed_db, ScriptedClient.of([Answer({"adults": 1})]), budget).structured(
        PROMPT, Out, step="extract", prompt_version="v@1"
    )
    run.rollback()
    assert [r["event"] for r in rows(committed_db)] == ["llm.called"]


def test_failures_and_budget_refusals_are_on_record(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """Together with the test above, every event in audit.LLM_EVENTS is written to the
    database: the cross-rules coverage guard excludes them for that reason."""
    budget = Budget(FixedClock(NOW), 1_000_000, tmp_path / "llm_spend.jsonl")
    failing = ScriptedClient.of([TransientError("timeout"), TransientError("timeout")])
    with pytest.raises(LLMError):
        llm(committed_db, failing, budget).structured(PROMPT, Out, step="extract",
                                                     prompt_version="v@1")  # fmt: skip
    broke = Budget(FixedClock(NOW), 0, tmp_path / "empty.jsonl")
    with pytest.raises(BudgetExceeded):
        llm(committed_db, ScriptedClient.of([]), broke).structured(
            PROMPT, Out, step="extract", prompt_version="v@1"
        )
    events = [r["event"] for r in rows(committed_db)]
    assert events == ["llm.failed", "llm.failed", "llm.refused_budget"]
    assert {e.value for e in LLM_EVENTS} == {"llm.called", "llm.failed", "llm.refused_budget"}
