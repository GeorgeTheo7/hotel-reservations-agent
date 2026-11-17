"""Stub drafter (week 3): what the agent will do in week 5, without a model.

`draft_reply` takes a stored email whose request is already in `requests` (week 4's
extraction will fill it) and, in one transaction (no commit):

1. takes the sender's lock (D61) and moves the email `received` -> `processing`;
2. runs the read tools: quote_price, and suggest_alternatives on FULLY_BOOKED;
3. escalates (no draft) or writes a draft from fixed templates with the
   code-rendered blocks (D8): route `quote` (one offer, alternatives, or the
   options of a window, D7),
   `ask_info` (NEEDS_INFO) or `decline` (CLOSED, stating the open season; NONE_FOUND)
   (D62);
4. runs guardrails 1 and 7 (D53); a failure escalates GUARDRAIL_FAIL;
5. stores draft, version 1 (author agent, with guardrail results) and offers, and
   moves the email to `awaiting_approval`, or, if a newer email from the same
   address is already stored, the draft is born superseded and the email goes
   `processing` -> `superseded` (D48, states.md).

Replies the stub cannot write yet (MIN_STAY_CLOSED, a room freed between two tool
calls) are escalated with MODEL: the stub stands in for the model, which may escalate
(D11). NO_RATE is a data error and escalates SYSTEM_ERROR.

Rendering and storing a draft are shared with the agent (anemos.drafting).
"""

from dataclasses import dataclass
from datetime import datetime
from string import Template

import psycopg
from psycopg.rows import TupleRow

from anemos.audit import AuditEvent, write_audit
from anemos.clock import Clock
from anemos.drafting import (
    REQUEST_SUMMARY,
    loaded_request,
    open_season_dates,
    phrases,
    render_offers,
    request_summary,
    store_draft,
    template,
)
from anemos.drafting import (
    Drafted as Drafted,  # re-exported: draft_reply returns it
)
from anemos.guardrails import ProposedOffer, all_passed, holds_valid, quote_block_once
from anemos.inbox import lock_sender
from anemos.reservations.errors import (
    Closed,
    FullyBooked,
    MinStayIntoClosed,
    NeedsEscalation,
    NeedsInfo,
    NoneFound,
    NoRate,
    OriginalAvailable,
    ReasonCode,
)
from anemos.reservations.formatting import Lang, room_type_name
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.quote_template import (
    date_with_article,
)
from anemos.reservations.tools import (
    Alternatives,
    Loaded,
    LoadedHoliday,
    LoadedWindow,
    StoredQuote,
    WindowQuotes,
    quote_price,
    suggest_alternatives,
)
from anemos.states import EmailStatus, TransitionRefused

type Conn = psycopg.Connection[TupleRow]


@dataclass(frozen=True)
class Escalated:
    reason: ReasonCode
    detail: str


def draft_reply(conn: Conn, hotel: HotelData, clock: Clock, email_id: int) -> Drafted | Escalated:
    """Claim a `received` email (received -> processing) and draft it: the stub on its
    own, without extraction or routing (tests; week 3)."""
    row = conn.execute("SELECT from_address FROM emails WHERE id = %s", (email_id,)).fetchone()
    if row is None:
        raise LookupError(f"no email {email_id}")
    lock_sender(conn, row[0])  # D61: before reading anything that superseding changes
    email = conn.execute(
        "SELECT status FROM emails WHERE id = %s FOR UPDATE", (email_id,)
    ).fetchone()
    if email is None:
        raise LookupError(f"no email {email_id}")
    if email[0] != EmailStatus.RECEIVED:
        raise TransitionRefused(
            "email", email_id, email[0], EmailStatus.PROCESSING, action="draft_reply"
        )
    _set_email(conn, email_id, EmailStatus.PROCESSING)
    write_audit(conn, clock.now(), "system", AuditEvent.EMAIL_PROCESSING, email_id=email_id)
    return draft_claimed(conn, hotel, clock, email_id)


def draft_claimed(conn: Conn, hotel: HotelData, clock: Clock, email_id: int) -> Drafted | Escalated:
    """Draft an email that is already `processing`, its sender lock held by the caller
    (D61): the run's final transaction after routing, or `draft_reply`."""
    email = conn.execute(
        "SELECT status, language FROM emails WHERE id = %s FOR UPDATE",
        (email_id,),
    ).fetchone()
    if email is None:
        raise LookupError(f"no email {email_id}")
    status, language = email
    if status != EmailStatus.PROCESSING:
        raise TransitionRefused("email", email_id, status, EmailStatus.AWAITING_APPROVAL,
                                action="draft")  # fmt: skip
    now = clock.now()

    request_row = conn.execute(
        "SELECT id FROM requests WHERE email_id = %s", (email_id,)
    ).fetchone()
    if request_row is None:
        raise LookupError(f"email {email_id} has no request; the stub needs one stored")
    request_id = int(request_row[0])
    lang: Lang = "el" if language == "el" else "en"

    outcome = _run_tools(conn, hotel, clock, request_id)
    if isinstance(outcome, Escalated):
        return _escalate(conn, now, email_id, outcome)

    offers: list[ProposedOffer] = []
    quotes: list[StoredQuote] = []
    blocks: dict[int, str] = {}  # quote id -> rendered block, kept for the price check
    if isinstance(outcome, NeedsInfo):
        route, text = "ask_info", _ask_info_text(outcome, lang)
        rendered = text
    elif isinstance(outcome, Closed | NoneFound):
        loaded = loaded_request(conn, request_id)
        route, text = "decline", _decline_text(hotel, outcome, loaded, lang)
        rendered = text.replace(REQUEST_SUMMARY, request_summary(hotel, loaded, lang))
    else:
        loaded = loaded_request(conn, request_id)
        route = "quote"
        if isinstance(outcome, Alternatives | WindowQuotes):
            quotes = list(outcome.options)
        else:
            quotes = [outcome]
        offers = [ProposedOffer(q.quote_id, hold=q.offer.hold_allowed) for q in quotes]
        if len(offers) > 1:  # D9: options are never held
            offers = [ProposedOffer(o.quote_id, hold=False) for o in offers]
        intro = (
            "alternatives_intro" if isinstance(outcome, Alternatives)
            else "window_intro" if isinstance(outcome, WindowQuotes)
            else None
        )  # fmt: skip
        text = _quote_text(lang, intro=intro)
        rendered, blocks = render_offers(hotel, text, loaded, quotes, offers, lang)

    results: dict[str, bool | str] = {
        "g1_quote_block_once": quote_block_once(text, route),
        "g7_holds_valid": holds_valid(
            offers,
            {q.quote_id for q in quotes},
            {q.quote_id: q.offer.hold_allowed for q in quotes},
        ),
    }
    if not all_passed(results):
        detail = "; ".join(str(r) for r in results.values() if r is not True)
        return _escalate(conn, now, email_id, Escalated(ReasonCode.GUARDRAIL_FAIL, detail))

    return store_draft(conn, now, email_id, request_id, route, rendered, results, offers, blocks)


# --- tools ------------------------------------------------------------------------


def _run_tools(
    conn: Conn, hotel: HotelData, clock: Clock, request_id: int
) -> StoredQuote | WindowQuotes | Alternatives | NeedsInfo | Closed | NoneFound | Escalated:
    priced = quote_price(conn, hotel, clock, request_id)
    if isinstance(priced, FullyBooked):
        found = suggest_alternatives(conn, hotel, clock, request_id)
        if isinstance(found, Alternatives | NoneFound):
            return found
        if isinstance(found, OriginalAvailable):
            # A room was freed between the two calls; let the owner look rather than
            # loop on a changing snapshot.
            return Escalated(ReasonCode.MODEL, "availability changed while drafting")
        return _escalated(found)
    if isinstance(priced, StoredQuote | WindowQuotes | NeedsInfo | Closed | NoneFound):
        return priced  # NoneFound: nothing free in a window (quote_window)
    return _escalated(priced)


def _escalated(
    error: NeedsEscalation | Closed | MinStayIntoClosed | NoRate | NeedsInfo,
) -> Escalated:
    if isinstance(error, NeedsEscalation):
        return Escalated(error.reason, error.detail)
    if isinstance(error, NoRate):
        return Escalated(ReasonCode.SYSTEM_ERROR, f"no rate for nights {error.nights}")
    return Escalated(ReasonCode.MODEL, f"{error.code}: reply not available in the stub")


def _escalate(conn: Conn, now: datetime, email_id: int, escalation: Escalated) -> Escalated:
    conn.execute(
        "UPDATE emails SET status = %s, reason_codes = %s WHERE id = %s",
        (EmailStatus.ESCALATED, [escalation.reason.value], email_id),
    )
    write_audit(
        conn, now, "agent", AuditEvent.EMAIL_ESCALATED, email_id=email_id,
        output={"reason": escalation.reason.value, "detail": escalation.detail},
    )  # fmt: skip
    return escalation


# --- text ----------------------------------------------------------------------------


def _quote_text(lang: Lang, *, intro: str | None) -> str:
    """intro: the phrase key that introduces several options, or None for one quote."""
    text = str(phrases(lang)[intro]) if intro is not None else ""
    return Template(template(f"stub_quote_{lang}.txt")).substitute(intro=text)


def _decline_text(hotel: HotelData, why: Closed | NoneFound, loaded: Loaded, lang: Lang) -> str:
    """D62: a decline restates the request ({{REQUEST_SUMMARY}}, guardrail 3) and says
    why. CLOSED names the open season of the year asked for."""
    if isinstance(why, NoneFound) and why.room_type_id is not None:
        # A window: only the plan's type was looked at, and no dates were shifted, so
        # the reply names that type and claims nothing about the rest of the hotel.
        room_type = room_type_name(hotel.room_types[why.room_type_id], lang)
        return Template(template(f"stub_decline_window_full_{lang}.txt")).substitute(
            room_type=room_type
        )
    if isinstance(why, NoneFound):
        return template(f"stub_decline_full_{lang}.txt")
    if isinstance(loaded, LoadedWindow):
        first = loaded.window.first_check_in
    elif isinstance(loaded, LoadedHoliday):
        first = loaded.holiday.first_day
    else:
        first = loaded.request.stay.check_in
    opens, closes = open_season_dates(hotel, first.year)
    return Template(template(f"stub_decline_closed_{lang}.txt")).substitute(
        opens=date_with_article(opens, lang), closes=date_with_article(closes, lang)
    )


def _ask_info_text(needs: NeedsInfo, lang: Lang) -> str:
    words = phrases(lang)
    missing = words["missing"]
    if not isinstance(missing, dict):
        raise TypeError("stub phrases: 'missing' must be an object")
    item = str(missing.get(needs.missing, words["missing_fallback"]))
    return Template(template(f"stub_ask_info_{lang}.txt")).substitute(missing=item)


# --- database --------------------------------------------------------------------------


def _set_email(conn: Conn, email_id: int, status: EmailStatus) -> None:
    conn.execute("UPDATE emails SET status = %s WHERE id = %s", (status, email_id))
