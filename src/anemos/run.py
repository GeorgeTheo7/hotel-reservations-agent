"""One run per email (D17): extraction, then routing in code (D70), then the draft.

`process_email` (extraction_run) claims the email, calls the model with nothing held,
and in its final transaction stores the extraction and calls a `finish`:

- `agent_finish(llm)` (production, `process_inbox`): the router's decision is audited
  (`email.routed`); SetAside and Escalate are written in that transaction. ToAgent runs
  the agent in D71's three phases: (1) that transaction commits (extraction, request,
  routing; the email stays `processing`); (2) the agent loop, with no transaction open,
  each tool call a short transaction on the run's connection; (3) one short final
  transaction under the sender lock: if the email is no longer `processing`, the run is
  discarded (audited); else the draft or the escalation is written (agent/final.py).
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
from anemos.agent.loop import EscalationWanted, LoopResult, first_message, run_loop
from anemos.agent.runner import ToolRunner
from anemos.clock import Clock
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import (
    Claimed,
    Finish,
    RunResult,
    Then,
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

type Conn = psycopg.Connection[TupleRow]

log = logging.getLogger("anemos.run")


def routed(conn: Conn, clock: Clock, claimed: Claimed, out: ExtractionOutput) -> Routed:
    """Route the email (D70) inside the final transaction; audit the decision; write a
    set-aside or an escalation. The hotel data is read in the same transaction."""
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
    """The production finish: routing, then the agent in D71's three phases."""

    def finish(conn: Conn, clock: Clock, claimed: Claimed, out: ExtractionOutput) -> Then | None:
        decision = routed(conn, clock, claimed, out)
        if not isinstance(decision, ToAgent):
            return None
        row = conn.execute(
            "SELECT id FROM requests WHERE email_id = %s", (claimed.email_id,)
        ).fetchone()
        request_id = None if row is None else int(row[0])
        lang: Lang = "el" if out.language == "el" else "en"

        def then(c: Conn) -> Literal["finished", "discarded"]:
            return _agent(c, clock, llm, claimed, decision, request_id, lang)

        return then

    return finish


def _agent(
    conn: Conn, clock: Clock, llm: LLM, claimed: Claimed, decision: ToAgent,
    request_id: int | None, lang: Lang,
) -> Literal["finished", "discarded"]:  # fmt: skip
    """Phases 2 and 3 (D71). Phase 1 has committed: the email is `processing`."""
    email_id = claimed.email_id
    tools = ToolRunner(conn, clock, llm, email_id, request_id)
    message = first_message(decision.route, decision.detail, lang, claimed.subject,
                            claimed.clean_body)  # fmt: skip
    loop = run_loop(llm, tools, route=decision.route, message=message, email_id=email_id)

    lock_sender(conn, claimed.from_address)  # phase 3: the final write
    row = conn.execute("SELECT status FROM emails WHERE id = %s FOR UPDATE", (email_id,)).fetchone()
    assert row is not None
    if row[0] != EmailStatus.PROCESSING:
        conn.rollback()
        discarded(conn, clock, email_id, row[0], step="agent")
        return "discarded"
    try:
        finish_agent(conn, clock.now(), email_id, loop, router_route=decision.route,
                     request_id=request_id, lang=lang)  # fmt: skip
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
