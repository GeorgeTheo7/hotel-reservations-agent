"""What every drafter shares (the stub drafter and the agent): rendering the code-made
blocks (D8) and storing a draft (D48, D63).

- `render_offers`: {{REQUEST_SUMMARY}} and {{QUOTE_BLOCK}} replaced by code-rendered
  text; each quote's block is returned too, stored with its offer for the owner-edit
  price check (D63). {{HOLD_UNTIL}} inside a held block stays for approval to fill.
- `open_season`: the text for {{OPEN_SEASON}} (D62): the first and last open night of
  the year asked for.
- `store_draft`: the draft, version 1 (author agent, with its check results) and its
  offers; the email goes to `awaiting_approval`, or the draft is born superseded if a
  newer email from the same address is already stored (D48). Called inside the run's
  final transaction, under the sender lock (D61).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cache
from importlib.resources import files
from string import Template

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.audit import AuditEvent, write_audit
from anemos.guardrails import QUOTE_BLOCK, ProposedOffer
from anemos.reservations.errors import NeedsInfo
from anemos.reservations.formatting import Lang
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import RoomRequest
from anemos.reservations.occupancy import fits
from anemos.reservations.offer import OfferChange
from anemos.reservations.quote_template import (
    HolidayRequest,
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
    render_summary,
)
from anemos.reservations.quote_template import OfferChange as NoteChange
from anemos.reservations.tools import (
    KnownRequest,
    Loaded,
    LoadedHoliday,
    LoadedWindow,
    StoredQuote,
    load_request,
)
from anemos.states import DraftStatus, EmailStatus

type Conn = psycopg.Connection[TupleRow]

REQUEST_SUMMARY = "{{REQUEST_SUMMARY}}"
OPEN_SEASON = "{{OPEN_SEASON}}"


@dataclass(frozen=True)
class Drafted:
    draft_id: int
    status: DraftStatus  # pending_approval, or superseded (born superseded, D48)
    route: str


# --- rendering -----------------------------------------------------------------------


def render_offers(
    hotel: HotelData,
    text: str,
    loaded: Loaded,
    quotes: Sequence[StoredQuote],
    offers: Sequence[ProposedOffer],
    lang: Lang,
) -> tuple[str, dict[int, str]]:
    """Replace {{REQUEST_SUMMARY}} and {{QUOTE_BLOCK}} with code-rendered text (D8).
    Returns the text and each quote's block."""
    hold = {o.quote_id: o.hold for o in offers}
    blocks = {
        q.quote_id: render_quote_block(
            hotel, q.offer.quote, lang, hold=hold[q.quote_id], changes=notes(hotel, loaded, q)
        )
        for q in quotes
    }
    text = text.replace(REQUEST_SUMMARY, request_summary(hotel, loaded, lang))
    return text.replace(QUOTE_BLOCK, "\n\n".join(blocks.values())), blocks


def request_summary(hotel: HotelData, loaded: Loaded, lang: Lang) -> str:
    """{{REQUEST_SUMMARY}}: the stay, a window's check-in range, or a fixed-date
    holiday's days (guardrail 3)."""
    asked: RoomRequest | WindowRequest | HolidayRequest
    if isinstance(loaded, LoadedWindow):
        asked = WindowRequest(loaded.window, loaded.rooms, loaded.room_type_id)
    elif isinstance(loaded, LoadedHoliday):
        asked = HolidayRequest(loaded.holiday, loaded.rooms, loaded.room_type_id)
    else:
        asked = loaded.request
    return render_request_summary(hotel, asked, loaded.party, lang)


def known_summary(hotel: HotelData, known: KnownRequest, lang: Lang) -> str:
    """{{REQUEST_SUMMARY}} for an incomplete request: the part it knows (D80)."""
    return render_summary(hotel, known.dates, known.rooms, known.room_type_id, known.party,
                          lang)  # fmt: skip


def notes(hotel: HotelData, loaded: Loaded, stored: StoredQuote) -> list[NoteChange]:
    """One note per entry in the quote's changes (the block sorts them into a fixed
    order). A type change is a capacity note (D27) when the type asked cannot hold the
    party, else an availability note (an alternative, D60)."""
    quote, changes = stored.offer.quote, stored.offer.changes
    found: list[NoteChange] = []
    if OfferChange.OPEN_PART in changes:
        found.append(OpenPartChange())
    if OfferChange.MIN_STAY in changes:
        found.append(MinStayChange(quote.stay.check_in, quote.stay.night_count))
    asked, asked_rooms = _asked(loaded)
    if OfferChange.OTHER_TYPE in changes and asked is not None:
        asked_type = hotel.room_types[asked]
        if fits(asked_type, loaded.party, asked_rooms):
            found.append(RoomTypeUnavailableChange(asked))
        else:
            found.append(RoomTypeChange(asked, asked_type.max_occupancy))
    if OfferChange.MORE_ROOMS in changes:
        found.append(MoreRoomsChange(quote.rooms))
    if OfferChange.OTHER_DATES in changes:
        found.append(OtherDatesChange())
    if OfferChange.WINDOW_OPTION in changes:
        found.append(WindowOptionChange())
    return found


def _asked(loaded: Loaded) -> tuple[str | None, int]:
    """The room type (None = any) and the number of rooms the guest asked for."""
    if isinstance(loaded, LoadedWindow | LoadedHoliday):
        return loaded.room_type_id, loaded.rooms
    return loaded.request.room_type_id, loaded.request.rooms


def open_season_dates(hotel: HotelData, year: int) -> tuple[date, date]:
    """First and last open night of `year`. CLOSED is only returned for a year with a
    calendar (D36), so there is at least one period."""
    periods = [p for p in hotel.season_periods if p.first_night.year == year]
    if not periods:
        raise LookupError(f"no season periods in {year}")
    return min(p.first_night for p in periods), max(p.end for p in periods) - timedelta(days=1)


def open_season(hotel: HotelData, year: int, lang: Lang) -> str:
    """The text for {{OPEN_SEASON}}: "from <first open night> to <last open night>"."""
    opens, closes = open_season_dates(hotel, year)
    return Template(str(phrases(lang)["open_season"])).substitute(
        opens=date_with_article(opens, lang), closes=date_with_article(closes, lang)
    )


def loaded_request(conn: Conn, request_id: int) -> Loaded:
    loaded = load_request(conn, request_id)
    if isinstance(loaded, NeedsInfo):  # the tools loaded it successfully before
        raise RuntimeError(f"request {request_id} changed while drafting: {loaded}")
    return loaded


@cache
def template(name: str) -> str:
    return files("anemos").joinpath("templates", name).read_text(encoding="utf-8")


@cache
def phrases(lang: Lang) -> dict[str, object]:
    found: dict[str, object] = json.loads(template(f"stub_phrases_{lang}.json"))
    return found


# --- storing ---------------------------------------------------------------------------


def store_draft(
    conn: Conn,
    now: datetime,
    email_id: int,
    request_id: int | None,
    route: str,
    body: str,
    results: Mapping[str, bool | str],
    offers: Sequence[ProposedOffer],
    blocks: Mapping[int, str],
    *,
    audit: Mapping[str, object] | None = None,
) -> Drafted:
    """The draft, version 1 and its offers; the email's next status (D48). `audit` adds
    fields to the draft.created entry (the agent's cited policy ids)."""
    email = conn.execute(
        "SELECT from_address, received_at FROM emails WHERE id = %s", (email_id,)
    ).fetchone()
    if email is None:
        raise LookupError(f"no email {email_id}")
    newer = newer_email_exists(conn, email[0], email[1], email_id)
    status = DraftStatus.SUPERSEDED if newer else DraftStatus.PENDING_APPROVAL
    row = conn.execute(
        "INSERT INTO drafts (email_id, request_id, route, status, created_at, decided_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (email_id, request_id, route, status, now,
         now if status is DraftStatus.SUPERSEDED else None),
    ).fetchone()  # fmt: skip
    if row is None:
        raise RuntimeError("INSERT ... RETURNING returned no row")
    draft_id = int(row[0])
    conn.execute(
        "INSERT INTO draft_versions (draft_id, version, author, body, guardrail_results,"
        " created_at) VALUES (%s, 1, 'agent', %s, %s, %s)",
        (draft_id, body, Jsonb(dict(results)), now),
    )
    for offer in offers:
        conn.execute(
            "INSERT INTO draft_offers (draft_id, request_id, quote_id, quote_hold_allowed,"
            " hold, rendered_block) SELECT %s, %s, id, hold_allowed, %s, %s"
            " FROM quotes WHERE id = %s",
            (draft_id, request_id, offer.hold, blocks[offer.quote_id], offer.quote_id),
        )
    next_status = EmailStatus.SUPERSEDED if newer else EmailStatus.AWAITING_APPROVAL
    conn.execute("UPDATE emails SET status = %s WHERE id = %s", (next_status, email_id))
    write_audit(
        conn, now, "agent", AuditEvent.DRAFT_CREATED, email_id=email_id, draft_id=draft_id,
        output={"status": status.value, "route": route, "guardrails": dict(results),
                **(audit or {})},
    )  # fmt: skip
    return Drafted(draft_id, status, route)


def newer_email_exists(conn: Conn, address: str, received_at: datetime, email_id: int) -> bool:
    """D48 order: a later (received_at, id) from the same address. Under the sender's
    lock (D61), so a newer email cannot be stored between this check and our commit."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM emails WHERE from_address = %s"
        " AND (received_at, id) > (%s, %s))",
        (address, received_at, email_id),
    ).fetchone()
    return bool(row and row[0])
