"""free_rooms against the seeded database (D22: expired holds never block).

Each test runs inside a rolled-back transaction; bookings use seeded rooms in 2027.
Times come from FixedClock (D20), never from the database's now().
"""

from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.clock import FixedClock
from anemos.reservations.availability import FREE_ROOMS_SQL, free_rooms
from anemos.reservations.model import Stay

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

NOW = datetime(2027, 6, 1, 9, 0, tzinfo=UTC)
STAY = Stay(date(2027, 7, 12), date(2027, 7, 15))  # nights of 12, 13, 14 Jul
ALL_STANDARD = (101, 102, 103, 104, 105, 106)


def book(
    db: Conn,
    room_id: int,
    check_in: date,
    check_out: date,
    status: str = "confirmed",
    expires_at: datetime | None = None,
) -> None:
    db.execute(
        "INSERT INTO bookings (room_id, stay, status, expires_at, price_cents, created_at)"
        " VALUES (%s, daterange(%s, %s), %s, %s, 10000, %s)",
        (room_id, check_in, check_out, status, expires_at, NOW - timedelta(days=1)),
    )


def standard_free(db: Conn, clock: FixedClock | None = None) -> tuple[int, ...]:
    return free_rooms(db, STAY, clock or FixedClock(NOW))["standard"]


def test_everything_free_without_bookings(db: Conn) -> None:
    assert dict(free_rooms(db, STAY, FixedClock(NOW))) == {
        "accessible": (107,),
        "family_suite": (301, 302, 303),
        "sea_view": (201, 202, 203, 204),
        "standard": ALL_STANDARD,
    }


def test_confirmed_booking_blocks_its_room(db: Conn) -> None:
    book(db, 101, date(2027, 7, 12), date(2027, 7, 15))
    assert standard_free(db) == (102, 103, 104, 105, 106)


def test_adjacent_bookings_do_not_block(db: Conn) -> None:
    # Checks out the morning this stay checks in; checks in the day this stay checks out.
    book(db, 101, date(2027, 7, 9), date(2027, 7, 12))
    book(db, 101, date(2027, 7, 15), date(2027, 7, 18))
    assert standard_free(db) == ALL_STANDARD


def test_room_free_only_some_nights_is_not_free(db: Conn) -> None:
    # Tetris: room 101 is taken for the middle night only. No room changes mid-stay.
    book(db, 101, date(2027, 7, 13), date(2027, 7, 14))
    assert 101 not in standard_free(db)


def test_type_with_nothing_free_is_present_and_empty(db: Conn) -> None:
    for room in (301, 302, 303):
        book(db, room, date(2027, 7, 1), date(2027, 7, 31))
    assert free_rooms(db, STAY, FixedClock(NOW))["family_suite"] == ()


@pytest.mark.parametrize(
    ("expires_in", "blocks"),
    [
        (timedelta(seconds=-1), False),  # expired 1 s ago: free (D22)
        (timedelta(0), True),  # exactly now: still blocks (free only once < now)
        (timedelta(seconds=1), True),  # expires in 1 s: blocks
    ],
)
def test_hold_blocks_until_it_expires(db: Conn, expires_in: timedelta, blocks: bool) -> None:
    book(db, 101, date(2027, 7, 12), date(2027, 7, 15), "hold", NOW + expires_in)
    assert (101 not in standard_free(db)) is blocks


def test_advancing_the_clock_frees_a_hold(db: Conn) -> None:
    book(db, 101, date(2027, 7, 12), date(2027, 7, 15), "hold", NOW + timedelta(hours=48))
    clock = FixedClock(NOW)
    assert 101 not in standard_free(db, clock)
    clock.advance(timedelta(hours=48, seconds=1))
    assert 101 in standard_free(db, clock)


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_inactive_bookings_are_ignored(db: Conn, status: str) -> None:
    book(db, 101, date(2027, 7, 12), date(2027, 7, 15), status)
    assert standard_free(db) == ALL_STANDARD


def test_query_is_read_only(db: Conn) -> None:
    # An expired hold is filtered, not flipped: flipping happens at approval (D10).
    book(db, 101, date(2027, 7, 12), date(2027, 7, 15), "hold", NOW - timedelta(hours=1))
    free_rooms(db, STAY, FixedClock(NOW))
    assert db.execute("SELECT status FROM bookings WHERE room_id = 101").fetchone() == ("hold",)


def test_sql_never_reads_the_database_clock() -> None:
    sql = FREE_ROOMS_SQL.lower()
    assert "now()" not in sql
    assert "current_timestamp" not in sql
