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
from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.clock import Clock
from anemos.reservations.availability import free_rooms, rooms_for_plan
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
)
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import Party, RoomRequest, Stay, party_from_counts, stay_or_error
from anemos.reservations.occupancy import fits
from anemos.reservations.offer import Offer, OfferChange, build_offer
from anemos.reservations.pricing import NightLine, Quote
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


REQUEST_SQL = """
SELECT stay, adults, children, child_ages, rooms, room_type_id, extracted
FROM requests WHERE id = %s
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
) -> StoredQuote | NeedsInfo | Closed | MinStayIntoClosed | NoRate | NeedsEscalation | FullyBooked:
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
    """
    loaded = _load_request(conn, request_id)
    if isinstance(loaded, NeedsInfo):
        return loaded
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
class _LoadedRequest:
    request: RoomRequest
    party: Party
    accessibility_need: bool


def _load_request(conn: Conn, request_id: int) -> _LoadedRequest | NeedsInfo:
    """The stored request as build_offer's inputs, or what must be asked first."""
    row = conn.execute(REQUEST_SQL, (request_id,)).fetchone()
    if row is None:
        raise LookupError(f"no request {request_id}")
    stay_range, adults, children, child_ages, rooms, room_type_id, extracted = row

    if stay_range is None:
        return NeedsInfo("dates of the stay")
    party = party_from_counts(adults, children, child_ages)
    if isinstance(party, NeedsInfo):
        return party
    request = RoomRequest(
        Stay(stay_range.lower, stay_range.upper),
        # Not stated = one room (D5: one stay, at most 2 rooms; 3+ escalates as GROUP).
        rooms=1 if rooms is None else rooms,
        room_type_id=room_type_id,
    )
    return _LoadedRequest(request, party, _has_flag(extracted, ACCESSIBILITY_FLAG))


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
            "lines": Jsonb([_line_json(line) for line in quote.lines]),
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


def _line_json(line: NightLine) -> dict[str, Any]:
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
    loaded = _load_request(conn, request_id)
    if isinstance(loaded, NeedsInfo):
        return loaded
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
    loaded: _LoadedRequest,
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
    conn: Conn, hotel: HotelData, clock: Clock, loaded: _LoadedRequest, base: Offer
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
