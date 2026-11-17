"""The audit log (spec Data model; write-once, D51): every event name in one place.

Event names have the form `area.action`; the database CHECK `audit_log_event_format`
refuses anything else, and a test inserts every name below to prove each is accepted.
Add a name here before using it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

type Actor = Literal["owner", "agent", "system"]


class AuditEvent(StrEnum):
    EMAIL_STORED = "email.stored"
    EMAIL_PROCESSING = "email.processing"
    EMAIL_ESCALATED = "email.escalated"
    EMAIL_CLOSED = "email.closed"
    EMAIL_REOPENED = "email.reopened"
    EMAIL_ACTION_REFUSED = "email.action_refused"
    EMAIL_ROUTED = "email.routed"
    EMAIL_SET_ASIDE = "email.set_aside"
    DRAFT_CREATED = "draft.created"
    DRAFT_EDITED = "draft.edited"
    DRAFT_REJECTED = "draft.rejected"
    DRAFT_EXPIRED = "draft.expired"
    DRAFT_ACTION_REFUSED = "draft.action_refused"
    DRAFT_PRICE_WARNING_SHOWN = "draft.price_warning_shown"
    DRAFT_GUARDRAIL_WARNING_SHOWN = "draft.guardrail_warning_shown"  # D77, one per warning
    DRAFT_SUPERSEDED = "draft.superseded"
    DRAFT_APPROVED = "draft.approved"
    DRAFT_INVALIDATED = "draft.invalidated"
    HOLD_EXPIRED = "hold.expired"
    HOLD_CONFIRMED = "hold.confirmed"
    HOLD_RELEASED = "hold.released"
    BOOKING_CANCELLED = "booking.cancelled"
    BOOKING_ACTION_REFUSED = "booking.action_refused"
    OUTBOX_SENT = "outbox.sent"
    OUTBOX_SEND_FAILED = "outbox.send_failed"
    LLM_CALLED = "llm.called"
    LLM_FAILED = "llm.failed"
    LLM_REFUSED_BUDGET = "llm.refused_budget"
    RUN_DISCARDED = "run.discarded"
    RUN_FINISHED = "run.finished"  # D75: the owner's action ended the draft; thread resumed
    RUN_RESUME_FAILED = "run.resume_failed"  # D75: that resume failed; thread left waiting
    AGENT_TOOL_CALLED = "agent.tool_called"


# Written only by the API, after it rolled back a refused or unconfirmed action
# (api/transactions.py). Domain code never writes them, so the cross-rules scenario,
# which calls the domain functions directly, does not either; tests/integration/
# test_api_actions.py covers them.
API_ONLY_EVENTS = frozenset(
    {
        AuditEvent.EMAIL_ACTION_REFUSED,
        AuditEvent.BOOKING_ACTION_REFUSED,
        AuditEvent.DRAFT_PRICE_WARNING_SHOWN,
        AuditEvent.DRAFT_GUARDRAIL_WARNING_SHOWN,
        AuditEvent.RUN_RESUME_FAILED,  # after an owner action committed (D75)
    }
)

# Written only by model calls (anemos.llm). The cross-rules scenario makes no model
# call, so it never writes them; tests/integration/test_llm_audit.py covers them.
LLM_EVENTS = frozenset(
    {AuditEvent.LLM_CALLED, AuditEvent.LLM_FAILED, AuditEvent.LLM_REFUSED_BUDGET}
)


# Written only by a run (extraction and routing, anemos.run): a run whose email changed
# while the model was working (D65), the router's decision for every email (D70), and
# set-aside spam, each agent tool call (D11), and a run's thread resumed after the
# owner's action (D75). The cross-rules scenario runs no model, so it never writes them;
# tests/integration/test_extraction_run.py, test_run.py, test_agent_runner.py and
# test_graph_resume.py cover them.
RUN_EVENTS = frozenset(
    {AuditEvent.RUN_DISCARDED, AuditEvent.EMAIL_ROUTED, AuditEvent.EMAIL_SET_ASIDE,
     AuditEvent.AGENT_TOOL_CALLED, AuditEvent.RUN_FINISHED}
)  # fmt: skip


def write_audit(
    conn: psycopg.Connection[TupleRow],
    at: datetime,
    actor: Actor,
    event: AuditEvent,
    *,
    email_id: int | None = None,
    draft_id: int | None = None,
    output: Mapping[str, object] | None = None,
) -> None:
    """Append one entry in the caller's transaction (no commit)."""
    conn.execute(
        "INSERT INTO audit_log (at, actor, event, email_id, draft_id, output)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (at, actor, event.value, email_id, draft_id, None if output is None else Jsonb(output)),
    )


def write_llm_audit(
    conn: psycopg.Connection[TupleRow],
    at: datetime,
    event: AuditEvent,
    *,
    email_id: int | None,
    step: str,
    model: str,
    prompt_version: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_micro_usd: int | None = None,
    latency_ms: int | None = None,
    error: str | None = None,
    output: Mapping[str, object] | None = None,
) -> None:
    """One model-call attempt (actor agent), in the caller's transaction (no commit)."""
    if event not in LLM_EVENTS:
        raise ValueError(f"{event} is not a model-call event")
    conn.execute(
        "INSERT INTO audit_log (at, actor, event, email_id, step, model, prompt_version,"
        " input_tokens, output_tokens, cost_micro_usd, latency_ms, error, output)"
        " VALUES (%s, 'agent', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (at, event.value, email_id, step, model, prompt_version, input_tokens, output_tokens,
         cost_micro_usd, latency_ms, error, None if output is None else Jsonb(output)),
    )  # fmt: skip


# A run's own ending (D74, D65, states.md): exactly one of these per run, written by
# the run. What the owner does later (approve, edit, run.finished) is not the run's.
TERMINAL_EVENTS = frozenset({
    AuditEvent.EMAIL_ESCALATED, AuditEvent.EMAIL_SET_ASIDE, AuditEvent.DRAFT_CREATED,
    AuditEvent.RUN_DISCARDED,
})  # fmt: skip


@dataclass(frozen=True)
class StepCost:
    """The model calls of one step (extract, agent, policy_select, summary)."""

    attempts: int  # every billed attempt: llm.called and llm.failed rows
    failed: int  # llm.failed rows: a retried attempt, a cut-off reply, a rejected call
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    latency_ms: int


@dataclass(frozen=True)
class RunCost:
    steps: dict[str, StepCost]

    @property
    def cost_micro_usd(self) -> int:
        return sum(s.cost_micro_usd for s in self.steps.values())

    @property
    def attempts(self) -> int:
        return sum(s.attempts for s in self.steps.values())


def run_cost(conn: psycopg.Connection[TupleRow], email_id: int) -> RunCost:
    """What one email's run cost, from its audit rows: the one definition the agent eval
    and monitoring share. Failed attempts count too (llm.failed rows carry the usage
    the provider billed: a cut-off reply, a rejected tool call, a first try before a
    retry), so a messy run is not under-costed. Cached input is billed at its own price
    inside cost_micro_usd; the audit stores no separate cached count. A budget refusal
    sent nothing and costs nothing (llm.refused_budget is not counted)."""
    rows = conn.execute(
        "SELECT step, event, coalesce(input_tokens, 0), coalesce(output_tokens, 0),"
        " coalesce(cost_micro_usd, 0), coalesce(latency_ms, 0) FROM audit_log"
        " WHERE email_id = %s AND event IN (%s, %s) ORDER BY id",
        (email_id, AuditEvent.LLM_CALLED.value, AuditEvent.LLM_FAILED.value),
    ).fetchall()
    steps: dict[str, StepCost] = {}
    for step, event, tokens_in, tokens_out, micro_usd, latency in rows:
        was = steps.get(step, StepCost(0, 0, 0, 0, 0, 0))
        steps[step] = StepCost(
            was.attempts + 1, was.failed + (event == AuditEvent.LLM_FAILED.value),
            was.input_tokens + tokens_in, was.output_tokens + tokens_out,
            was.cost_micro_usd + micro_usd, was.latency_ms + latency,
        )  # fmt: skip
    return RunCost(steps)
