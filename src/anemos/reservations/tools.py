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
from datetime import date
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
    NoRate,
)
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import RoomRequest, Stay, party_from_counts, stay_or_error
from anemos.reservations.offer import Offer, build_offer
from anemos.reservations.pricing import NightLine, Quote
from anemos.reservations.stay_rules import check_stay

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

    result = build_offer(
        hotel, request, party, accessibility_need=_has_flag(extracted, ACCESSIBILITY_FLAG)
    )
    if not isinstance(result, Offer):
        return result
    plan = result.quote
    rooms = rooms_for_plan(conn, clock, plan.room_type_id, plan.stay, plan.rooms)
    if isinstance(rooms, FullyBooked):
        return rooms
    quote_id = _store_quote(conn, clock, request_id, result)
    return StoredQuote(quote_id, result)


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
