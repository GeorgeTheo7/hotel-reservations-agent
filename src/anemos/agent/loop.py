"""The agent loop (D11, D74): model turns and read tools until write_draft or escalate.

Runs in phase 2 of a run (D71): no transaction open while the model works; each tool
call is a short transaction on the run's connection (runner.py).

**The limit: 8 calls**, counting every turn that produced a call: a read tool, a call
the adapter rejected (UnknownTool, InvalidArguments), a refused call (an invalid query,
check_availability on a window), and the final write_draft or escalate. A rejected
call is answered under its own call_id with `{"error": ...}` and the loop goes on.
After 8 calls without write_draft or escalate: LOOP_LIMIT (an invalid write_draft as
the 8th call too).

**How it ends:**
- write_draft: `DraftWanted`, checked and stored by the run's final transaction (D72:
  route and hold by code; guardrails 1, 6, 7; placeholders).
- escalate: `EscalationWanted` with the model's code (one of six, D11) and note.
- a tool result the rules end at (runner `Ends`: a rule escalation, NO_POLICY,
  SYSTEM_ERROR, MODEL): escalated by code; the model is not asked again (D11: the
  model may escalate, never un-escalate).
- SYSTEM_ERROR (D49): a reply cut off at the output limit (no call_id to answer, and
  the same prompt would likely be cut off again), any other failed model call (no call,
  several calls, a refusal, a failed request), the budget refusing a call, or a bug in
  a tool. The email is escalated, never left `processing`.
"""

import json
import logging
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

from pydantic import BaseModel

from anemos.agent.runner import Answer, Ends
from anemos.agent.tools import (
    ESCALATE,
    TOOLS_BY_ROUTE,
    WRITE_DRAFT,
    EscalateArgs,
    ToolRoute,
    WriteDraftArgs,
)
from anemos.extraction_run import delimited_email
from anemos.llm.adapter import (
    LLM,
    InvalidArguments,
    RejectedCall,
    ReplyCutOff,
    ToolOutput,
    Transcript,
    UserText,
)
from anemos.policies.search import Hit
from anemos.prompts import CURRENT_AGENT, ToolPromptSpec
from anemos.reservations.errors import ReasonCode
from anemos.reservations.tools import StoredQuote

log = logging.getLogger("anemos.agent")

MAX_CALLS = 8  # D11, counting the final write_draft or escalate
STEP = "agent"

ROUTE_NOTES: dict[ToolRoute, str] = {
    "quote": "the guest asks for a stay; price it and offer it",
    "ask_info": "the guest asks for a stay, but a detail is missing",
    "decline": "the guest asks for a stay the hotel cannot offer",
    "policy": "a question about the hotel, with no stay asked for",
    "other": "not a stay request or a question about the hotel",
}


class Tools(Protocol):
    def run(self, name: str, args: BaseModel) -> Answer | Ends: ...


@dataclass(frozen=True)
class DraftWanted:
    text: str
    offer_quote_ids: tuple[int, ...]
    policy_ids: tuple[str, ...]


@dataclass(frozen=True)
class EscalationWanted:
    code: ReasonCode
    detail: str
    by: Literal["agent", "system"]  # the model's escalate, or code


@dataclass
class LoopState:
    """What the run keeps from the loop: the final write checks the draft against it."""

    transcript: Transcript
    calls: int = 0
    quotes: dict[int, StoredQuote] = field(default_factory=dict)  # priced in this run
    outcomes: set[str] = field(default_factory=set)  # CLOSED, NONE_FOUND, NEEDS_INFO (D72)
    closed_year: int | None = None  # the year {{OPEN_SEASON}} names
    hits: dict[str, Hit] = field(default_factory=dict)  # policy id -> what was returned

    def keep(self, answer: Answer) -> None:
        self.quotes.update({q.quote_id: q for q in answer.quotes})
        if answer.outcome is not None:
            self.outcomes.add(answer.outcome)
        if answer.closed_year is not None:
            self.closed_year = answer.closed_year
        self.hits.update({h.policy_id: h for h in answer.hits})


@dataclass(frozen=True)
class LoopResult:
    end: DraftWanted | EscalationWanted
    state: LoopState


def first_message(
    route: ToolRoute, detail: str, language: str | None, subject: str, body: str
) -> str:
    """What code tells the model, then the email (escaped, between the markers)."""
    reply = "Greek (el)" if language == "el" else "English (en)"
    lines = [f"Route: {route} ({ROUTE_NOTES[route]})"]
    if route == "ask_info" and detail:
        lines.append(f"Missing: {detail}")
    lines.append(f"Reply language: {reply}")
    return "\n".join(lines) + "\n\n" + delimited_email(subject, body)


def run_loop(
    llm: LLM,
    tools: Tools,
    *,
    route: ToolRoute,
    message: str,
    email_id: int,
    spec: ToolPromptSpec = CURRENT_AGENT,
) -> LoopResult:
    state = LoopState(Transcript(spec.text(), (UserText(message),)))
    try:
        end = _turns(llm, tools, state, route, email_id, spec)
    except Exception as exc:  # a failed call, the budget, a bug: escalated, never stuck
        log.exception("agent loop for email %s failed", email_id)
        end = EscalationWanted(ReasonCode.SYSTEM_ERROR, f"{type(exc).__name__}: {exc}",
                               "system")  # fmt: skip
    return LoopResult(end, state)


def _turns(
    llm: LLM, tools: Tools, state: LoopState, route: ToolRoute, email_id: int,
    spec: ToolPromptSpec,
) -> DraftWanted | EscalationWanted:  # fmt: skip
    offered = TOOLS_BY_ROUTE[route]
    settings = replace(llm.settings, max_output_tokens=spec.max_output_tokens,
                       reasoning_effort=spec.reasoning_effort)  # fmt: skip
    while state.calls < MAX_CALLS:
        try:
            turn = llm.tool_turn(state.transcript, offered, step=STEP,
                                 prompt_version=spec.version, email_id=email_id,
                                 settings=settings)  # fmt: skip
        except RejectedCall as exc:
            state.calls += 1
            kind = "INVALID_ARGUMENTS" if isinstance(exc, InvalidArguments) else "UNKNOWN_TOOL"
            state.transcript = state.transcript.then(
                exc.call, _output(exc.call.call_id, {"error": kind, "message": str(exc)})
            )
            continue
        except ReplyCutOff:
            return EscalationWanted(ReasonCode.SYSTEM_ERROR,
                                    "reply cut off at the output limit", "system")  # fmt: skip
        state.calls += 1
        call, args = turn.call, turn.args
        if call.name == WRITE_DRAFT:
            assert isinstance(args, WriteDraftArgs)
            return DraftWanted(args.text, tuple(args.offer_quote_ids), tuple(args.policy_ids))
        if call.name == ESCALATE:
            assert isinstance(args, EscalateArgs)
            return EscalationWanted(ReasonCode(args.reason_code), args.note, "agent")
        result = tools.run(call.name, args)
        if isinstance(result, Ends):
            return EscalationWanted(result.code, result.detail, "system")
        state.keep(result)
        state.transcript = state.transcript.then(call, _output(call.call_id, result.output))
    return EscalationWanted(
        ReasonCode.LOOP_LIMIT, f"{MAX_CALLS} calls without write_draft or escalate", "system"
    )


def _output(call_id: str, output: dict[str, object]) -> ToolOutput:
    return ToolOutput(call_id, json.dumps(output, ensure_ascii=False))
