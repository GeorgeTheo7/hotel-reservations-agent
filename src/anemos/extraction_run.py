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
   transaction: routing and drafting (step 5) hand the email on from there.
"""

import logging
import re
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
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.model import Stay
from anemos.routing import EmailFacts, Escalate, Routed, SetAside, ToAgent, route
from anemos.states import EmailStatus, NotFound, TransitionRefused

type Conn = psycopg.Connection[TupleRow]

log = logging.getLogger("anemos.extraction")

EMAIL_OPEN, EMAIL_CLOSE = "<email>", "</email>"
# Anything in the guest's text that looks like one of our markers (any case, spacing
# or attributes) is neutralised, so the email cannot close its own block early.
_MARKER = re.compile(r"<\s*/?\s*email\b[^>]*>", re.IGNORECASE)
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


type Finish = Callable[[Conn, Clock, Claimed, ExtractionOutput], None]


def _neutral(text: str) -> str:
    return _MARKER.sub(lambda m: m.group(0).replace("<", LEFT).replace(">", RIGHT), text)


def delimited_email(subject: str, body: str) -> str:
    """The guest's text, untouched except for marker look-alikes, inside the markers."""
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
    with connect() as conn:  # 1. claim
        claimed = claim(conn, clock, email_id)
        if claimed.in_reply_to is not None:
            # Thread replies are routed before extraction: no model call (D70 rule 1).
            decision = route(EmailFacts(claimed.received_at, claimed.in_reply_to), None, None,
                             load_hotel_data(conn))  # fmt: skip
            assert isinstance(decision, Escalate), decision
            record_route(conn, clock.now(), email_id, decision)
            escalate(conn, clock.now(), email_id, decision.codes, decision.detail, actor="system")
            conn.commit()
            return RunResult(email_id, "escalated", None)
        conn.commit()

    extraction: ExtractionOutput | None = None  # 2. the model, nothing held
    error = ""
    settings = replace(llm.settings, max_output_tokens=spec.max_output_tokens,
                       reasoning_effort=spec.reasoning_effort)  # fmt: skip
    try:
        result = llm.structured(
            build_prompt(claimed.subject, claimed.clean_body, spec), ExtractionOutput,
            step="extract", prompt_version=spec.version, email_id=email_id, settings=settings,
        )  # fmt: skip
        extraction = result.output
    except Exception as exc:  # budget, model error or a bug: escalated, never stuck
        log.exception("extraction of email %s failed", email_id)
        error = f"{type(exc).__name__}: {exc}"

    with connect() as conn:  # 3. the final write
        lock_sender(conn, claimed.from_address)
        row = conn.execute(
            "SELECT status FROM emails WHERE id = %s FOR UPDATE", (email_id,)
        ).fetchone()
        assert row is not None
        if row[0] != EmailStatus.PROCESSING:
            conn.rollback()
            _discarded(connect, clock, email_id, row[0])
            return RunResult(email_id, "discarded", extraction)
        if extraction is None:
            _escalate_system_error(conn, clock.now(), email_id, error)
            conn.commit()
            return RunResult(email_id, "escalated", None)
        store_extraction(conn, clock, claimed, extraction)
        finish(conn, clock, claimed, extraction)
        conn.commit()
    return RunResult(email_id, "finished", extraction)


def _escalate_system_error(conn: Conn, now: datetime, email_id: int, error: str) -> None:
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


def _discarded(connect: Callable[[], Conn], clock: Clock, email_id: int, status: str) -> None:
    """D65: the run's result was dropped because the email changed meanwhile; recorded
    in its own transaction, after the final write rolled back."""
    with connect() as conn:
        write_audit(
            conn, clock.now(), "system", AuditEvent.RUN_DISCARDED, email_id=email_id,
            output={"step": "extract", "email_status": status},
        )  # fmt: skip
        conn.commit()
