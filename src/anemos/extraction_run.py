"""Extraction of one email, under D65's transaction boundaries.

1. Claim (short transaction, committed): the sender lock (D61), then the email must be
   `received`; it becomes `processing`. A thread reply (`in_reply_to` set) is escalated
   THREAD_REPLY in this same transaction and the run ends: no model call (D70).
2. The model call, with no transaction open and no lock held. The email's subject and
   body go between <email> markers as untrusted data; the model is never given today's
   date or received_at (D6): code resolves dates later.
3. The final write (one short transaction): the sender lock first, then the email must
   still be `processing` (the owner may have closed it, a newer email may have arrived
   and changed it). If it is not, nothing is written, and a `run.discarded` audit row is
   written in its own transaction afterwards. If the model call failed (budget, a model
   error, a bug), the email is escalated `SYSTEM_ERROR` (D49). Otherwise the extraction
   is stored (the request row; the email's language) and `finish` runs in the same
   transaction: routing and drafting hand the email on from there.
4. On an agent route (D71), `finish` returns `AgentStart`: the transaction of step 3
   commits first (extraction, request and routing decision; the email stays
   `processing`), then the agent nodes and their own final transaction run.

The steps are nodes of the run graph (anemos.graph, D75); `process_email` runs it.

The whole run uses one connection (no transaction open during a model call), so a run
does not churn local ports (NOTES 2026-11-16).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from anemos.audit import AuditEvent, write_audit
from anemos.clock import Clock
from anemos.extraction import ExtractionOutput
from anemos.inbox import escalate, lock_sender
from anemos.llm.adapter import LLM, Prompt
from anemos.prompts import CURRENT_EXTRACTION, PromptSpec
from anemos.reservations.datespec import resolve
from anemos.reservations.errors import ReasonCode
from anemos.reservations.formatting import Lang
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.model import Stay
from anemos.routing import EmailFacts, Escalate, Routed, SetAside, ToAgent, route
from anemos.states import EmailStatus, NotFound, TransitionRefused

type Conn = psycopg.Connection[TupleRow]

log = logging.getLogger("anemos.extraction")

EMAIL_OPEN, EMAIL_CLOSE = "<email>", "</email>"
# Every < and > in the guest's text becomes a single guillemet, so the email cannot
# write a marker of ours (</email>, or a block a prompt adds later, whatever its case,
# spacing or attributes) and close its own block early.
LEFT, RIGHT = chr(0x2039), chr(0x203A)  # single guillemets: readable, never a marker


@dataclass(frozen=True)
class Claimed:
    email_id: int
    from_address: str
    subject: str
    clean_body: str
    received_at: datetime
    in_reply_to: str | None = None


@dataclass(frozen=True)
class RunResult:
    email_id: int
    outcome: Literal["finished", "escalated", "discarded"]
    extraction: ExtractionOutput | None


@dataclass(frozen=True)
class AgentStart:
    """What `finish` returns on an agent route (D71): the run graph goes on to the agent
    nodes after the transaction commits."""

    decision: ToAgent
    request_id: int | None
    lang: Lang


@dataclass(frozen=True)
class EscalateLater:
    """What `finish` returns for an escalation by code that gets a summary (D81, D71):
    the transaction commits with the email still `processing`; the summary call runs
    with nothing held, then one final transaction escalates."""

    codes: tuple[ReasonCode, ...]
    detail: str


type Then = Callable[[Conn], Literal["finished", "discarded"]]
type Finish = Callable[
    [Conn, Clock, Claimed, ExtractionOutput], Then | AgentStart | EscalateLater | None
]


def _neutral(text: str) -> str:
    return text.replace("<", LEFT).replace(">", RIGHT)


def delimited_email(subject: str, body: str) -> str:
    """The guest's text, untouched except for < and > (escaped), inside the markers."""
    return f"{EMAIL_OPEN}\nSubject: {_neutral(subject)}\n\n{_neutral(body)}\n{EMAIL_CLOSE}"


def build_prompt(subject: str, body: str, spec: PromptSpec = CURRENT_EXTRACTION) -> Prompt:
    return Prompt(instructions=spec.text(), input=delimited_email(subject, body))


def claim(conn: Conn, clock: Clock, email_id: int) -> Claimed:
    """received -> processing (the caller commits)."""
    row = conn.execute("SELECT from_address FROM emails WHERE id = %s", (email_id,)).fetchone()
    if row is None:
        raise NotFound(f"no email {email_id}")
    lock_sender(conn, row[0])
    found = conn.execute(
        "SELECT status, from_address, subject, clean_body, received_at, in_reply_to"
        " FROM emails WHERE id = %s FOR UPDATE",
        (email_id,),
    ).fetchone()
    assert found is not None  # emails are never deleted (D51)
    status, address, subject, body, received_at, in_reply_to = found
    if status != EmailStatus.RECEIVED:
        raise TransitionRefused("email", email_id, status, EmailStatus.PROCESSING, action="extract")
    conn.execute("UPDATE emails SET status = %s WHERE id = %s", (EmailStatus.PROCESSING, email_id))
    write_audit(conn, clock.now(), "system", AuditEvent.EMAIL_PROCESSING, email_id=email_id)
    return Claimed(email_id, address, subject, body, received_at, in_reply_to)


def load_claimed(conn: Conn, email_id: int) -> Claimed:
    """The claimed email read again (a resumed run, D75); the caller ends the read."""
    row = conn.execute(
        "SELECT from_address, subject, clean_body, received_at, in_reply_to FROM emails"
        " WHERE id = %s",
        (email_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"no email {email_id}")
    return Claimed(email_id, *row)


def store_extraction(conn: Conn, clock: Clock, claimed: Claimed, out: ExtractionOutput) -> None:
    """The email's language; for a request, the requests row. The stay is resolved by
    code at the email's received_at; a window (Easter, part of a month) or dates that
    need asking keep stay NULL, with the date_spec in `extracted`."""
    conn.execute("UPDATE emails SET language = %s WHERE id = %s", (out.language, claimed.email_id))
    if out.kind != "request":
        return
    spec = out.date_spec_value()
    resolved = resolve(spec, claimed.received_at) if spec is not None else None
    stay = (
        Range(resolved.check_in, resolved.check_out, "[)") if isinstance(resolved, Stay) else None
    )
    conn.execute(
        "INSERT INTO requests (email_id, extracted, stay, adults, children, child_ages,"
        " rooms, room_type_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (claimed.email_id, Jsonb(out.model_dump(mode="json")), stay, out.adults,
         out.children, out.child_ages, out.rooms, out.room_type, clock.now()),
    )  # fmt: skip


def process_email(
    connect: Callable[[], Conn],
    clock: Clock,
    llm: LLM,
    email_id: int,
    *,
    finish: Finish,
    spec: PromptSpec = CURRENT_EXTRACTION,
) -> RunResult:
    """One run, as the run graph (anemos.graph, D75) on one connection."""
    from anemos.graph import run_email  # the graph imports this module's steps

    with connect() as conn:  # one connection for the whole run; checkpoints on another
        return run_email(conn, connect, clock, llm, email_id, finish=finish, spec=spec)


def extract(llm: LLM, claimed: Claimed, spec: PromptSpec) -> tuple[ExtractionOutput | None, str]:
    """2. The model call, with no transaction open: the extraction, or the error."""
    settings = replace(llm.settings, max_output_tokens=spec.max_output_tokens,
                       reasoning_effort=spec.reasoning_effort)  # fmt: skip
    try:
        result = llm.structured(
            build_prompt(claimed.subject, claimed.clean_body, spec), ExtractionOutput,
            step="extract", prompt_version=spec.version, email_id=claimed.email_id,
            settings=settings,
        )  # fmt: skip
    except Exception as exc:  # budget, model error or a bug: escalated, never stuck
        log.exception("extraction of email %s failed", claimed.email_id)
        return None, f"{type(exc).__name__}: {exc}"
    return result.output, ""


def escalate_thread_reply(conn: Conn, clock: Clock, claimed: Claimed) -> None:
    """Thread replies are routed before extraction: no model call (D70 rule 1)."""
    decision = route(EmailFacts(claimed.received_at, claimed.in_reply_to), None, None,
                     load_hotel_data(conn))  # fmt: skip
    assert isinstance(decision, Escalate), decision
    record_route(conn, clock.now(), claimed.email_id, decision)
    escalate(conn, clock.now(), claimed.email_id, decision.codes, decision.detail,
             actor="system")  # fmt: skip


def escalate_system_error(conn: Conn, now: datetime, email_id: int, error: str) -> None:
    escalate(conn, now, email_id, (ReasonCode.SYSTEM_ERROR,), error, actor="system")


def record_route(conn: Conn, now: datetime, email_id: int, decision: Routed) -> None:
    """The router's decision, audited for every routed email (D70): the eval and the
    monitoring compare it with what the email ended as."""
    match decision:
        case SetAside():
            output: dict[str, object] = {"outcome": "set_aside"}
        case Escalate(codes=codes, detail=detail):
            output = {"outcome": "escalate", "codes": [c.value for c in codes], "detail": detail}
        case ToAgent(route=agent_route, detail=detail):
            output = {"outcome": "to_agent", "route": agent_route, "detail": detail}
    write_audit(conn, now, "system", AuditEvent.EMAIL_ROUTED, email_id=email_id, output=output)


def discarded(conn: Conn, clock: Clock, email_id: int, status: str, *, step: str) -> None:
    """D65: the run's result was dropped because the email changed meanwhile; recorded
    in its own transaction, after the final write rolled back."""
    write_audit(
        conn, clock.now(), "system", AuditEvent.RUN_DISCARDED, email_id=email_id,
        output={"step": step, "email_status": status},
    )  # fmt: skip
    conn.commit()
