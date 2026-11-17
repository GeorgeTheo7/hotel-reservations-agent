"""The run of one email as a LangGraph `StateGraph` (D75, D17: one run per email).

The nodes are the existing steps, unchanged:

    claim -> extract -> store_and_route -> agent_turn <-> tool -> final_write

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

The graph state holds ids, the transcript, the loop's counter and plain facts; the
connection, the clock, the model and the objects the final write needs are in the
run's context (`RunContext`), never in the state (D75). The database stays the source
of truth for statuses (D51).
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Literal, cast

import psycopg
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from psycopg.rows import TupleRow

from anemos.agent.loop import (
    AGENT_TURN,
    DONE,
    RECURSION_LIMIT,
    TOOL,
    LoopContext,
    LoopFields,
    after_tool,
    after_turn,
    agent_turn,
    first_message,
    loop_result,
    start_fields,
    tool,
)
from anemos.agent.runner import ToolRunner
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
    store_extraction,
)
from anemos.inbox import lock_sender
from anemos.llm.adapter import LLM
from anemos.prompts import CURRENT_AGENT, PromptSpec
from anemos.run import final_write
from anemos.states import EmailStatus

type Conn = psycopg.Connection[TupleRow]
type Outcome = Literal["finished", "escalated", "discarded"]

CLAIM, EXTRACT, STORE_AND_ROUTE, FINAL_WRITE = "claim", "extract", "store_and_route", "final_write"


class RunState(LoopFields, total=False):
    email_id: int
    extraction: ExtractionOutput | None
    error: str  # why extraction failed
    outcome: Outcome  # set when the run has ended


@dataclass
class RunContext:
    """What the nodes use and the state does not hold. `claimed`, `start` and `loop`
    are filled by the nodes as the run goes."""

    conn: Conn
    clock: Clock
    llm: LLM
    finish: Finish
    spec: PromptSpec
    claimed: Claimed | None = None
    start: AgentStart | None = None
    loop: LoopContext | None = None


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
    assert ctx.claimed is not None
    extraction, error = extract(ctx.llm, ctx.claimed, ctx.spec)
    return {"extraction": extraction, "error": error}


def _store_and_route(state: RunState, runtime: Runtime[RunContext]) -> RunState:
    ctx, conn = runtime.context, runtime.context.conn
    claimed = ctx.claimed
    assert claimed is not None
    email_id, extraction = claimed.email_id, state.get("extraction")
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
        return {"outcome": "finished"}
    if not isinstance(then, AgentStart):
        return {"outcome": then(conn)}
    ctx.start = then  # D71 phase 2: the agent's nodes
    agent_route = then.decision.route
    tools = ToolRunner(conn, ctx.clock, ctx.llm, email_id, then.request_id)
    ctx.loop = LoopContext(ctx.llm, tools, agent_route, email_id, CURRENT_AGENT)
    message = first_message(agent_route, then.decision.detail, then.lang, claimed.subject,
                            claimed.clean_body)  # fmt: skip
    return cast(RunState, start_fields(message, CURRENT_AGENT))


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
    return {"outcome": final_write(ctx.conn, ctx.clock, ctx.claimed, ctx.start, loop)}


def _ended_or(next_node: str) -> Callable[[RunState], str]:
    def choose(state: RunState) -> str:
        return END if "outcome" in state else next_node

    return choose


@cache
def run_graph() -> CompiledStateGraph[RunState, RunContext, RunState, RunState]:
    g = StateGraph(RunState, context_schema=RunContext)
    g.add_node(CLAIM, _claim)
    g.add_node(EXTRACT, _extract)
    g.add_node(STORE_AND_ROUTE, _store_and_route)
    g.add_node(AGENT_TURN, _agent_turn)
    g.add_node(TOOL, _tool)
    g.add_node(FINAL_WRITE, _final_write)
    g.add_edge(START, CLAIM)
    g.add_conditional_edges(CLAIM, _ended_or(EXTRACT), [EXTRACT, END])
    g.add_edge(EXTRACT, STORE_AND_ROUTE)
    g.add_conditional_edges(STORE_AND_ROUTE, _ended_or(AGENT_TURN), [AGENT_TURN, END])
    g.add_conditional_edges(AGENT_TURN, after_turn,
                            {TOOL: TOOL, AGENT_TURN: AGENT_TURN, DONE: FINAL_WRITE})  # fmt: skip
    g.add_conditional_edges(TOOL, after_tool, {AGENT_TURN: AGENT_TURN, DONE: FINAL_WRITE})
    g.add_edge(FINAL_WRITE, END)
    return g.compile()


def run_email(
    conn: Conn, clock: Clock, llm: LLM, email_id: int, *, finish: Finish, spec: PromptSpec
) -> RunResult:
    """Run the graph for one email on the run's connection (process_email)."""
    ctx = RunContext(conn, clock, llm, finish, spec)
    final = run_graph().invoke({"email_id": email_id}, context=ctx,
                               config={"recursion_limit": RECURSION_LIMIT})  # fmt: skip
    state = cast(RunState, final)
    return RunResult(email_id, state["outcome"], state.get("extraction"))
