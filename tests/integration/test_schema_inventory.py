"""Constraints of migration 0002 (inventory + bookings).

Test rows use ids and dates that cannot clash with the step-8 seed data:
room type 'test_double', rooms 901/902, calendar rows in 2030.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.errors import (
    CheckViolation,
    ExclusionViolation,
    ForeignKeyViolation,
    NotNullViolation,
    UniqueViolation,
)
from psycopg.rows import TupleRow

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]
type ExpectViolation = Callable[[type[psycopg.Error], str], AbstractContextManager[None]]

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)  # fixed "now" for created_at / expires_at
STAY = "[2027-07-12,2027-07-15)"  # check-in 12 Jul, 3 nights, check-out 15 Jul
OVERLAPPING = "[2027-07-14,2027-07-17)"  # shares the night of 14 Jul with STAY


@pytest.fixture
def rooms(db: Conn) -> None:
    db.execute(
        "INSERT INTO room_types (id, name, name_el, standard_occupancy, max_occupancy,"
        " extra_beds)"
        " VALUES ('test_double', 'Test Double', 'Δοκιμαστικό', 2, 2, 'cot_only')"
    )
    db.execute(
        "INSERT INTO rooms (id, room_type_id) VALUES (901, 'test_double'), (902, 'test_double')"
    )


@pytest.fixture
def season(db: Conn) -> None:
    db.execute(
        "INSERT INTO seasons (id, city_tax_cents) VALUES ('low', 150) ON CONFLICT DO NOTHING"
    )


def insert_booking(
    db: Conn, *, room_id: int = 901, stay: str = STAY, status: str = "confirmed"
) -> int:
    expires_at = T0 + timedelta(hours=48) if status == "hold" else None
    row = db.execute(
        "INSERT INTO bookings (room_id, stay, status, expires_at, price_cents, created_at)"
        " VALUES (%s, %s::daterange, %s, %s, 42000, %s) RETURNING id",
        (room_id, stay, status, expires_at, T0),
    ).fetchone()
    assert row is not None
    return int(row[0])


# --- bookings: the double-booking exclusion constraint --------------------------------


@pytest.mark.usefixtures("rooms")
@pytest.mark.parametrize("first", ["hold", "confirmed"])
@pytest.mark.parametrize("second", ["hold", "confirmed"])
def test_overlapping_active_stays_in_same_room_rejected(
    db: Conn, expect_violation: ExpectViolation, first: str, second: str
) -> None:
    insert_booking(db, status=first)
    with expect_violation(ExclusionViolation, "bookings_no_double_booking"):
        insert_booking(db, stay=OVERLAPPING, status=second)


@pytest.mark.usefixtures("rooms")
@pytest.mark.parametrize("inactive", ["cancelled", "expired"])
def test_overlap_with_inactive_booking_allowed(db: Conn, inactive: str) -> None:
    insert_booking(db, status="confirmed")
    insert_booking(db, stay=OVERLAPPING, status=inactive)


@pytest.mark.usefixtures("rooms")
@pytest.mark.parametrize("inactive", ["cancelled", "expired"])
def test_inactive_booking_does_not_block_a_new_one(db: Conn, inactive: str) -> None:
    insert_booking(db, status=inactive)
    insert_booking(db, stay=OVERLAPPING, status="confirmed")


@pytest.mark.usefixtures("rooms")
def test_reactivating_cancelled_booking_onto_occupied_stay_rejected(
    db: Conn, expect_violation: ExpectViolation
) -> None:
    """The constraint also guards UPDATE, not only INSERT."""
    insert_booking(db, status="confirmed")
    cancelled = insert_booking(db, stay=OVERLAPPING, status="cancelled")
    with expect_violation(ExclusionViolation, "bookings_no_double_booking"):
        db.execute("UPDATE bookings SET status = 'confirmed' WHERE id = %s", (cancelled,))


@pytest.mark.usefixtures("rooms")
def test_hold_past_expiry_still_blocks_until_flipped_to_expired(
    db: Conn, expect_violation: ExpectViolation
) -> None:
    """D10/D22: the database ignores the clock; approval flips stale holds first."""
    stale = insert_booking(db, status="hold")
    db.execute("UPDATE bookings SET expires_at = %s WHERE id = %s", (T0 - timedelta(days=1), stale))
    with expect_violation(ExclusionViolation, "bookings_no_double_booking"):
        insert_booking(db, stay=OVERLAPPING, status="hold")

    db.execute("UPDATE bookings SET status = 'expired' WHERE id = %s", (stale,))
    insert_booking(db, stay=OVERLAPPING, status="hold")


@pytest.mark.usefixtures("rooms")
def test_back_to_back_stays_allowed(db: Conn) -> None:
    """[in, out): check-out day of one stay = check-in day of the next."""
    insert_booking(db, stay="[2027-07-10,2027-07-15)")
    insert_booking(db, stay="[2027-07-15,2027-07-20)")


@pytest.mark.usefixtures("rooms")
def test_one_night_stays_on_consecutive_nights_allowed(db: Conn) -> None:
    for stay in ["[2027-07-12,2027-07-13)", "[2027-07-13,2027-07-14)", "[2027-07-14,2027-07-15)"]:
        insert_booking(db, stay=stay)


@pytest.mark.usefixtures("rooms")
def test_different_rooms_same_dates_allowed(db: Conn) -> None:
    insert_booking(db, room_id=901)
    insert_booking(db, room_id=902)


@pytest.mark.usefixtures("rooms")
def test_inclusive_upper_bound_is_stored_half_open(db: Conn) -> None:
    booking = insert_booking(db, stay="[2027-07-12,2027-07-15]")
    row = db.execute("SELECT stay::text FROM bookings WHERE id = %s", (booking,)).fetchone()
    assert row == ("[2027-07-12,2027-07-16)",)


# --- bookings: check constraints and foreign keys -------------------------------------


@pytest.mark.usefixtures("rooms")
def test_hold_without_expiry_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "bookings_hold_has_expiry"):
        db.execute(
            "INSERT INTO bookings (room_id, stay, status, price_cents, created_at)"
            " VALUES (901, %s::daterange, 'hold', 42000, %s)",
            (STAY, T0),
        )


@pytest.mark.usefixtures("rooms")
def test_unknown_status_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "bookings_status_valid"):
        insert_booking(db, status="pending")


@pytest.mark.usefixtures("rooms")
@pytest.mark.parametrize("stay", ["[2027-07-12,2027-07-12)", "[2027-07-12,)", "(,2027-07-15)"])
def test_empty_or_open_ended_stay_rejected(
    db: Conn, expect_violation: ExpectViolation, stay: str
) -> None:
    with expect_violation(CheckViolation, "bookings_stay_bounded"):
        insert_booking(db, stay=stay)


@pytest.mark.usefixtures("rooms")
def test_negative_booking_price_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "bookings_price_non_negative"):
        db.execute(
            "INSERT INTO bookings (room_id, stay, status, price_cents, created_at)"
            " VALUES (901, %s::daterange, 'confirmed', -1, %s)",
            (STAY, T0),
        )


@pytest.mark.usefixtures("rooms")
def test_booking_for_unknown_room_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(ForeignKeyViolation, "bookings_room_id_fkey"):
        insert_booking(db, room_id=999)


# --- room types and rooms -------------------------------------------------------------


@pytest.mark.parametrize(("standard", "maximum"), [(3, 2), (0, 2)])
def test_invalid_occupancy_rejected(
    db: Conn, expect_violation: ExpectViolation, standard: int, maximum: int
) -> None:
    with expect_violation(CheckViolation, "room_types_occupancy_valid"):
        db.execute(
            "INSERT INTO room_types (id, name, name_el, standard_occupancy, max_occupancy,"
            " extra_beds)"
            " VALUES ('bad', 'Bad', 'Κακό', %s, %s, 'cot_only')",
            (standard, maximum),
        )


def test_unknown_extra_beds_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "room_types_extra_beds_valid"):
        db.execute(
            "INSERT INTO room_types (id, name, name_el, standard_occupancy, max_occupancy,"
            " extra_beds)"
            " VALUES ('bad', 'Bad', 'Κακό', 2, 3, 'hammock')"
        )


def test_room_of_unknown_type_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(ForeignKeyViolation, "rooms_room_type_id_fkey"):
        db.execute("INSERT INTO rooms (id, room_type_id) VALUES (903, 'no_such_type')")


# --- seasons, season periods, room rates ----------------------------------------------


def test_unknown_season_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "seasons_id_valid"):
        db.execute("INSERT INTO seasons (id, city_tax_cents) VALUES ('shoulder', 100)")


def test_negative_city_tax_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "seasons_city_tax_non_negative"):
        db.execute("INSERT INTO seasons (id, city_tax_cents) VALUES ('high', -1)")


def insert_season_period(db: Conn, nights: str) -> None:
    db.execute(
        "INSERT INTO season_periods (season_id, nights) VALUES ('low', %s::daterange)", (nights,)
    )


@pytest.mark.usefixtures("season")
def test_overlapping_season_periods_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    insert_season_period(db, "[2030-04-15,2030-06-01)")
    with expect_violation(ExclusionViolation, "season_periods_no_overlap"):
        insert_season_period(db, "[2030-05-31,2030-07-15)")


@pytest.mark.usefixtures("season")
def test_adjacent_season_periods_allowed(db: Conn) -> None:
    insert_season_period(db, "[2030-04-15,2030-06-01)")
    insert_season_period(db, "[2030-06-01,2030-07-15)")


@pytest.mark.usefixtures("season")
def test_open_ended_season_period_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "season_periods_nights_bounded"):
        insert_season_period(db, "[2030-04-15,)")


@pytest.mark.usefixtures("rooms", "season")
def test_second_rate_for_same_type_and_season_rejected(
    db: Conn, expect_violation: ExpectViolation
) -> None:
    db.execute("INSERT INTO room_rates VALUES ('test_double', 'low', 7000)")
    with expect_violation(UniqueViolation, "room_rates_pkey"):
        db.execute("INSERT INTO room_rates VALUES ('test_double', 'low', 9500)")


@pytest.mark.usefixtures("rooms", "season")
def test_zero_rate_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "room_rates_rate_positive"):
        db.execute("INSERT INTO room_rates VALUES ('test_double', 'low', 0)")


# --- minimum stays --------------------------------------------------------------------


def insert_min_stay(db: Conn, arrivals: str, min_nights: int) -> None:
    db.execute(
        "INSERT INTO min_stay_rules (arrivals, min_nights) VALUES (%s::daterange, %s)",
        (arrivals, min_nights),
    )


def test_overlapping_min_stay_rules_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    insert_min_stay(db, "[2030-07-15,2030-09-01)", 3)
    with expect_violation(ExclusionViolation, "min_stay_rules_no_overlap"):
        insert_min_stay(db, "[2030-08-10,2030-08-21)", 5)


def test_zero_min_nights_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "min_stay_rules_min_nights_positive"):
        insert_min_stay(db, "[2030-07-15,2030-09-01)", 0)


# --- room_types.name_el (migration 0004) ----------------------------------------------


def test_greek_name_must_not_be_blank(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "room_types_name_el_not_blank"):
        db.execute("UPDATE room_types SET name_el = '  ' WHERE id = 'standard'")


def test_greek_names_are_unique(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(UniqueViolation, "room_types_name_el_key"):
        db.execute("UPDATE room_types SET name_el = 'Οικογενειακή Σουίτα' WHERE id = 'standard'")


def test_greek_name_is_required(db: Conn) -> None:
    # A NOT NULL violation names the column, not a constraint, so check that instead.
    with pytest.raises(NotNullViolation) as caught, db.transaction():
        db.execute("UPDATE room_types SET name_el = NULL WHERE id = 'standard'")
    assert caught.value.diag.column_name == "name_el"
