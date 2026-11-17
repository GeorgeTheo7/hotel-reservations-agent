"""The agent loop (D11, D74): model turns and read tools until write_draft or escalate.

Runs in phase 2 of a run (D71): no transaction open while the model works; each tool
call is a short transaction on the run's connection (runner.py).

**As graph nodes (D75):** `agent_turn` (one model turn) and `tool` (one read tool). The
graph state holds the transcript, the counter, plain facts (quote and policy ids, the
outcome codes, the closed year) and how the loop ended; the quote and policy objects
the final write renders and checks are kept in `LoopContext`, not in the state.
`run_loop` runs the two nodes on their own; the run graph (anemos.graph) adds them
between the routing and the final write.

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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel

from anemos.agent.checks import DraftFacts, check_draft
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
    Item,
    RejectedCall,
    ReplyCutOff,
    ToolCall,
    ToolOutput,
    Transcript,
    UserText,
)
from anemos.policies.search import Hit
from anemos.prompts import CURRENT_AGENT, ToolPromptSpec
from anemos.reservations.errors import ReasonCode
from anemos.reservations.tools import StoredQuote

log = logging.getLogger("anemos.agent")

MAX_CALLS = 8  # D11, counting the final write_draft or escalate, and refused drafts
MAX_REDRAFTS = 1  # D76: one more write_draft after a refused one
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


@dataclass
class LoopContext:
    """What the loop's nodes use and the graph state does not hold (D75): the model, the
    tools, and the evidence the tools returned (quotes and policy texts, kept as objects
    for the final write; their ids and the outcome codes are in the state). A resumed run
    rebuilds the evidence from the database (anemos.agent.evidence)."""

    llm: LLM
    tools: Tools
    route: ToolRoute
    email_id: int
    spec: ToolPromptSpec
    quotes: dict[int, StoredQuote] = field(default_factory=dict)
    hits: dict[str, Hit] = field(default_factory=dict)
    # D76: with facts, every write_draft is checked here and one redraft is allowed.
    # The run graph always passes them (anemos.graph); without them (run_loop's
    # default) only the final write checks.
    facts: DraftFacts | None = None
    record: Callable[[dict[str, Any]], None] | None = None  # audits a refused draft


type Json = dict[str, Any]


class LoopFields(TypedDict, total=False):
    """The loop's part of the graph state (D75), plain JSON values only (a checkpoint
    loads no class): the transcript, the counter, plain facts, the read tool waiting to
    run, and how the loop ended."""

    transcript: Json  # transcript_json
    calls: int
    redrafts: int  # write_draft refused by the checks so far (D76: at most 1)
    outcomes: list[str]  # CLOSED, NONE_FOUND, NEEDS_INFO (D72), in the order seen
    closed_year: int | None
    quote_ids: list[int]
    policy_ids: list[str]
    pending: Json | None  # the model's call, exactly as written (call_id, name, arguments)
    end: Json | None  # end_json


AGENT_TURN, TOOL, DONE = "agent_turn", "tool", "done"
# Supersteps: at most 8 turns, each followed by a tool node, plus the run's own nodes.
RECURSION_LIMIT = 40


# --- the state's JSON (D75) ---------------------------------------------------------------


def transcript_json(transcript: Transcript) -> Json:
    items: list[Json] = []
    for item in transcript.items:
        match item:
            case UserText(text=text):
                items.append({"type": "user_text", "text": text})
            case ToolCall(call_id=call_id, name=name, arguments=arguments):
                items.append({"type": "tool_call", "call_id": call_id, "name": name,
                              "arguments": arguments})  # fmt: skip
            case ToolOutput(call_id=call_id, output=output):
                items.append({"type": "tool_output", "call_id": call_id, "output": output})
    return {"instructions": transcript.instructions, "items": items}


def transcript_from_json(data: Json) -> Transcript:
    items: list[Item] = []
    for item in data["items"]:
        match item["type"]:
            case "user_text":
                items.append(UserText(item["text"]))
            case "tool_call":
                items.append(_call_from_json(item))
            case "tool_output":
                items.append(ToolOutput(item["call_id"], item["output"]))
            case other:
                raise ValueError(f"unknown transcript item {other!r}")
    return Transcript(data["instructions"], tuple(items))


def end_json(end: DraftWanted | EscalationWanted) -> Json:
    if isinstance(end, DraftWanted):
        return {"kind": "draft", "text": end.text, "offer_quote_ids": list(end.offer_quote_ids),
                "policy_ids": list(end.policy_ids)}  # fmt: skip
    return {"kind": "escalation", "code": end.code.value, "detail": end.detail, "by": end.by}


def end_from_json(data: Json) -> DraftWanted | EscalationWanted:
    if data["kind"] == "draft":
        return DraftWanted(data["text"], tuple(data["offer_quote_ids"]), tuple(data["policy_ids"]))
    return EscalationWanted(ReasonCode(data["code"]), data["detail"], data["by"])


def _call_json(call: ToolCall) -> Json:
    return {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}


def _call_from_json(data: Json) -> ToolCall:
    return ToolCall(data["call_id"], data["name"], data["arguments"])


# --- the nodes ------------------------------------------------------------------------------


def agent_turn(state: LoopFields, ctx: LoopContext) -> LoopFields:
    """One model turn. Ends the loop (`end`) on write_draft, escalate, a reply cut off,
    a failed turn or the 8-call limit; a rejected call is answered and counted; a read
    tool is left `pending` for the tool node."""
    calls = state.get("calls", 0)
    if calls >= MAX_CALLS:
        return {"end": end_json(EscalationWanted(
            ReasonCode.LOOP_LIMIT, f"{MAX_CALLS} calls without write_draft or escalate",
            "system"))}  # fmt: skip
    transcript = transcript_from_json(state["transcript"])
    spec = ctx.spec
    settings = replace(ctx.llm.settings, max_output_tokens=spec.max_output_tokens,
                       reasoning_effort=spec.reasoning_effort)  # fmt: skip
    try:
        turn = ctx.llm.tool_turn(transcript, TOOLS_BY_ROUTE[ctx.route], step=STEP,
                                 prompt_version=spec.version, email_id=ctx.email_id,
                                 settings=settings)  # fmt: skip
    except RejectedCall as exc:
        kind = "INVALID_ARGUMENTS" if isinstance(exc, InvalidArguments) else "UNKNOWN_TOOL"
        output = _output(exc.call.call_id, {"error": kind, "message": str(exc)})
        return {"calls": calls + 1,
                "transcript": transcript_json(transcript.then(exc.call, output))}  # fmt: skip
    except ReplyCutOff:
        return {"end": end_json(EscalationWanted(
            ReasonCode.SYSTEM_ERROR, "reply cut off at the output limit", "system"))}  # fmt: skip
    except Exception as exc:  # a failed call, the budget, a bug: escalated, never stuck
        return {"end": end_json(_failed(ctx, exc))}
    call, args = turn.call, turn.args
    if call.name == WRITE_DRAFT:
        assert isinstance(args, WriteDraftArgs)
        draft = DraftWanted(args.text, tuple(args.offer_quote_ids), tuple(args.policy_ids))
        if ctx.facts is not None:
            return _checked_draft(state, ctx, ctx.facts, transcript, call, draft, calls + 1)
        return {"calls": calls + 1, "end": end_json(draft)}
    if call.name == ESCALATE:
        assert isinstance(args, EscalateArgs)
        wanted = EscalationWanted(ReasonCode(args.reason_code), args.note, "agent")
        return {"calls": calls + 1, "end": end_json(wanted)}
    return {"calls": calls + 1, "pending": _call_json(call)}


def _checked_draft(
    state: LoopFields, ctx: LoopContext, facts: DraftFacts, transcript: Transcript,
    call: ToolCall, draft: DraftWanted, calls: int,
) -> LoopFields:  # fmt: skip
    """D76: a draft that passes ends the loop; the first that fails goes back to the
    model with the failed checks (its own text only: no rendered block, no price); the
    second is GUARDRAIL_FAIL. The call counts toward the 8 either way."""
    checked = check_draft(
        draft.text, draft.offer_quote_ids, draft.policy_ids, facts, quotes=ctx.quotes,
        hits=ctx.hits, outcomes=state.get("outcomes", []), closed_year=state.get("closed_year"),
    )  # fmt: skip
    if checked.passed:
        return {"calls": calls, "end": end_json(draft)}
    redrafts = state.get("redrafts", 0) + 1
    if ctx.record is not None:
        ctx.record({"tool": WRITE_DRAFT, "refused": checked.failures, "redraft": redrafts})
    if redrafts > MAX_REDRAFTS:
        failed = EscalationWanted(ReasonCode.GUARDRAIL_FAIL, "; ".join(checked.failures), "agent")
        return {"calls": calls, "redrafts": redrafts, "end": end_json(failed)}
    refusal: dict[str, object] = {"error": "GUARDRAIL_FAIL", "failed": checked.failures,
               "message": "The draft was not stored. Fix every failed check and call "
                          "write_draft again; this is the last chance before the email "
                          "goes to the owner."}  # fmt: skip
    output = _output(call.call_id, refusal)
    return {"calls": calls, "redrafts": redrafts,
            "transcript": transcript_json(transcript.then(call, output))}  # fmt: skip


def tool(state: LoopFields, ctx: LoopContext) -> LoopFields:
    """Run the pending read tool; its answer goes back to the model, an `Ends` ends the
    loop (escalated by code, D11). The arguments are validated again from the call as
    the model wrote it, as the adapter did (a resumed run has only the JSON)."""
    pending = state.get("pending")
    assert pending is not None
    call = _call_from_json(pending)
    try:
        offered = {t.name: t for t in TOOLS_BY_ROUTE[ctx.route]}
        args = offered[call.name].args.model_validate_json(call.arguments)
        result = ctx.tools.run(call.name, args)
    except Exception as exc:  # a bug in a tool: escalated, never stuck
        return {"pending": None, "end": end_json(_failed(ctx, exc))}
    if isinstance(result, Ends):
        ended = EscalationWanted(result.code, result.detail, "system")
        return {"pending": None, "end": end_json(ended)}
    ctx.quotes.update({q.quote_id: q for q in result.quotes})
    ctx.hits.update({h.policy_id: h for h in result.hits})
    outcomes = list(state.get("outcomes", []))
    if result.outcome is not None and result.outcome not in outcomes:
        outcomes.append(result.outcome)
    closed_year = result.closed_year if result.closed_year is not None else state.get("closed_year")
    transcript = transcript_from_json(state["transcript"])
    output = _output(call.call_id, result.output)
    return {
        "pending": None,
        "transcript": transcript_json(transcript.then(call, output)),
        "outcomes": outcomes,
        "closed_year": closed_year,
        "quote_ids": [*state.get("quote_ids", []), *(q.quote_id for q in result.quotes)],
        "policy_ids": [*state.get("policy_ids", []), *(h.policy_id for h in result.hits)],
    }


def after_turn(state: LoopFields) -> str:
    if state.get("end") is not None:
        return DONE
    return TOOL if state.get("pending") is not None else AGENT_TURN


def after_tool(state: LoopFields) -> str:
    return DONE if state.get("end") is not None else AGENT_TURN


def start_fields(message: str, spec: ToolPromptSpec) -> LoopFields:
    return {"transcript": transcript_json(Transcript(spec.text(), (UserText(message),))),
            "calls": 0}  # fmt: skip


def loop_result(state: LoopFields, ctx: LoopContext) -> LoopResult:
    """The loop's state and evidence as the final write reads them."""
    end = state.get("end")
    assert end is not None
    kept = LoopState(transcript_from_json(state["transcript"]), state.get("calls", 0),
                     dict(ctx.quotes), set(state.get("outcomes", [])),
                     state.get("closed_year"), dict(ctx.hits))  # fmt: skip
    return LoopResult(end_from_json(end), kept)


def _failed(ctx: LoopContext, exc: Exception) -> EscalationWanted:
    log.error("agent loop for email %s failed", ctx.email_id, exc_info=exc)
    return EscalationWanted(ReasonCode.SYSTEM_ERROR, f"{type(exc).__name__}: {exc}", "system")


def _loop_graph() -> CompiledStateGraph[LoopFields, LoopContext, LoopFields, LoopFields]:
    """The loop on its own (run_loop); the run graph (anemos.graph) adds the same two
    nodes between the routing and the final write."""
    g = StateGraph(LoopFields, context_schema=LoopContext)
    g.add_node(AGENT_TURN, _turn_node)
    g.add_node(TOOL, _tool_node)
    g.add_edge(START, AGENT_TURN)
    g.add_conditional_edges(AGENT_TURN, after_turn,
                            {TOOL: TOOL, AGENT_TURN: AGENT_TURN, DONE: END})  # fmt: skip
    g.add_conditional_edges(TOOL, after_tool, {AGENT_TURN: AGENT_TURN, DONE: END})
    return g.compile()


def _turn_node(state: LoopFields, runtime: Runtime[LoopContext]) -> LoopFields:
    return agent_turn(state, runtime.context)


def _tool_node(state: LoopFields, runtime: Runtime[LoopContext]) -> LoopFields:
    return tool(state, runtime.context)


def run_loop(
    llm: LLM,
    tools: Tools,
    *,
    route: ToolRoute,
    message: str,
    email_id: int,
    spec: ToolPromptSpec = CURRENT_AGENT,
    facts: DraftFacts | None = None,
) -> LoopResult:
    ctx = LoopContext(llm, tools, route, email_id, spec, facts=facts)
    state = _loop_graph().invoke(start_fields(message, spec), context=ctx,
                                 config={"recursion_limit": RECURSION_LIMIT})  # fmt: skip
    return loop_result(cast(LoopFields, state), ctx)


def _output(call_id: str, output: dict[str, object]) -> ToolOutput:
    return ToolOutput(call_id, json.dumps(output, ensure_ascii=False))
