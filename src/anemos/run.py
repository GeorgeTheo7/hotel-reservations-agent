"""One run per email (D17): extraction, then routing in code (D70), then the draft.

`process_email` (extraction_run) claims the email, calls the model with nothing held,
and in its final transaction stores the extraction and calls a `finish`:

- `agent_finish(llm)` (production, `process_inbox`): the router's decision is audited
  (`email.routed`); SetAside and Escalate are written in that transaction. ToAgent
  returns `AgentStart`, and the run graph (anemos.graph, D75) runs the agent in D71's
  three phases: (1) that transaction commits (extraction, request, routing; the email
  stays `processing`); (2) the agent loop's nodes, with no transaction open, each tool
  call a short transaction on the run's connection; (3) `final_write`: one short
  transaction under the sender lock: if the email is no longer `processing`, the run
  is discarded (audited); else the draft or the escalation is written (agent/final.py).
- `route_and_draft` (the demo inbox and the extraction eval, until the gold agent):
  the same routing, then the stub drafter on routes quote, ask_info and decline;
  routes policy and other, which only the agent answers, escalate MODEL.

Thread replies never get here: `process_email` escalates them before the model call.
"""

import logging
from collections.abc import Callable
from typing import Literal

import psycopg
from psycopg.rows import TupleRow

from anemos.agent.final import finish_agent
from anemos.agent.loop import EscalationWanted, LoopResult
from anemos.clock import Clock
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import (
    AgentStart,
    Claimed,
    EscalateLater,
    Finish,
    RunResult,
    discarded,
    process_email,
    record_route,
)
from anemos.inbox import escalate, lock_sender, set_aside
from anemos.llm.adapter import LLM
from anemos.reservations.datespec import resolve
from anemos.reservations.errors import ReasonCode
from anemos.reservations.formatting import Lang
from anemos.reservations.loader import load_hotel_data
from anemos.routing import EmailFacts, Escalate, Routed, SetAside, ToAgent, route
from anemos.states import EmailStatus, TransitionRefused
from anemos.stub_drafter import draft_claimed
from anemos.summary import Summary, wanted

type Conn = psycopg.Connection[TupleRow]

log = logging.getLogger("anemos.run")


def routed(
    conn: Conn, clock: Clock, claimed: Claimed, out: ExtractionOutput, *, defer: bool = False
) -> Routed:
    """Route the email (D70) inside the final transaction; audit the decision; write a
    set-aside or an escalation. The hotel data is read in the same transaction. With
    `defer`, an escalation that gets a summary (D81) is not written here: the caller
    returns EscalateLater."""
    hotel = load_hotel_data(conn)
    spec = out.date_spec_value()
    resolved = None if spec is None else resolve(spec, claimed.received_at)
    decision = route(EmailFacts(claimed.received_at, claimed.in_reply_to), out, resolved, hotel)
    now = clock.now()
    record_route(conn, now, claimed.email_id, decision)
    match decision:
        case SetAside():
            set_aside(conn, now, claimed.email_id)
        case Escalate(codes=codes, detail=detail):
            if not (defer and wanted(codes)):
                escalate(conn, now, claimed.email_id, codes, detail, actor="system")
        case ToAgent():
            pass
    return decision


def route_and_draft(conn: Conn, clock: Clock, claimed: Claimed, out: ExtractionOutput) -> None:
    """The stub drafter's finish (demo inbox, extraction eval): no agent model calls."""
    decision = routed(conn, clock, claimed, out)
    if isinstance(decision, ToAgent):
        if decision.route in ("policy", "other"):
            escalate(conn, clock.now(), claimed.email_id, (ReasonCode.MODEL,),
                     f"kind {out.kind}: only the agent loop answers it, not the stub drafter",
                     actor="system")  # fmt: skip
        else:
            draft_claimed(conn, load_hotel_data(conn), clock, claimed.email_id)


def agent_finish(llm: LLM) -> Finish:
    """The production finish: routing, then the agent in D71's three phases (the run
    graph's agent nodes and final write, anemos.graph). `llm` is the run's model; the
    graph's context carries it to the agent nodes."""

    def finish(
        conn: Conn, clock: Clock, claimed: Claimed, out: ExtractionOutput
    ) -> AgentStart | EscalateLater | None:
        decision = routed(conn, clock, claimed, out, defer=True)
        if isinstance(decision, Escalate) and wanted(decision.codes):
            return EscalateLater(tuple(decision.codes), decision.detail)  # D81
        if not isinstance(decision, ToAgent):
            return None
        row = conn.execute(
            "SELECT id FROM requests WHERE email_id = %s", (claimed.email_id,)
        ).fetchone()
        request_id = None if row is None else int(row[0])
        lang: Lang = "el" if out.language == "el" else "en"
        return AgentStart(decision, request_id, lang)

    return finish


def final_write(
    conn: Conn,
    clock: Clock,
    claimed: Claimed,
    start: AgentStart,
    loop: LoopResult,
    summary: Summary | None = None,
) -> Literal["finished", "discarded"]:
    """Phase 3 (D71): one short transaction under the sender lock; discarded if the
    email is no longer `processing`."""
    email_id = claimed.email_id
    lock_sender(conn, claimed.from_address)
    row = conn.execute("SELECT status FROM emails WHERE id = %s FOR UPDATE", (email_id,)).fetchone()
    assert row is not None
    if row[0] != EmailStatus.PROCESSING:
        conn.rollback()
        discarded(conn, clock, email_id, row[0], step="agent")
        return "discarded"
    try:
        finish_agent(conn, clock.now(), email_id, loop, router_route=start.decision.route,
                     request_id=start.request_id, lang=start.lang, summary=summary)  # fmt: skip
        conn.commit()
    except Exception as exc:  # a bug in the final write: escalated, never stuck (D49)
        log.exception("final write for email %s failed", email_id)
        conn.rollback()
        _system_error(conn, clock, claimed, loop, f"{type(exc).__name__}: {exc}")
    return "finished"


def _system_error(conn: Conn, clock: Clock, claimed: Claimed, loop: LoopResult,
                  error: str) -> None:  # fmt: skip
    lock_sender(conn, claimed.from_address)
    row = conn.execute(
        "SELECT status FROM emails WHERE id = %s FOR UPDATE", (claimed.email_id,)
    ).fetchone()
    if row is not None and row[0] == EmailStatus.PROCESSING:
        ended = loop.end.code.value if isinstance(loop.end, EscalationWanted) else "draft"
        escalate(conn, clock.now(), claimed.email_id, (ReasonCode.SYSTEM_ERROR,),
                 f"final write failed after the loop ended ({ended}): {error}",
                 actor="system")  # fmt: skip
    conn.commit()


def write_escalation(
    conn: Conn, clock: Clock, claimed: Claimed, later: EscalateLater, summary: Summary | None
) -> Literal["finished", "discarded"]:
    """The final transaction of an escalation by code that got its summary (D81, D71):
    under the sender lock, if the email is still `processing`."""
    lock_sender(conn, claimed.from_address)
    row = conn.execute(
        "SELECT status FROM emails WHERE id = %s FOR UPDATE", (claimed.email_id,)
    ).fetchone()
    assert row is not None
    if row[0] != EmailStatus.PROCESSING:
        conn.rollback()
        discarded(conn, clock, claimed.email_id, row[0], step="summary")
        return "discarded"
    escalate(conn, clock.now(), claimed.email_id, later.codes, later.detail, actor="system",
             summary=None if summary is None else summary.text,
             model_added=() if summary is None else summary.added)  # fmt: skip
    conn.commit()
    return "finished"


def process_inbox(
    connect: Callable[[], Conn], clock: Clock, llm: LLM, *, finish: Finish | None = None
) -> list[RunResult]:
    """Run every `received` email, oldest first (received_at, then id: D48's order), one
    run each, with the agent unless another finish is given. An email another worker
    claimed meanwhile is skipped."""
    finish = finish or agent_finish(llm)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM emails WHERE status = %s ORDER BY received_at, id",
            (EmailStatus.RECEIVED,),
        ).fetchall()
        conn.commit()
    results = []
    for (email_id,) in rows:
        try:
            results.append(process_email(connect, clock, llm, email_id, finish=finish))
        except TransitionRefused:
            continue
    return results
