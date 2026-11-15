"""One run per email (D17): extraction, then routing in code (D70), then the draft.

`process_email` (extraction_run) claims the email, calls the model with nothing held,
and in its final transaction stores the extraction and calls `route_and_draft`:

- the router's decision is audited (`email.routed`) for every email;
- SetAside: the email is set aside (spam);
- Escalate: the email is escalated with every reason code;
- ToAgent(quote / ask_info / decline): the stub drafter writes the draft. Its tools can
  still decline (nothing free) or escalate; the agent loop replaces it in week 5.

Thread replies never get here: `process_email` escalates them before the model call.
"""

from collections.abc import Callable

import psycopg
from psycopg.rows import TupleRow

from anemos.clock import Clock
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import Claimed, RunResult, process_email, record_route
from anemos.inbox import escalate, set_aside
from anemos.llm.adapter import LLM
from anemos.reservations.datespec import resolve
from anemos.reservations.loader import load_hotel_data
from anemos.routing import EmailFacts, Escalate, SetAside, ToAgent, route
from anemos.states import EmailStatus, TransitionRefused
from anemos.stub_drafter import draft_claimed

type Conn = psycopg.Connection[TupleRow]


def route_and_draft(conn: Conn, clock: Clock, claimed: Claimed, out: ExtractionOutput) -> None:
    """The `finish` of a run, inside its final transaction (sender lock held, email
    still `processing`). The hotel data is read here, in the same transaction, so the
    calendar and rates are the ones the draft is written against."""
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
        case ToAgent(route="policy"):
            raise AssertionError("the router sends no policy route before week 5")
        case ToAgent():
            draft_claimed(conn, hotel, clock, claimed.email_id)


def process_inbox(connect: Callable[[], Conn], clock: Clock, llm: LLM) -> list[RunResult]:
    """Run every `received` email, oldest first (received_at, then id: D48's order), one
    run each. An email another worker claimed meanwhile is skipped."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM emails WHERE status = %s ORDER BY received_at, id",
            (EmailStatus.RECEIVED,),
        ).fetchall()
        conn.commit()
    results = []
    for (email_id,) in rows:
        try:
            results.append(process_email(connect, clock, llm, email_id, finish=route_and_draft))
        except TransitionRefused:
            continue
    return results
