"""The run of one email as a LangGraph `StateGraph` (D75, D17: one run per email).

The nodes are the existing steps:

    claim -> extract -> store_and_route -> agent_turn <-> tool -> final_write
                                    \\                               |
                                     +--------> await_owner <-------+

- `claim`: received -> processing, committed (a thread reply is escalated here and the
  run ends, no model call; D70).
- `extract`: the model call, with no transaction open and no lock held (D65).
- `store_and_route`: the final transaction of the extraction and `finish` (routing; a
  set-aside, an escalation or the stub drafter end the run there). On an agent route
  `finish` returns `AgentStart`; the transaction commits (D71 phase 1) and the run goes
  on to the agent's nodes (phase 2).
- `agent_turn` / `tool`: the agent loop (anemos.agent.loop), no transaction open
  during a model call, each tool call a short transaction.
- `final_write`: phase 3, one short transaction under the sender lock (anemos.run).
- `await_owner`: after a draft is stored (pending or born superseded), `interrupt()`.
  An owner action that ends the draft resumes the thread after its own transaction
  committed (`resume_after_owner`), only to record the run finished (`run.finished`).
  Supersede and expiry leave the thread interrupted (week-10 tidy-up).

**Checkpoints** (anemos.checkpoints): after every node, on the checkpointer's own
autocommit connection, `thread_id` = the email id. The state is plain JSON values: ids,
the transcript, the loop's counter and facts. The connection, the clock, the model and
the objects the final write needs are in `RunContext`, never in the state; a resumed run
rebuilds them (`resume_run`, anemos.agent.evidence). The database is the source of
truth (D51): a resume reads the email's status first and does nothing if it disagrees.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any, Literal, cast

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, StateSnapshot, interrupt
from psycopg.rows import TupleRow

from anemos.agent.checks import load_facts
from anemos.agent.evidence import rebuild_hits, rebuild_quotes
from anemos.agent.loop import (
    AGENT_TURN,
    DONE,
    RECURSION_LIMIT,
    TOOL,
    EscalationWanted,
    LoopContext,
    LoopFields,
    after_tool,
    after_turn,
    agent_turn,
    end_json,
    first_message,
    loop_result,
    start_fields,
    tool,
)
from anemos.agent.runner import ToolRunner
from anemos.audit import AuditEvent, write_audit
from anemos.checkpoints import checkpointer
from anemos.clock import Clock
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import (
    AgentStart,
    Claimed,
    Finish,
    RunResult,
    claim,
    discarded,
    escalate_system_error,
    escalate_thread_reply,
    extract,
    load_claimed,
    store_extraction,
)
from anemos.inbox import lock_sender
from anemos.llm.adapter import LLM
from anemos.prompts import CURRENT_AGENT, CURRENT_EXTRACTION, PromptSpec
from anemos.reservations.errors import ReasonCode
from anemos.routing import ToAgent
from anemos.run import final_write
from anemos.states import DraftStatus, EmailStatus

type Conn = psycopg.Connection[TupleRow]
type Connect = Callable[[], Conn]
type Outcome = Literal["finished", "escalated", "discarded"]

CLAIM, EXTRACT, STORE_AND_ROUTE = "claim", "extract", "store_and_route"
FINAL_WRITE, AWAIT_OWNER = "final_write", "await_owner"
# The draft statuses an owner action ends a draft in (D75: they resume the thread).
OWNER_ENDED = frozenset({DraftStatus.APPROVED, DraftStatus.REJECTED, DraftStatus.INVALIDATED})


class RunState(LoopFields, total=False):
    """The run's graph state: plain JSON values only (D75)."""

    email_id: int
    extraction: dict[str, Any] | None  # ExtractionOutput, as JSON
    error: str  # why extraction failed
    agent: dict[str, Any]  # AgentStart: route, detail, request_id, lang
    outcome: Outcome  # set when the run's work has ended
    draft_stored: bool  # a draft was stored: the run waits for the owner
    finished: str  # the draft's status when the owner's action resumed the thread


@dataclass
class RunContext:
    """What the nodes use and the state does not hold. `claimed`, `start` and `loop`
    are filled as the run goes, or rebuilt by `resume_run`. A resume after an owner
    action needs only the connection and the clock."""

    conn: Conn
    clock: Clock
    llm: LLM | None = None
    finish: Finish | None = None
    spec: PromptSpec = CURRENT_EXTRACTION
    claimed: Claimed | None = None
    start: AgentStart | None = None
    loop: LoopContext | None = None


# --- nodes ----------------------------------------------------------------------------------


def _claim(state: RunState, runtime: Runtime[RunContext]) -> RunState:
    ctx = runtime.context
    claimed = ctx.claimed = claim(ctx.conn, ctx.clock, state["email_id"])
    if claimed.in_reply_to is not None:
        escalate_thread_reply(ctx.conn, ctx.clock, claimed)
        ctx.conn.commit()
        return {"outcome": "escalated", "extraction": None}
    ctx.conn.commit()
    return {}


def _extract(state: RunState, runtime: Runtime[RunContext]) -> RunState:
    ctx = runtime.context
    assert ctx.claimed is not None and ctx.llm is not None
    extraction, error = extract(ctx.llm, ctx.claimed, ctx.spec)
    return {"extraction": None if extraction is None else extraction.model_dump(mode="json"),
            "error": error}  # fmt: skip


def _store_and_route(state: RunState, runtime: Runtime[RunContext]) -> RunState:
    ctx, conn = runtime.context, runtime.context.conn
    claimed = ctx.claimed
    assert claimed is not None and ctx.finish is not None and ctx.llm is not None
    email_id = claimed.email_id
    data = state.get("extraction")
    extraction = None if data is None else ExtractionOutput.model_validate(data)
    lock_sender(conn, claimed.from_address)
    row = conn.execute("SELECT status FROM emails WHERE id = %s FOR UPDATE", (email_id,)).fetchone()
    assert row is not None
    if row[0] != EmailStatus.PROCESSING:
        conn.rollback()
        discarded(conn, ctx.clock, email_id, row[0], step="extract")
        return {"outcome": "discarded"}
    if extraction is None:
        escalate_system_error(conn, ctx.clock.now(), email_id, state.get("error", ""))
        conn.commit()
        return {"outcome": "escalated"}
    store_extraction(conn, ctx.clock, claimed, extraction)
    then = ctx.finish(conn, ctx.clock, claimed, extraction)
    conn.commit()
    if then is None:
        return {"outcome": "finished", "draft_stored": _draft_stored(conn, email_id)}
    if not isinstance(then, AgentStart):
        outcome = then(conn)
        return {"outcome": outcome, "draft_stored": _draft_stored(conn, email_id)}
    _start_agent(ctx, then)  # D71 phase 2: the agent's nodes
    message = first_message(then.decision.route, then.decision.detail, then.lang,
                            claimed.subject, claimed.clean_body)  # fmt: skip
    agent = {"route": then.decision.route, "detail": then.decision.detail,
             "request_id": then.request_id, "lang": then.lang}  # fmt: skip
    return cast(RunState, {**start_fields(message, CURRENT_AGENT), "agent": agent})


def _agent_turn(state: RunState, runtime: Runtime[RunContext]) -> RunState:
    assert runtime.context.loop is not None
    return cast(RunState, agent_turn(state, runtime.context.loop))


def _tool(state: RunState, runtime: Runtime[RunContext]) -> RunState:
    assert runtime.context.loop is not None
    return cast(RunState, tool(state, runtime.context.loop))


def _final_write(state: RunState, runtime: Runtime[RunContext]) -> RunState:
    ctx = runtime.context
    assert ctx.claimed is not None and ctx.start is not None and ctx.loop is not None
    loop = loop_result(state, ctx.loop)
    outcome = final_write(ctx.conn, ctx.clock, ctx.claimed, ctx.start, loop)
    return {"outcome": outcome, "draft_stored": _draft_stored(ctx.conn, ctx.claimed.email_id)}


def _await_owner(state: RunState, runtime: Runtime[RunContext]) -> RunState:
    """Waits for the owner (`interrupt`); on resume, records the run finished with the
    draft's status as the database has it (D75: the database wins). Writes nothing
    before the interrupt: LangGraph runs the node again from the top on resume."""
    interrupt({"email_id": state["email_id"]})
    ctx, email_id = runtime.context, state["email_id"]
    status = _draft_status(ctx.conn, email_id)
    already = ctx.conn.execute(
        "SELECT 1 FROM audit_log WHERE email_id = %s AND event = %s",
        (email_id, AuditEvent.RUN_FINISHED.value),
    ).fetchone()
    if already is None:
        write_audit(ctx.conn, ctx.clock.now(), "system", AuditEvent.RUN_FINISHED,
                    email_id=email_id, output={"draft_status": status})  # fmt: skip
    ctx.conn.commit()
    return {"finished": str(status)}


# --- edges ----------------------------------------------------------------------------------


def _after_claim(state: RunState) -> str:
    return END if "outcome" in state else EXTRACT


def _after_store(state: RunState) -> str:
    if "outcome" not in state:
        return AGENT_TURN
    return AWAIT_OWNER if state.get("draft_stored") else END


def _after_final_write(state: RunState) -> str:
    return AWAIT_OWNER if state.get("draft_stored") else END


def _build(checkpointer_: BaseCheckpointSaver[Any] | None) -> CompiledStateGraph[
    RunState, RunContext, RunState, RunState
]:  # fmt: skip
    g = StateGraph(RunState, context_schema=RunContext)
    g.add_node(CLAIM, _claim)
    g.add_node(EXTRACT, _extract)
    g.add_node(STORE_AND_ROUTE, _store_and_route)
    g.add_node(AGENT_TURN, _agent_turn)
    g.add_node(TOOL, _tool)
    g.add_node(FINAL_WRITE, _final_write)
    g.add_node(AWAIT_OWNER, _await_owner)
    g.add_edge(START, CLAIM)
    g.add_conditional_edges(CLAIM, _after_claim, [EXTRACT, END])
    g.add_edge(EXTRACT, STORE_AND_ROUTE)
    g.add_conditional_edges(STORE_AND_ROUTE, _after_store, [AGENT_TURN, AWAIT_OWNER, END])
    g.add_conditional_edges(AGENT_TURN, after_turn,
                            {TOOL: TOOL, AGENT_TURN: AGENT_TURN, DONE: FINAL_WRITE})  # fmt: skip
    g.add_conditional_edges(TOOL, after_tool, {AGENT_TURN: AGENT_TURN, DONE: FINAL_WRITE})
    g.add_conditional_edges(FINAL_WRITE, _after_final_write, [AWAIT_OWNER, END])
    g.add_edge(AWAIT_OWNER, END)
    return g.compile(checkpointer=checkpointer_)


@cache
def run_graph() -> CompiledStateGraph[RunState, RunContext, RunState, RunState]:
    """The graph without a checkpointer: its shape (tests/unit/test_graph.py)."""
    return _build(None)


# --- running and resuming -------------------------------------------------------------------


def thread(email_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(email_id)}, "recursion_limit": RECURSION_LIMIT}


def run_email(
    conn: Conn, connect: Connect, clock: Clock, llm: LLM, email_id: int, *, finish: Finish,
    spec: PromptSpec,
) -> RunResult:  # fmt: skip
    """Run the graph for one email on the run's connection, checkpointing on another
    (process_email). One run per email (D17): a thread that already exists is a bug."""
    ctx = RunContext(conn, clock, llm, finish, spec)
    with checkpointer(connect) as saver:
        graph = _build(saver)
        if graph.get_state(thread(email_id)).values:
            raise RuntimeError(f"email {email_id} already has a run (thread exists)")
        final = graph.invoke({"email_id": email_id}, thread(email_id), context=ctx)
    return _result(email_id, cast(RunState, final))


def resume_run(
    connect: Connect, clock: Clock, llm: LLM, email_id: int, *, finish: Finish,
    spec: PromptSpec = CURRENT_EXTRACTION,
) -> RunResult | None:  # fmt: skip
    """Continue a run that stopped mid-way from its last checkpoint: finished nodes
    (and their model calls) are not repeated. Nothing to do (None) if the thread has no
    checkpoint, is waiting for the owner or has ended, or if the email is no longer
    `processing` (the database wins, D75). The loop's evidence is rebuilt from the
    database (anemos.agent.evidence); a difference ends the run SYSTEM_ERROR."""
    with connect() as conn, checkpointer(connect) as saver:
        graph = _build(saver)
        snapshot = graph.get_state(thread(email_id))
        if not snapshot.next or snapshot.interrupts:
            return None
        row = conn.execute("SELECT status FROM emails WHERE id = %s", (email_id,)).fetchone()
        conn.commit()
        if row is None or row[0] != EmailStatus.PROCESSING:
            return None
        state = cast(RunState, snapshot.values)
        ctx = RunContext(conn, clock, llm, finish, spec, claimed=load_claimed(conn, email_id))
        agent = state.get("agent")
        if agent is not None:
            start = AgentStart(ToAgent(agent["route"], agent["detail"]), agent["request_id"],
                               agent["lang"])  # fmt: skip
            _start_agent(ctx, start)
            assert ctx.loop is not None
            hits = rebuild_hits(conn, email_id, state.get("policy_ids", []))
            quotes = rebuild_quotes(conn, start.request_id, state.get("quote_ids", []))
            conn.commit()
            why = hits if isinstance(hits, str) else quotes if isinstance(quotes, str) else None
            if why is not None:  # end the loop: the final write escalates it
                ended = EscalationWanted(ReasonCode.SYSTEM_ERROR, f"resume refused: {why}",
                                         "system")  # fmt: skip
                graph.update_state(thread(email_id), {"pending": None, "end": end_json(ended)},
                                   as_node=TOOL)  # fmt: skip
            else:
                assert not isinstance(hits, str) and not isinstance(quotes, str)
                ctx.loop.hits, ctx.loop.quotes = hits, quotes
        conn.commit()
        final = graph.invoke(None, thread(email_id), context=ctx)
    return _result(email_id, cast(RunState, final))


def resume_after_owner(connect: Connect, clock: Clock, email_id: int) -> bool:
    """After an owner action that ended the draft has committed: resume the thread only
    to record the run finished. Idempotent: False, and nothing done, if the thread is
    not waiting (no checkpoint, already finished) or the draft is not in a status an
    owner action ends it in (the database wins)."""
    with connect() as conn, checkpointer(connect) as saver:
        graph = _build(saver)
        if not graph.get_state(thread(email_id)).interrupts:
            return False
        status = _draft_status(conn, email_id)
        if status not in OWNER_ENDED:
            return False
        graph.invoke(Command(resume=status), thread(email_id), context=RunContext(conn, clock))
    return True


def thread_snapshot(connect: Connect, email_id: int) -> StateSnapshot:
    """The run's thread as last checkpointed: its state values, the next nodes, and
    `interrupts` while it waits for the owner (tests; the week-10 tidy-up)."""
    with checkpointer(connect) as saver:
        return _build(saver).get_state(thread(email_id))


def _start_agent(ctx: RunContext, start: AgentStart) -> None:
    assert ctx.llm is not None and ctx.claimed is not None
    email_id = ctx.claimed.email_id
    ctx.start = start
    tools = ToolRunner(ctx.conn, ctx.clock, ctx.llm, email_id, start.request_id)
    # D76: the loop checks every write_draft with the run's facts (one redraft).
    facts = load_facts(ctx.conn, start.request_id, start.decision.route, start.lang)
    ctx.conn.commit()  # a read: no transaction left open during the loop (D71)
    ctx.loop = LoopContext(ctx.llm, tools, start.decision.route, email_id, CURRENT_AGENT,
                           facts=facts, record=tools.record)  # fmt: skip


def _draft_status(conn: Conn, email_id: int) -> str | None:
    row = conn.execute("SELECT status FROM drafts WHERE email_id = %s", (email_id,)).fetchone()
    conn.commit()  # a read: no transaction left open
    return None if row is None else str(row[0])


def _draft_stored(conn: Conn, email_id: int) -> bool:
    return _draft_status(conn, email_id) in (DraftStatus.PENDING_APPROVAL, DraftStatus.SUPERSEDED)


def _result(email_id: int, state: RunState) -> RunResult:
    data = state.get("extraction")
    extraction = None if data is None else ExtractionOutput.model_validate(data)
    return RunResult(email_id, state["outcome"], extraction)
