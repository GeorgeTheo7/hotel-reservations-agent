"""The read tools check_availability and quote_price against the seeded database.

Each test runs in a rolled-back transaction (`db`); requests and bookings are built
with the `rows` fixture. Times come from a FixedClock (D20).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.clock import FixedClock
from anemos.reservations.errors import (
    Closed,
    FullyBooked,
    InvalidDates,
    MinStay,
    NeedsEscalation,
    NeedsInfo,
    ReasonCode,
)
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.model import Stay
from anemos.reservations.offer import OfferChange
from anemos.reservations.tools import Availability, StoredQuote, check_availability, quote_price

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
ROOMS_BY_TYPE = {
    "accessible": (107,), "family_suite": (301, 302, 303), "sea_view": (201, 202, 203, 204),
    "standard": (101, 102, 103, 104, 105, 106),
}  # fmt: skip


def d(month: int, day: int, year: int = 2027) -> date:
    return date(year, month, day)


@pytest.fixture
def hotel(db: Conn) -> HotelData:
    return load_hotel_data(db)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def counts(db: Conn) -> tuple[int, int]:
    row = db.execute("SELECT (SELECT count(*) FROM bookings), (SELECT count(*) FROM quotes)")
    result = row.fetchone()
    assert result is not None
    return int(result[0]), int(result[1])


# --- check_availability -------------------------------------------------------------


def test_everything_free(db: Conn, hotel: HotelData, clock: FixedClock) -> None:
    result = check_availability(db, hotel, clock, d(6, 1), d(6, 4))
    assert result == Availability(Stay(d(6, 1), d(6, 4)), ROOMS_BY_TYPE)


def test_seeded_room_numbers_are_the_ones_assumed(db: Conn) -> None:
    rows = db.execute("SELECT room_type_id, array_agg(id ORDER BY id) FROM rooms GROUP BY 1")
    assert {type_id: tuple(ids) for type_id, ids in rows.fetchall()} == ROOMS_BY_TYPE


@pytest.mark.parametrize(
    ("check_in", "check_out"),
    [(d(6, 1), d(6, 1)), (d(6, 4), d(6, 1))],
    ids=["0-nights", "reversed"],
)
def test_invalid_dates(
    db: Conn, hotel: HotelData, clock: FixedClock, check_in: date, check_out: date
) -> None:
    assert isinstance(check_availability(db, hotel, clock, check_in, check_out), InvalidDates)


def test_closed(db: Conn, hotel: HotelData, clock: FixedClock) -> None:
    result = check_availability(db, hotel, clock, d(10, 30), d(11, 2))
    assert result == Closed((d(11, 1),))


def test_min_stay(db: Conn, hotel: HotelData, clock: FixedClock) -> None:
    """Ex 4: arrivals 10-20 Aug need 5 nights."""
    assert check_availability(db, hotel, clock, d(8, 10), d(8, 13)) == MinStay(5)


def test_no_calendar_escalates(db: Conn, hotel: HotelData, clock: FixedClock) -> None:
    result = check_availability(db, hotel, clock, d(6, 1, 2028), d(6, 4, 2028))
    assert isinstance(result, NeedsEscalation)
    assert result.reason is ReasonCode.NO_CALENDAR


def test_booked_room_is_not_free(db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock) -> None:
    rows.booking(room_id=201, stay="[2027-06-02,2027-06-05)")
    result = check_availability(db, hotel, clock, d(6, 1), d(6, 4))
    assert isinstance(result, Availability)
    assert result.free_rooms["sea_view"] == (202, 203, 204)


def test_expired_hold_is_free_and_not_flipped(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """D22: the read filters a stale hold; only approval flips it."""
    booking_id = rows.booking(
        room_id=101, stay="[2027-06-01,2027-06-04)", status="hold",
        expires_at=NOW - timedelta(minutes=1),
    )  # fmt: skip
    result = check_availability(db, hotel, clock, d(6, 1), d(6, 4))
    assert isinstance(result, Availability)
    assert 101 in result.free_rooms["standard"]
    status = db.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,)).fetchone()
    assert status == ("hold",)


# --- quote_price ----------------------------------------------------------------------


def stored_quote(db: Conn, quote_id: int) -> dict[str, Any]:
    cur = db.execute(
        "SELECT room_type_id, lower(stay), upper(stay), rooms, jsonb_array_length(lines),"
        " extra_bed_bands, accommodation_subtotal_cents, city_tax_cents, deposit_cents,"
        " free_cancellation_until, changes, hold_allowed, created_at"
        " FROM quotes WHERE id = %s",
        (quote_id,),
    )
    assert cur.description is not None
    names = [column.name for column in cur.description]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def test_exact_request_is_quoted_and_stored(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """Ex 2: Sea View 12-16 Jul, 2 adults + child 8: 660 + 16 tax, deposit 198."""
    request_id = rows.request(
        stay="[2027-07-12,2027-07-16)", adults=2, children=1, child_ages=[8], rooms=1,
        room_type_id="sea_view",
    )  # fmt: skip
    result = quote_price(db, hotel, clock, request_id)
    assert isinstance(result, StoredQuote)
    assert result.offer.hold_allowed is True
    assert stored_quote(db, result.quote_id) == {
        "room_type_id": "sea_view",
        "lower": d(7, 12),
        "upper": d(7, 16),
        "rooms": 1,
        "jsonb_array_length": 4,
        "extra_bed_bands": ["child_2_11"],
        "accommodation_subtotal_cents": 66_000,
        "city_tax_cents": 1_600,
        "deposit_cents": 19_800,
        "free_cancellation_until": d(6, 28),
        "changes": [],
        "hold_allowed": True,
        "created_at": NOW,
    }


def test_stored_lines_match_the_quote(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    request_id = rows.request(stay="[2027-07-12,2027-07-16)", adults=2, room_type_id="sea_view")
    result = quote_price(db, hotel, clock, request_id)
    assert isinstance(result, StoredQuote)
    lines = db.execute("SELECT lines FROM quotes WHERE id = %s", (result.quote_id,)).fetchone()
    assert lines is not None
    assert [(line["night"], line["season"], line["room_cents"]) for line in lines[0]] == [
        ("2027-07-12", "mid", 13_000),
        ("2027-07-13", "mid", 13_000),
        ("2027-07-14", "mid", 13_000),
        ("2027-07-15", "high", 19_000),
    ]


@pytest.mark.parametrize(
    ("request_row", "expected_type", "expected_stay", "changes"),
    [
        pytest.param(
            {"stay": "[2027-08-10,2027-08-13)", "adults": 2, "room_type_id": "sea_view"},
            "sea_view", (d(8, 10), d(8, 15)), ["min_stay"], id="ex04-min-stay",
        ),
        pytest.param(
            {"stay": "[2027-10-30,2027-11-02)", "adults": 2, "room_type_id": "standard"},
            "standard", (d(10, 30), d(11, 1)), ["open_part"], id="ex07-open-part",
        ),
        pytest.param(
            {"stay": "[2027-06-05,2027-06-08)", "adults": 4, "room_type_id": "sea_view"},
            "family_suite", (d(6, 5), d(6, 8)), ["other_type"], id="ex08-over-capacity",
        ),
    ],
)  # fmt: skip
def test_changed_offer_is_stored_with_its_changes_and_no_hold(
    db: Conn,
    rows: Rows,
    hotel: HotelData,
    clock: FixedClock,
    request_row: dict[str, Any],
    expected_type: str,
    expected_stay: tuple[date, date],
    changes: list[str],
) -> None:
    result = quote_price(db, hotel, clock, rows.request(**request_row))
    assert isinstance(result, StoredQuote)
    stored = stored_quote(db, result.quote_id)
    assert (stored["room_type_id"], (stored["lower"], stored["upper"])) == (
        expected_type,
        expected_stay,
    )
    assert stored["changes"] == changes
    assert stored["hold_allowed"] is False


@pytest.mark.parametrize(
    ("request_row", "missing"),
    [
        ({"adults": 2}, "dates of the stay"),
        ({"stay": "[2027-06-01,2027-06-04)"}, "number of adults"),
        ({"stay": "[2027-06-01,2027-06-04)", "adults": 2, "children": 2, "child_ages": [8]},
         "children's ages"),
    ],
    ids=["no-dates", "no-adults", "missing-age"],
)  # fmt: skip
def test_missing_facts_need_info_and_store_nothing(
    db: Conn,
    rows: Rows,
    hotel: HotelData,
    clock: FixedClock,
    request_row: dict[str, Any],
    missing: str,
) -> None:
    request_id = rows.request(**request_row)
    before = counts(db)
    assert quote_price(db, hotel, clock, request_id) == NeedsInfo(missing)
    assert counts(db) == before


def test_rooms_not_stated_means_one(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    request_id = rows.request(stay="[2027-06-01,2027-06-04)", adults=2)
    result = quote_price(db, hotel, clock, request_id)
    assert isinstance(result, StoredQuote)
    assert (result.offer.quote.rooms, result.offer.quote.room_type_id) == (1, "standard")
    assert result.offer.hold_allowed is True  # D55: no type named, smallest that fits


def test_accessibility_flag_from_the_extraction(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    request_id = rows.request(
        stay="[2027-06-01,2027-06-04)", adults=2, room_type_id="sea_view",
        extracted=Jsonb({"flags": ["accessibility_need"]}),
    )  # fmt: skip
    result = quote_price(db, hotel, clock, request_id)
    assert isinstance(result, StoredQuote)
    assert result.offer.quote.room_type_id == "accessible"
    assert result.offer.changes == {OfferChange.OTHER_TYPE}


def test_escalation_stores_nothing(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    request_id = rows.request(stay="[2027-06-01,2027-06-04)", adults=6, rooms=3)
    before = counts(db)
    result = quote_price(db, hotel, clock, request_id)
    assert isinstance(result, NeedsEscalation)
    assert result.reason is ReasonCode.GROUP
    assert counts(db) == before


def test_closed_stores_nothing(db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock) -> None:
    request_id = rows.request(stay="[2027-11-05,2027-11-08)", adults=2)
    before = counts(db)
    assert isinstance(quote_price(db, hotel, clock, request_id), Closed)
    assert counts(db) == before


def test_quote_has_no_inventory_effect(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """Spec Tools: quote_price stores a quote, never a booking."""
    bookings_before, quotes_before = counts(db)
    request_id = rows.request(stay="[2027-06-01,2027-06-04)", adults=2, room_type_id="standard")
    assert isinstance(quote_price(db, hotel, clock, request_id), StoredQuote)
    assert counts(db) == (bookings_before, quotes_before + 1)


def test_two_rooms_needed_one_free_is_fully_booked(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """D59: 4 adults, 2 Standard rooms asked; 5 of 6 Standard rooms booked. Nothing is
    stored; the model's next step is suggest_alternatives."""
    for room in ROOMS_BY_TYPE["standard"][:5]:
        rows.booking(room_id=room, stay="[2027-06-01,2027-06-04)")
    request_id = rows.request(
        stay="[2027-06-01,2027-06-04)", adults=4, rooms=2, room_type_id="standard"
    )
    before = counts(db)
    result = quote_price(db, hotel, clock, request_id)
    assert result == FullyBooked("standard", rooms_needed=2, rooms_free=1)
    assert counts(db) == before


def test_room_held_by_an_expired_hold_counts_as_free(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """D22 inside D59: three Sea View rooms booked, the fourth held by a hold that
    expired a minute ago -> the quote is stored."""
    sea_view = ROOMS_BY_TYPE["sea_view"]
    for room in sea_view[:3]:
        rows.booking(room_id=room, stay="[2027-06-01,2027-06-04)")
    rows.booking(
        room_id=sea_view[3], stay="[2027-06-01,2027-06-04)", status="hold",
        expires_at=NOW - timedelta(minutes=1),
    )  # fmt: skip
    request_id = rows.request(stay="[2027-06-01,2027-06-04)", adults=2, room_type_id="sea_view")
    assert isinstance(quote_price(db, hotel, clock, request_id), StoredQuote)


def test_the_d27_substitute_is_the_type_checked(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """Ex 8: Sea View asked for 4 adults -> Family Suite. All Suites booked ->
    FULLY_BOOKED for the Suite, and nothing stored."""
    for room in ROOMS_BY_TYPE["family_suite"]:
        rows.booking(room_id=room, stay="[2027-06-05,2027-06-08)")
    request_id = rows.request(stay="[2027-06-05,2027-06-08)", adults=4, room_type_id="sea_view")
    before = counts(db)
    result = quote_price(db, hotel, clock, request_id)
    assert result == FullyBooked("family_suite", rooms_needed=1, rooms_free=0)
    assert counts(db) == before


def test_the_type_asked_is_not_checked_when_substituted(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """The other half: every Sea View booked, Suites free -> the Suite quote is stored,
    because the Sea View was never going to be offered to 4 adults."""
    for room in ROOMS_BY_TYPE["sea_view"]:
        rows.booking(room_id=room, stay="[2027-06-05,2027-06-08)")
    request_id = rows.request(stay="[2027-06-05,2027-06-08)", adults=4, room_type_id="sea_view")
    result = quote_price(db, hotel, clock, request_id)
    assert isinstance(result, StoredQuote)
    assert result.offer.quote.room_type_id == "family_suite"


def test_unknown_request_is_a_bug(db: Conn, hotel: HotelData, clock: FixedClock) -> None:
    with pytest.raises(LookupError, match="no request 999999999"):
        quote_price(db, hotel, clock, 999_999_999)


def test_each_call_stores_a_new_quote(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """D58: quotes are never reused; each call is its own priced snapshot."""
    request_id = rows.request(stay="[2027-06-01,2027-06-04)", adults=2, room_type_id="standard")
    first = quote_price(db, hotel, clock, request_id)
    clock.advance(timedelta(minutes=5))
    second = quote_price(db, hotel, clock, request_id)
    assert isinstance(first, StoredQuote)
    assert isinstance(second, StoredQuote)
    assert first.quote_id != second.quote_id
    times = db.execute(
        "SELECT created_at FROM quotes WHERE request_id = %s ORDER BY id", (request_id,)
    ).fetchall()
    assert times == [(NOW,), (NOW + timedelta(minutes=5),)]
