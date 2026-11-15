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
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cache
from importlib.resources import files
from string import Template

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.audit import AuditEvent, write_audit
from anemos.clock import Clock
from anemos.guardrails import QUOTE_BLOCK, ProposedOffer, all_passed, holds_valid, quote_block_once
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
from anemos.reservations.occupancy import fits
from anemos.reservations.offer import OfferChange
from anemos.reservations.quote_template import (
    MinStayChange,
    MoreRoomsChange,
    OpenPartChange,
    OtherDatesChange,
    RoomTypeChange,
    RoomTypeUnavailableChange,
    WindowOptionChange,
    WindowRequest,
    date_with_article,
    render_quote_block,
    render_request_summary,
)
from anemos.reservations.quote_template import OfferChange as NoteChange
from anemos.reservations.tools import (
    Alternatives,
    LoadedRequest,
    LoadedWindow,
    StoredQuote,
    WindowQuotes,
    load_request,
    quote_price,
    suggest_alternatives,
)
from anemos.states import DraftStatus, EmailStatus, TransitionRefused

type Conn = psycopg.Connection[TupleRow]

REQUEST_SUMMARY = "{{REQUEST_SUMMARY}}"


@dataclass(frozen=True)
class Drafted:
    draft_id: int
    status: DraftStatus  # pending_approval, or superseded (born superseded, D48)
    route: str


@dataclass(frozen=True)
class Escalated:
    reason: ReasonCode
    detail: str


def draft_reply(conn: Conn, hotel: HotelData, clock: Clock, email_id: int) -> Drafted | Escalated:
    row = conn.execute("SELECT from_address FROM emails WHERE id = %s", (email_id,)).fetchone()
    if row is None:
        raise LookupError(f"no email {email_id}")
    lock_sender(conn, row[0])  # D61: before reading anything that superseding changes
    email = conn.execute(
        "SELECT status, language, received_at FROM emails WHERE id = %s FOR UPDATE",
        (email_id,),
    ).fetchone()
    if email is None:
        raise LookupError(f"no email {email_id}")
    status, language, received_at = email
    if status != EmailStatus.RECEIVED:
        raise TransitionRefused(
            "email", email_id, status, EmailStatus.PROCESSING, action="draft_reply"
        )
    now = clock.now()
    _set_email(conn, email_id, EmailStatus.PROCESSING)
    write_audit(conn, now, "system", AuditEvent.EMAIL_PROCESSING, email_id=email_id)

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
        loaded = _loaded(conn, request_id)
        route, text = "decline", _decline_text(hotel, outcome, loaded, lang)
        rendered = text.replace(REQUEST_SUMMARY, _summary(hotel, loaded, lang))
    else:
        loaded = _loaded(conn, request_id)
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
        rendered, blocks = _render(hotel, text, loaded, quotes, offers, lang)

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

    newer = _newer_email_exists(conn, row[0], received_at, email_id)
    draft_status = DraftStatus.SUPERSEDED if newer else DraftStatus.PENDING_APPROVAL
    draft_id = _insert_draft(conn, now, email_id, request_id, route, draft_status)
    conn.execute(
        "INSERT INTO draft_versions (draft_id, version, author, body, guardrail_results,"
        " created_at) VALUES (%s, 1, 'agent', %s, %s, %s)",
        (draft_id, rendered, Jsonb(results), now),
    )
    for offer in offers:
        conn.execute(
            "INSERT INTO draft_offers (draft_id, request_id, quote_id, quote_hold_allowed,"
            " hold, rendered_block) SELECT %s, %s, id, hold_allowed, %s, %s"
            " FROM quotes WHERE id = %s",
            (draft_id, request_id, offer.hold, blocks[offer.quote_id], offer.quote_id),
        )
    _set_email(conn, email_id, EmailStatus.SUPERSEDED if newer else EmailStatus.AWAITING_APPROVAL)
    write_audit(
        conn, now, "agent", AuditEvent.DRAFT_CREATED, email_id=email_id, draft_id=draft_id,
        output={"status": draft_status.value, "route": route, "guardrails": results},
    )  # fmt: skip
    return Drafted(draft_id, draft_status, route)


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
    text = str(_phrases(lang)[intro]) if intro is not None else ""
    return Template(_template(f"stub_quote_{lang}.txt")).substitute(intro=text)


def _decline_text(
    hotel: HotelData, why: Closed | NoneFound, loaded: LoadedRequest | LoadedWindow, lang: Lang
) -> str:
    """D62: a decline restates the request ({{REQUEST_SUMMARY}}, guardrail 3) and says
    why. CLOSED names the open season of the year asked for."""
    if isinstance(why, NoneFound) and why.room_type_id is not None:
        # A window: only the plan's type was looked at, and no dates were shifted, so
        # the reply names that type and claims nothing about the rest of the hotel.
        room_type = room_type_name(hotel.room_types[why.room_type_id], lang)
        return Template(_template(f"stub_decline_window_full_{lang}.txt")).substitute(
            room_type=room_type
        )
    if isinstance(why, NoneFound):
        return _template(f"stub_decline_full_{lang}.txt")
    first = (
        loaded.window.first_check_in
        if isinstance(loaded, LoadedWindow)
        else (loaded.request.stay.check_in)
    )
    opens, closes = _open_season(hotel, first.year)
    return Template(_template(f"stub_decline_closed_{lang}.txt")).substitute(
        opens=date_with_article(opens, lang), closes=date_with_article(closes, lang)
    )


def _open_season(hotel: HotelData, year: int) -> tuple[date, date]:
    """First and last open night of `year`. CLOSED is only returned for a year with a
    calendar (D36), so there is at least one period."""
    periods = [p for p in hotel.season_periods if p.first_night.year == year]
    if not periods:
        raise LookupError(f"no season periods in {year}")
    return min(p.first_night for p in periods), max(p.end for p in periods) - timedelta(days=1)


def _ask_info_text(needs: NeedsInfo, lang: Lang) -> str:
    phrases = _phrases(lang)
    missing = phrases["missing"]
    if not isinstance(missing, dict):
        raise TypeError("stub phrases: 'missing' must be an object")
    item = str(missing.get(needs.missing, phrases["missing_fallback"]))
    return Template(_template(f"stub_ask_info_{lang}.txt")).substitute(missing=item)


def _render(
    hotel: HotelData,
    text: str,
    loaded: LoadedRequest | LoadedWindow,
    quotes: list[StoredQuote],
    offers: list[ProposedOffer],
    lang: Lang,
) -> tuple[str, dict[int, str]]:
    """Replace the placeholders with code-rendered blocks (D8). Returns the text and
    each quote's block, stored with its offer for the owner-edit price check (D63).
    {{HOLD_UNTIL}} inside a held block stays for the approval transaction to fill."""
    hold = {o.quote_id: o.hold for o in offers}
    blocks = {
        q.quote_id: render_quote_block(
            hotel, q.offer.quote, lang, hold=hold[q.quote_id], changes=_notes(hotel, loaded, q)
        )
        for q in quotes
    }
    text = text.replace(REQUEST_SUMMARY, _summary(hotel, loaded, lang))
    return text.replace(QUOTE_BLOCK, "\n\n".join(blocks.values())), blocks


def _summary(hotel: HotelData, loaded: LoadedRequest | LoadedWindow, lang: Lang) -> str:
    """{{REQUEST_SUMMARY}}: the stay, or for a window its check-in range (guardrail 3)."""
    asked = (
        WindowRequest(loaded.window, loaded.rooms, loaded.room_type_id)
        if isinstance(loaded, LoadedWindow)
        else loaded.request
    )
    return render_request_summary(hotel, asked, loaded.party, lang)


def _asked(loaded: LoadedRequest | LoadedWindow) -> tuple[str | None, int]:
    """The room type (None = any) and the number of rooms the guest asked for."""
    if isinstance(loaded, LoadedWindow):
        return loaded.room_type_id, loaded.rooms
    return loaded.request.room_type_id, loaded.request.rooms


def _notes(
    hotel: HotelData, loaded: LoadedRequest | LoadedWindow, stored: StoredQuote
) -> list[NoteChange]:
    """One note per entry in the quote's changes (the block sorts them into a fixed
    order). A type change is a capacity note (D27) when the type asked cannot hold the
    party, else an availability note (an alternative, D60)."""
    quote, changes = stored.offer.quote, stored.offer.changes
    notes: list[NoteChange] = []
    if OfferChange.OPEN_PART in changes:
        notes.append(OpenPartChange())
    if OfferChange.MIN_STAY in changes:
        notes.append(MinStayChange(quote.stay.check_in, quote.stay.night_count))
    asked, asked_rooms = _asked(loaded)
    if OfferChange.OTHER_TYPE in changes and asked is not None:
        asked_type = hotel.room_types[asked]
        if fits(asked_type, loaded.party, asked_rooms):
            notes.append(RoomTypeUnavailableChange(asked))
        else:
            notes.append(RoomTypeChange(asked, asked_type.max_occupancy))
    if OfferChange.MORE_ROOMS in changes:
        notes.append(MoreRoomsChange(quote.rooms))
    if OfferChange.OTHER_DATES in changes:
        notes.append(OtherDatesChange())
    if OfferChange.WINDOW_OPTION in changes:
        notes.append(WindowOptionChange())
    return notes


def _loaded(conn: Conn, request_id: int) -> LoadedRequest | LoadedWindow:
    loaded = load_request(conn, request_id)
    if isinstance(loaded, NeedsInfo):  # the tools just loaded it successfully
        raise RuntimeError(f"request {request_id} changed while drafting: {loaded}")
    return loaded


@cache
def _template(name: str) -> str:
    return files("anemos").joinpath("templates", name).read_text(encoding="utf-8")


@cache
def _phrases(lang: Lang) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(_template(f"stub_phrases_{lang}.json"))
    return loaded


# --- database --------------------------------------------------------------------------


def _set_email(conn: Conn, email_id: int, status: EmailStatus) -> None:
    conn.execute("UPDATE emails SET status = %s WHERE id = %s", (status, email_id))


def _newer_email_exists(conn: Conn, address: str, received_at: datetime, email_id: int) -> bool:
    """D48 order: a later (received_at, id) from the same address. Under the sender's
    lock (D61), so a newer email cannot be stored between this check and our commit."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM emails WHERE from_address = %s"
        " AND (received_at, id) > (%s, %s))",
        (address, received_at, email_id),
    ).fetchone()
    return bool(row and row[0])


def _insert_draft(
    conn: Conn, now: datetime, email_id: int, request_id: int, route: str, status: DraftStatus
) -> int:
    row = conn.execute(
        "INSERT INTO drafts (email_id, request_id, route, status, created_at, decided_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            email_id,
            request_id,
            route,
            status,
            now,
            now if status is DraftStatus.SUPERSEDED else None,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING returned no row")
    return int(row[0])
