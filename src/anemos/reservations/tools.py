"""Read tools of the agent (spec Tools; D11, D16): plain functions, typed errors.

Neither tool changes inventory. `quote_price` stores a `quotes` row (no inventory
effect, spec Tools). Neither commits: the caller owns the transaction. Write tools
(hold_room, send_reply) do not exist as functions the model could reach; only the
approval transaction creates bookings and outbox rows (D12, states.md).

Business failures are return values (errors.py); a missing request id or a broken
database row is a bug and raises.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.clock import Clock
from anemos.reservations.availability import free_rooms, rooms_for_plan
from anemos.reservations.datespec import FixedHoliday, Window, date_spec_from_json, resolve
from anemos.reservations.errors import (
    Closed,
    FullyBooked,
    InvalidDates,
    MinStay,
    MinStayIntoClosed,
    NeedsEscalation,
    NeedsInfo,
    NoneFound,
    NoRate,
    OriginalAvailable,
    ReasonCode,
)
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import Party, RoomRequest, Stay, party_from_counts, stay_or_error
from anemos.reservations.occupancy import fits
from anemos.reservations.offer import Offer, OfferChange, build_offer
from anemos.reservations.options import window_options
from anemos.reservations.pricing import NightLine, Quote
from anemos.reservations.seasons import is_open
from anemos.reservations.stay_rules import check_stay, short_notice

type Conn = psycopg.Connection[TupleRow]

ACCESSIBILITY_FLAG = "accessibility_need"  # extraction flag (spec Escalation rules)


# --- check_availability -----------------------------------------------------------


@dataclass(frozen=True)
class Availability:
    """Free rooms for every night of the stay, per room type (snapshot, D38)."""

    stay: Stay
    free_rooms: Mapping[str, tuple[int, ...]]  # every type present; () = none free


def check_availability(
    conn: Conn, hotel: HotelData, clock: Clock, check_in: date, check_out: date
) -> Availability | InvalidDates | Closed | MinStay | NeedsEscalation:
    """Which rooms are free for these dates, after the calendar allows the stay.

    Errors (spec Tools): INVALID_DATES (0 nights, check-out before check-in), CLOSED,
    MIN_STAY(n); NO_CALENDAR escalates (D36). Only reads: an expired hold counts as
    free but is not flipped (D22).
    """
    stay = stay_or_error(check_in, check_out)
    if isinstance(stay, InvalidDates):
        return stay
    problem = check_stay(hotel, stay)
    if problem is not None:
        return problem
    return Availability(stay, free_rooms(conn, stay, clock))


# --- quote_price ------------------------------------------------------------------


@dataclass(frozen=True)
class StoredQuote:
    quote_id: int
    offer: Offer


@dataclass(frozen=True)
class WindowQuotes:
    """Up to 3 options inside the window, each priced and stored (D58); never held (D9).
    `changes` of each: window_option, plus min_stay when the requested length fits
    nowhere only because of the minimum stay (D7), plus any room-plan change (D27)."""

    options: tuple[StoredQuote, ...]


REQUEST_SQL = """
SELECT r.stay, r.adults, r.children, r.child_ages, r.rooms, r.room_type_id, r.extracted,
       e.received_at
FROM requests r JOIN emails e ON e.id = r.email_id WHERE r.id = %s
"""

INSERT_QUOTE_SQL = """
INSERT INTO quotes (
    request_id, room_type_id, stay, rooms, lines, extra_bed_bands,
    room_subtotal_cents, extra_bed_subtotal_cents, accommodation_subtotal_cents,
    city_tax_cents, deposit_cents, free_cancellation_until, changes, created_at
) VALUES (
    %(request_id)s, %(room_type_id)s, daterange(%(check_in)s, %(check_out)s, '[)'),
    %(rooms)s, %(lines)s, %(extra_bed_bands)s,
    %(room_subtotal_cents)s, %(extra_bed_subtotal_cents)s, %(accommodation_subtotal_cents)s,
    %(city_tax_cents)s, %(deposit_cents)s, %(free_cancellation_until)s, %(changes)s,
    %(created_at)s
) RETURNING id
"""


def quote_price(
    conn: Conn, hotel: HotelData, clock: Clock, request_id: int
) -> (
    StoredQuote
    | WindowQuotes
    | NeedsInfo
    | Closed
    | MinStayIntoClosed
    | NoRate
    | NeedsEscalation
    | FullyBooked
    | NoneFound
):
    """Price the stored request through build_offer, check the plan is free, and store
    the quote.

    The party, dates and room type always come from the request row, never from the
    caller, so the model cannot price a party the guest did not describe. Missing
    dates, adults or children's ages -> NEEDS_INFO, and nothing is stored. Over
    capacity is not an error here: build_offer substitutes the D27 plan and records
    it in `changes` (the spec's "valid alternative attached").

    D59 (D11: rules inside tools): the plan build_offer chose (a D27 substitute, not
    the type asked) must have enough free rooms, else FULLY_BOOKED and nothing is
    stored; the model's next step is suggest_alternatives. Approval still re-checks
    (D47), because this is a snapshot.

    A window request (Easter, part of a month; D7, D28) is priced by `quote_window`:
    up to 3 options, never held, or why there are none. A fixed-date holiday (D68) is
    never priced: `holiday_outcome` says NO_CALENDAR, CLOSED or NEEDS_INFO.
    """
    loaded = load_request(conn, request_id)
    if isinstance(loaded, NeedsInfo):
        return loaded
    if isinstance(loaded, LoadedWindow):
        return quote_window(conn, hotel, clock, request_id, loaded)
    if isinstance(loaded, LoadedHoliday):
        return holiday_outcome(hotel, loaded.holiday)
    result = build_offer(
        hotel, loaded.request, loaded.party, accessibility_need=loaded.accessibility_need
    )
    if not isinstance(result, Offer):
        return result
    plan = result.quote
    rooms = rooms_for_plan(conn, clock, plan.room_type_id, plan.stay, plan.rooms)
    if isinstance(rooms, FullyBooked):
        return rooms
    quote_id = _store_quote(conn, clock, request_id, result)
    return StoredQuote(quote_id, result)


@dataclass(frozen=True)
class LoadedRequest:
    request: RoomRequest
    party: Party
    accessibility_need: bool


@dataclass(frozen=True)
class LoadedWindow:
    """A stored request for a check-in window (D7, D28) instead of one stay."""

    window: Window
    rooms: int
    room_type_id: str | None
    party: Party
    accessibility_need: bool


@dataclass(frozen=True)
class LoadedHoliday:
    """A stored request for a fixed-date holiday (D68): no stay and no window; the
    calendar decides (NO_CALENDAR, CLOSED, else NEEDS_INFO)."""

    holiday: FixedHoliday
    rooms: int
    room_type_id: str | None
    party: Party


type Loaded = LoadedRequest | LoadedWindow | LoadedHoliday


def load_request(conn: Conn, request_id: int) -> Loaded | NeedsInfo:
    """The stored request as build_offer's inputs, or what must be asked first.

    A window (Easter, part of a month) is not stored as a stay: `requests.stay` stays
    NULL and the DateSpec is in `extracted`. It is resolved again here, at the email's
    received_at, by the same code as at storing (D6), so the result is the same window.
    """
    row = conn.execute(REQUEST_SQL, (request_id,)).fetchone()
    if row is None:
        raise LookupError(f"no request {request_id}")
    stay_range, adults, children, child_ages, rooms, room_type_id, extracted, received_at = row

    dates = None
    if stay_range is None:
        dates = _stored_dates(extracted, received_at)
        if dates is None:
            return NeedsInfo("dates of the stay")
    party = party_from_counts(adults, children, child_ages)
    if isinstance(party, NeedsInfo):
        return party
    # Not stated = one room (D5: one stay, at most 2 rooms; 3+ escalates as GROUP).
    rooms = 1 if rooms is None else rooms
    need = _has_flag(extracted, ACCESSIBILITY_FLAG)
    if isinstance(dates, Window):
        return LoadedWindow(dates, rooms, room_type_id, party, need)
    if isinstance(dates, FixedHoliday):
        return LoadedHoliday(dates, rooms, room_type_id, party)
    request = RoomRequest(Stay(stay_range.lower, stay_range.upper), rooms, room_type_id)
    return LoadedRequest(request, party, need)


def _stored_dates(
    extracted: Mapping[str, Any], received_at: datetime
) -> Window | FixedHoliday | None:
    """The window or fixed-date holiday of a stored DateSpec, or None if it has neither
    (no dates, or dates that need asking). A DateSpec that does not parse is a bug in
    what we stored: it raises."""
    raw = extracted.get("date_spec")
    if raw is None:
        return None
    resolved = resolve(date_spec_from_json(raw), received_at)
    return resolved if isinstance(resolved, Window | FixedHoliday) else None


def _has_flag(extracted: Mapping[str, Any], flag: str) -> bool:
    flags: Sequence[str] = extracted.get("flags", [])
    return flag in flags


def _store_quote(conn: Conn, clock: Clock, request_id: int, offer: Offer) -> int:
    quote: Quote = offer.quote
    row = conn.execute(
        INSERT_QUOTE_SQL,
        {
            "request_id": request_id,
            "room_type_id": quote.room_type_id,
            "check_in": quote.stay.check_in,
            "check_out": quote.stay.check_out,
            "rooms": quote.rooms,
            "lines": Jsonb([line_json(line) for line in quote.lines]),
            "extra_bed_bands": [band.value for band in quote.extra_bed_bands],
            "room_subtotal_cents": quote.room_subtotal_cents,
            "extra_bed_subtotal_cents": quote.extra_bed_subtotal_cents,
            "accommodation_subtotal_cents": quote.accommodation_subtotal_cents,
            "city_tax_cents": quote.city_tax_cents,
            "deposit_cents": quote.deposit_cents,
            "free_cancellation_until": quote.free_cancellation_until,
            "changes": sorted(change.value for change in offer.changes),
            "created_at": clock.now(),
        },
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING returned no row")
    return int(row[0])


def line_json(line: NightLine) -> dict[str, Any]:
    return {
        "night": line.night.isoformat(),
        "season": line.season.value,
        "room_rate_cents": line.room_rate_cents,
        "room_cents": line.room_cents,
        "extra_bed_cents": line.extra_bed_cents,
        "city_tax_cents": line.city_tax_cents,
    }


# --- suggest_alternatives -----------------------------------------------------------

MAX_ALTERNATIVES = 2  # spec Unmet requests: <= 2 alternatives
DATE_SEARCH_DAYS = 7  # spec Unmet requests: nearest same-length ranges within +-7 d


@dataclass(frozen=True)
class Alternatives:
    """Free alternatives to the offer build_offer would make, each priced and stored.

    kind OTHER_TYPE: other room types for the same stay, cheapest first.
    kind OTHER_DATES: the same plan on same-length stays within +-7 d, nearest first.
    """

    kind: OfferChange
    options: tuple[StoredQuote, ...]


def suggest_alternatives(
    conn: Conn, hotel: HotelData, clock: Clock, request_id: int
) -> (
    Alternatives
    | OriginalAvailable
    | NoneFound
    | NeedsInfo
    | Closed
    | MinStayIntoClosed
    | NoRate
    | NeedsEscalation
):
    """What to offer when the plan is FULLY_BOOKED (spec Unmet requests).

    1. Other room types for the same stay: types the party fits in the same number of
       rooms, free for the whole stay (rooms_for_plan, D59), at most 2, cheapest first
       (accommodation subtotal, then type id). The Accessible room only if no other
       type is free (D33). Skipped for an accessibility need: no other type meets it.
    2. Only if no other type is free: the same plan on same-length stays shifted by
       1..7 days, nearest first, earlier first on a tie, at most 2. A shifted stay must
       pass the calendar unchanged (check_stay) and not be SHORT_NOTICE (D31).
    3. Nothing -> NONE_FOUND (decline).

    Each option is stored as a quote (D58) whose `changes` say how it differs from the
    request; with options offered, no hold (D9, guardrail 7). The plan's own type is
    never an alternative: if that plan is free, the result is ORIGINAL_AVAILABLE (next
    step quote_price) and nothing is stored (D60). Errors of the base offer
    (NEEDS_INFO, CLOSED, escalations) are returned unchanged and nothing is stored.
    """
    loaded = load_request(conn, request_id)
    if isinstance(loaded, NeedsInfo):
        return loaded
    if isinstance(loaded, LoadedWindow):
        # Window options are the choice already (D7); a window has no "other dates".
        raise ValueError(f"request {request_id} is a window: options come from quote_price")
    if isinstance(loaded, LoadedHoliday):
        raise ValueError(f"request {request_id} is a fixed-date holiday: nothing is priced")
    base = build_offer(
        hotel, loaded.request, loaded.party, accessibility_need=loaded.accessibility_need
    )
    if not isinstance(base, Offer):
        return base
    plan = base.quote
    if not isinstance(
        rooms_for_plan(conn, clock, plan.room_type_id, plan.stay, plan.rooms), FullyBooked
    ):
        return OriginalAvailable(plan.room_type_id, plan.rooms)

    options: list[Offer] = []
    kind = OfferChange.OTHER_TYPE
    if not loaded.accessibility_need:
        options = _other_types(conn, hotel, clock, loaded, base, flagged=False)
        if not options:
            options = _other_types(conn, hotel, clock, loaded, base, flagged=True)
    if not options:
        kind = OfferChange.OTHER_DATES
        options = _other_dates(conn, hotel, clock, loaded, base)
    if not options:
        stay = base.quote.stay
        return NoneFound(stay.check_in, stay.check_out, DATE_SEARCH_DAYS)

    stored = tuple(
        StoredQuote(_store_quote(conn, clock, request_id, offer), offer) for offer in options
    )
    return Alternatives(kind, stored)


def _other_types(
    conn: Conn,
    hotel: HotelData,
    clock: Clock,
    loaded: LoadedRequest,
    base: Offer,
    *,
    flagged: bool,
) -> list[Offer]:
    rooms = base.quote.rooms
    found: list[Offer] = []
    for room_type in hotel.room_types.values():
        if (
            room_type.id == base.quote.room_type_id
            or room_type.offer_only_if_needed != flagged
            or not fits(room_type, loaded.party, rooms)
        ):
            continue
        priced = build_offer(
            hotel, RoomRequest(loaded.request.stay, rooms, room_type.id), loaded.party
        )
        if not isinstance(priced, Offer):
            continue
        free = rooms_for_plan(conn, clock, room_type.id, priced.quote.stay, rooms)
        if isinstance(free, FullyBooked):
            continue
        # Calendar changes (open part, min stay) come from build_offer; the type and
        # room count are compared with what the guest asked, not with this request.
        changes = set(priced.changes)
        if loaded.request.room_type_id is not None:
            changes.add(OfferChange.OTHER_TYPE)  # D55: no type named = any type was asked
        if rooms > loaded.request.rooms:
            changes.add(OfferChange.MORE_ROOMS)
        found.append(Offer(priced.quote, frozenset(changes)))
    found.sort(key=lambda o: (o.quote.accommodation_subtotal_cents, o.quote.room_type_id))
    return found[:MAX_ALTERNATIVES]


def _other_dates(
    conn: Conn, hotel: HotelData, clock: Clock, loaded: LoadedRequest, base: Offer
) -> list[Offer]:
    plan = base.quote
    found: list[Offer] = []
    # Nearest first; on a tie the earlier stay first: -1, +1, -2, +2, ...
    for shift in sorted(
        (s for s in range(-DATE_SEARCH_DAYS, DATE_SEARCH_DAYS + 1) if s != 0),
        key=lambda s: (abs(s), s),
    ):
        moved = Stay(
            plan.stay.check_in + timedelta(days=shift), plan.stay.check_out + timedelta(days=shift)
        )
        if short_notice(moved.check_in, clock.now()) is not None:
            continue  # also skips stays in the past
        if check_stay(hotel, moved) is not None:
            continue  # same length only: no trimming or lengthening of an alternative
        priced = build_offer(
            hotel,
            RoomRequest(moved, plan.rooms, plan.room_type_id),
            loaded.party,
            accessibility_need=loaded.accessibility_need,
        )
        if not isinstance(priced, Offer):
            continue
        free = rooms_for_plan(conn, clock, plan.room_type_id, moved, plan.rooms)
        if isinstance(free, FullyBooked):
            continue
        found.append(Offer(priced.quote, base.changes | {OfferChange.OTHER_DATES}))
        if len(found) == MAX_ALTERNATIVES:
            break
    return found


# --- window requests (D7, D28) --------------------------------------------------------


def quote_window(
    conn: Conn, hotel: HotelData, clock: Clock, request_id: int, loaded: LoadedWindow
) -> WindowQuotes | Closed | NoneFound | NoRate | NeedsEscalation:
    """Options for a window request (D7), or why there are none.

    In order: a candidate night in a year with no calendar -> NO_CALENDAR (D36); every
    candidate night closed -> CLOSED (decline, D62). Then `window_options` picks the
    options; a stay counts only if it passes the calendar, is not SHORT_NOTICE (D31,
    as in `_other_dates`), build_offer prices it, and its plan is free (D59).
    No option: a party escalation (GROUP, ACCESSIBILITY, UNACCOMPANIED_CHILDREN, the
    same for every stay) or NoRate is returned; else, if some stay was taken, NONE_FOUND
    (decline; days_searched 0: a window is not shifted); else, if every stay was too
    soon, SHORT_NOTICE; else no stay passes the calendar: CLOSED with the closed nights.
    """
    window = loaded.window
    nights = window.candidate_nights()
    missing = sorted({n.year for n in nights if not hotel.has_calendar_for(n.year)})
    if missing:
        return NeedsEscalation(
            ReasonCode.NO_CALENDAR, f"no season calendar for {', '.join(map(str, missing))}"
        )
    closed = tuple(n for n in nights if not is_open(hotel, n))
    if len(closed) == len(nights):
        return Closed(closed)

    offers: dict[Stay, Offer] = {}
    problems: list[NoRate | NeedsEscalation] = []
    taken: list[str] = []  # the plan's room type, per stay that was taken
    too_soon: list[Stay] = []

    def is_available(stay: Stay) -> bool:
        if short_notice(stay.check_in, clock.now()) is not None:
            too_soon.append(stay)
            return False
        offer = build_offer(
            hotel, RoomRequest(stay, loaded.rooms, loaded.room_type_id), loaded.party,
            accessibility_need=loaded.accessibility_need,
        )  # fmt: skip
        if isinstance(offer, NoRate | NeedsEscalation):
            problems.append(offer)
            return False
        if not isinstance(offer, Offer):  # the calendar was checked first: a bug
            raise AssertionError(f"{stay} passed check_stay but build_offer says {offer}")
        plan = offer.quote
        free = rooms_for_plan(conn, clock, plan.room_type_id, plan.stay, plan.rooms)
        if isinstance(free, FullyBooked):
            taken.append(plan.room_type_id)
            return False
        offers[stay] = offer
        return True

    found = window_options(hotel, window, is_available)
    if found.options:
        extra = {OfferChange.WINDOW_OPTION}
        if found.length_changed:
            extra.add(OfferChange.MIN_STAY)
        stored = []
        for stay in found.options:
            offer = Offer(offers[stay].quote, offers[stay].changes | extra)
            stored.append(StoredQuote(_store_quote(conn, clock, request_id, offer), offer))
        return WindowQuotes(tuple(stored))
    if problems:
        return problems[0]
    if taken:
        last_check_out = window.last_check_in + timedelta(days=window.nights)
        return NoneFound(window.first_check_in, last_check_out, 0, room_type_id=taken[0])
    if too_soon:
        return NeedsEscalation(
            ReasonCode.SHORT_NOTICE, f"every check-in from {window.first_check_in} is too soon"
        )
    return Closed(closed)


# --- fixed-date holidays (D68) -----------------------------------------------------------


def holiday_outcome(
    hotel: HotelData, holiday: FixedHoliday
) -> Closed | NeedsInfo | NeedsEscalation:
    """D68: a day in a year with no calendar -> NO_CALENDAR (D36, first); every day
    outside the open season -> CLOSED (decline with the open season, D62); else the
    holiday is in the season and the guest is asked for dates (NEEDS_INFO, D28)."""
    days = holiday.days()
    missing = sorted({d.year for d in days if not hotel.has_calendar_for(d.year)})
    if missing:
        return NeedsEscalation(
            ReasonCode.NO_CALENDAR, f"no season calendar for {', '.join(map(str, missing))}"
        )
    if not any(is_open(hotel, d) for d in days):
        return Closed(days)
    return NeedsInfo("dates of the stay")
