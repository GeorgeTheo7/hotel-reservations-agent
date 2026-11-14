"""suggest_alternatives against the seeded database (spec Unmet requests, D33, D59).

Prices for 2 adults, 1-4 Jun 2027 (Mid, 3 nights): Standard 285, Accessible 285,
Sea View 390, Family Suite 540. Seeded rooms: Standard 101-106, Accessible 107,
Sea View 201-204, Family Suite 301-303.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.clock import FixedClock
from anemos.reservations.errors import (
    NeedsEscalation,
    NeedsInfo,
    NoneFound,
    OriginalAvailable,
    ReasonCode,
)
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.model import Stay
from anemos.reservations.offer import OfferChange
from anemos.reservations.tools import Alternatives, suggest_alternatives

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
STANDARD = (101, 102, 103, 104, 105, 106)
ACCESSIBLE = (107,)
SEA_VIEW = (201, 202, 203, 204)
SUITES = (301, 302, 303)
ALL_ROOMS = STANDARD + ACCESSIBLE + SEA_VIEW + SUITES
JUNE = "[2027-06-01,2027-06-04)"


def d(month: int, day: int) -> date:
    return date(2027, month, day)


@pytest.fixture
def hotel(db: Conn) -> HotelData:
    return load_hotel_data(db)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def book(rows: Rows, rooms: Iterable[int], stay: str) -> None:
    for room in rooms:
        rows.booking(room_id=room, stay=stay)


def quote_count(db: Conn) -> int:
    row = db.execute("SELECT count(*) FROM quotes").fetchone()
    assert row is not None
    return int(row[0])


def summary(result: object) -> tuple[OfferChange, list[tuple[str, Stay, frozenset[OfferChange]]]]:
    assert isinstance(result, Alternatives), result
    return result.kind, [
        (o.offer.quote.room_type_id, o.offer.quote.stay, o.offer.changes) for o in result.options
    ]


def request(rows: Rows, **overrides: Any) -> int:
    columns: dict[str, Any] = {"stay": JUNE, "adults": 2} | overrides
    return rows.request(**columns)


OTHER_TYPE = frozenset({OfferChange.OTHER_TYPE})
OTHER_DATES = frozenset({OfferChange.OTHER_DATES})
JUNE_STAY = Stay(d(6, 1), d(6, 4))


# --- other room types -----------------------------------------------------------------


def test_other_types_cheapest_first_at_most_two(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """Sea View full -> Standard (285) and Family Suite (540); the Accessible room
    (also 285) is left out because other types are free (D33)."""
    book(rows, SEA_VIEW, JUNE)
    result = suggest_alternatives(db, hotel, clock, request(rows, room_type_id="sea_view"))
    assert summary(result) == (
        OfferChange.OTHER_TYPE,
        [("standard", JUNE_STAY, OTHER_TYPE), ("family_suite", JUNE_STAY, OTHER_TYPE)],
    )


def test_alternatives_are_stored_and_never_held(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    book(rows, SEA_VIEW, JUNE)
    before = quote_count(db)
    result = suggest_alternatives(db, hotel, clock, request(rows, room_type_id="sea_view"))
    assert isinstance(result, Alternatives)
    assert quote_count(db) == before + 2
    held = db.execute(
        "SELECT hold_allowed, changes FROM quotes WHERE id = ANY(%s) ORDER BY id",
        ([option.quote_id for option in result.options],),
    ).fetchall()
    assert held == [(False, ["other_type"]), (False, ["other_type"])]


def test_no_type_named_another_type_is_not_a_change(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """D55: no type named, so any type was asked. (Two options are still never held
    together: guardrail 7.)"""
    book(rows, STANDARD, JUNE)
    result = suggest_alternatives(db, hotel, clock, request(rows))
    assert summary(result) == (
        OfferChange.OTHER_TYPE,
        [("sea_view", JUNE_STAY, frozenset()), ("family_suite", JUNE_STAY, frozenset())],
    )


def test_accessible_room_only_when_nothing_else_is_free(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    book(rows, STANDARD + SEA_VIEW + SUITES, JUNE)
    result = suggest_alternatives(db, hotel, clock, request(rows, room_type_id="sea_view"))
    assert summary(result) == (OfferChange.OTHER_TYPE, [("accessible", JUNE_STAY, OTHER_TYPE)])


def test_alternative_types_must_fit_the_party(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """3 adults: Standard and Accessible hold 2, so only the Family Suite is left."""
    book(rows, SEA_VIEW, JUNE)
    result = suggest_alternatives(
        db, hotel, clock, request(rows, adults=3, room_type_id="sea_view")
    )
    assert summary(result) == (OfferChange.OTHER_TYPE, [("family_suite", JUNE_STAY, OTHER_TYPE)])


def test_calendar_changes_of_the_offer_are_kept(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """Ex 4: 10-13 Aug becomes the 5-night min-stay version; Sea View full for it ->
    the alternatives say both: min stay and other type."""
    book(rows, SEA_VIEW, "[2027-08-10,2027-08-15)")
    request_id = request(rows, stay="[2027-08-10,2027-08-13)", room_type_id="sea_view")
    kind, options = summary(suggest_alternatives(db, hotel, clock, request_id))
    assert kind is OfferChange.OTHER_TYPE
    both = frozenset({OfferChange.MIN_STAY, OfferChange.OTHER_TYPE})
    assert options == [
        ("standard", Stay(d(8, 10), d(8, 15)), both),
        ("family_suite", Stay(d(8, 10), d(8, 15)), both),
    ]


# --- other dates (+-7 days) ---------------------------------------------------------


def test_all_booked_gives_the_nearest_same_length_stays(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """Every room booked 1-4 Jun: shifts of 1 and 2 days overlap; -3 (29 May) and +3
    (4 Jun) are free. Equal distance -> the earlier stay first."""
    book(rows, ALL_ROOMS, JUNE)
    result = suggest_alternatives(db, hotel, clock, request(rows, room_type_id="standard"))
    assert summary(result) == (
        OfferChange.OTHER_DATES,
        [
            ("standard", Stay(d(5, 29), d(6, 1)), OTHER_DATES),
            ("standard", Stay(d(6, 4), d(6, 7)), OTHER_DATES),
        ],
    )


def test_shifted_stays_must_pass_the_calendar(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """15-17 Apr (opening day) fully booked: earlier shifts are closed and skipped;
    +1 overlaps; +2 and +3 are the two options."""
    book(rows, ALL_ROOMS, "[2027-04-15,2027-04-17)")
    request_id = request(rows, stay="[2027-04-15,2027-04-17)", room_type_id="standard")
    assert summary(suggest_alternatives(db, hotel, clock, request_id)) == (
        OfferChange.OTHER_DATES,
        [
            ("standard", Stay(d(4, 17), d(4, 19)), OTHER_DATES),
            ("standard", Stay(d(4, 18), d(4, 20)), OTHER_DATES),
        ],
    )


def test_short_notice_and_past_stays_are_skipped(db: Conn, rows: Rows, hotel: HotelData) -> None:
    """Received 28 May: a 29 May arrival is under 48 h (D31) and 28 May is today or
    past, so the earlier shifts are skipped; +3 and +4 remain."""
    book(rows, ALL_ROOMS, JUNE)
    clock = FixedClock(datetime(2027, 5, 28, 9, 0, tzinfo=UTC))
    result = suggest_alternatives(db, hotel, clock, request(rows, room_type_id="standard"))
    assert summary(result) == (
        OfferChange.OTHER_DATES,
        [
            ("standard", Stay(d(6, 4), d(6, 7)), OTHER_DATES),
            ("standard", Stay(d(6, 5), d(6, 8)), OTHER_DATES),
        ],
    )


def test_accessibility_need_never_gets_other_types(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """D33: only the Accessible room meets the need; when it is taken, other dates."""
    book(rows, ACCESSIBLE, JUNE)
    request_id = request(rows, extracted=Jsonb({"flags": ["accessibility_need"]}))
    assert summary(suggest_alternatives(db, hotel, clock, request_id)) == (
        OfferChange.OTHER_DATES,
        [
            ("accessible", Stay(d(5, 29), d(6, 1)), OTHER_DATES),
            ("accessible", Stay(d(6, 4), d(6, 7)), OTHER_DATES),
        ],
    )


# --- nothing found, and errors of the base offer ------------------------------------


def test_nothing_free_is_none_found_and_stores_nothing(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    book(rows, ALL_ROOMS, "[2027-05-24,2027-06-12)")
    before = quote_count(db)
    result = suggest_alternatives(db, hotel, clock, request(rows, room_type_id="standard"))
    assert result == NoneFound(d(6, 1), d(6, 4), days_searched=7)
    assert quote_count(db) == before


def test_missing_ages_need_info_and_store_nothing(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    request_id = request(rows, children=1)
    before = quote_count(db)
    assert suggest_alternatives(db, hotel, clock, request_id) == NeedsInfo("children's ages")
    assert quote_count(db) == before


def test_escalation_of_the_base_offer_is_returned(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    result = suggest_alternatives(db, hotel, clock, request(rows, adults=6, rooms=3))
    assert isinstance(result, NeedsEscalation)
    assert result.reason is ReasonCode.GROUP


def test_original_plan_free_points_to_quote_price_and_stores_nothing(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """D60: called while the Sea View asked for is still free (one of four left)."""
    book(rows, SEA_VIEW[:3], JUNE)
    before = quote_count(db)
    result = suggest_alternatives(db, hotel, clock, request(rows, room_type_id="sea_view"))
    assert result == OriginalAvailable("sea_view", rooms=1)
    assert quote_count(db) == before


def test_original_available_checks_the_d27_substitute(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """Ex 8 shape: Sea View asked for 4 adults -> the plan is a Family Suite. Every Sea
    View booked, Suites free -> the original plan (the Suite) is available."""
    book(rows, SEA_VIEW, JUNE)
    request_id = request(rows, adults=4, room_type_id="sea_view")
    result = suggest_alternatives(db, hotel, clock, request_id)
    assert result == OriginalAvailable("family_suite", rooms=1)
