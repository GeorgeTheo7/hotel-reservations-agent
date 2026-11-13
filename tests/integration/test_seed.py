"""Seed data for 2027 (step 8). The session test database is seeded by conftest."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.db.seed import seed

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

FIRST_OPEN, LAST_OPEN = date(2027, 4, 15), date(2027, 10, 31)


def open_dates_2027() -> list[date]:
    return [FIRST_OPEN + timedelta(days=i) for i in range((LAST_OPEN - FIRST_OPEN).days + 1)]


def rate_on(db: Conn, room_type: str, night: date) -> tuple[str, int, int] | None:
    """(season, rate_cents, city_tax_cents) for one night, None if the hotel is closed."""
    row = db.execute(
        "SELECT sp.season_id, r.rate_cents, s.city_tax_cents"
        " FROM season_periods sp"
        " JOIN seasons s ON s.id = sp.season_id"
        " JOIN room_rates r ON r.season_id = sp.season_id AND r.room_type_id = %s"
        " WHERE sp.nights @> %s::date",
        (room_type, night),
    ).fetchone()
    return None if row is None else (str(row[0]), int(row[1]), int(row[2]))


def min_stay_on(db: Conn, arrival: date) -> int | None:
    row = db.execute(
        "SELECT min_nights FROM min_stay_rules WHERE arrivals @> %s::date", (arrival,)
    ).fetchone()
    return None if row is None else int(row[0])


# Every open night (= a night inside some season period), expanded to single dates.
OPEN_NIGHTS = """
    SELECT d::date AS night
    FROM season_periods sp,
         generate_series(lower(sp.nights), upper(sp.nights) - 1, interval '1 day') AS d
"""


def open_arrivals_without_exactly_one_rule(db: Conn) -> list[tuple[date, int]]:
    return [
        (row[0], row[1])
        for row in db.execute(
            f"SELECT o.night, count(m.id) FROM ({OPEN_NIGHTS}) o"
            " LEFT JOIN min_stay_rules m ON m.arrivals @> o.night"
            " GROUP BY o.night HAVING count(m.id) <> 1 ORDER BY 1"
        )
    ]


def closed_dates_with_a_rule(db: Conn) -> list[date]:
    return [
        row[0]
        for row in db.execute(
            "SELECT d::date FROM min_stay_rules m,"
            " generate_series(lower(m.arrivals), upper(m.arrivals) - 1, interval '1 day') AS d"
            " WHERE NOT EXISTS (SELECT 1 FROM season_periods sp WHERE sp.nights @> d::date)"
            " ORDER BY 1"
        )
    ]


# --- rooms ----------------------------------------------------------------------------


def test_fourteen_rooms_split_6_4_3_1(db: Conn) -> None:
    rows = db.execute("SELECT room_type_id, count(*) FROM rooms GROUP BY room_type_id")
    counts = {str(row[0]): int(row[1]) for row in rows}
    assert counts == {"standard": 6, "sea_view": 4, "family_suite": 3, "accessible": 1}


def test_room_numbers(db: Conn) -> None:
    rows = db.execute("SELECT id, room_type_id FROM rooms ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [*range(101, 108), *range(201, 205), *range(301, 304)]
    room_type_of = {int(row[0]): str(row[1]) for row in rows}
    assert room_type_of[107] == "accessible"


def test_room_types_match_spec(db: Conn) -> None:
    rows = db.execute(
        "SELECT id, standard_occupancy, max_occupancy, extra_beds, offer_only_if_needed"
        " FROM room_types ORDER BY id"
    ).fetchall()
    assert rows == [
        ("accessible", 2, 2, "cot_only", True),
        ("family_suite", 2, 4, "sofa_beds", False),
        ("sea_view", 2, 3, "extra_bed", False),
        ("standard", 2, 2, "cot_only", False),
    ]


def test_room_type_names_en_and_el(db: Conn) -> None:
    rows = db.execute("SELECT id, name, name_el FROM room_types ORDER BY id").fetchall()
    assert rows == [
        ("accessible", "Accessible Double", "Προσβάσιμο Δίκλινο"),
        ("family_suite", "Family Suite", "Οικογενειακή Σουίτα"),
        ("sea_view", "Sea View Double", "Δίκλινο με θέα θάλασσα"),
        ("standard", "Standard Double", "Δίκλινο Standard"),
    ]


# --- calendar and rates ---------------------------------------------------------------


def test_open_nights_are_exactly_15_apr_to_31_oct_2027(db: Conn) -> None:
    nights = [row[0] for row in db.execute(f"{OPEN_NIGHTS} ORDER BY 1")]
    assert nights == open_dates_2027()
    assert len(nights) == 200


def test_every_open_night_has_exactly_one_rate_per_room_type(db: Conn) -> None:
    gaps = db.execute(
        f"SELECT o.night, rt.id, count(r.rate_cents) FROM ({OPEN_NIGHTS}) o"
        " CROSS JOIN room_types rt"
        " JOIN season_periods sp ON sp.nights @> o.night"
        " LEFT JOIN room_rates r ON r.season_id = sp.season_id AND r.room_type_id = rt.id"
        " GROUP BY o.night, rt.id HAVING count(r.rate_cents) <> 1"
    ).fetchall()
    assert gaps == []
    assert db.execute("SELECT count(*) FROM room_rates").fetchone() == (12,)


@pytest.mark.parametrize(
    ("room_type", "night", "expected"),
    [
        ("standard", date(2027, 4, 15), ("low", 7_000, 150)),  # first open night
        ("standard", date(2027, 5, 2), ("low", 7_000, 150)),  # Orthodox Easter 2027
        ("sea_view", date(2027, 5, 31), ("low", 9_500, 150)),
        ("sea_view", date(2027, 6, 1), ("mid", 13_000, 400)),
        ("sea_view", date(2027, 7, 14), ("mid", 13_000, 400)),
        ("sea_view", date(2027, 7, 15), ("high", 19_000, 400)),
        ("family_suite", date(2027, 8, 31), ("high", 26_000, 400)),
        ("family_suite", date(2027, 9, 1), ("mid", 18_000, 400)),
        ("accessible", date(2027, 9, 30), ("mid", 9_500, 400)),
        ("accessible", date(2027, 10, 1), ("low", 7_000, 150)),
        ("family_suite", date(2027, 10, 31), ("low", 14_000, 150)),  # last open night
    ],
)
def test_rate_spot_checks(
    db: Conn, room_type: str, night: date, expected: tuple[str, int, int]
) -> None:
    assert rate_on(db, room_type, night) == expected


@pytest.mark.parametrize(
    "night",
    [
        date(2027, 3, 28),  # Western Easter 2027
        date(2027, 4, 14),  # day before opening
        date(2027, 11, 1),  # day after the last open night
        date(2026, 7, 15),  # not seeded (D21: 2027 only)
        date(2028, 7, 15),
    ],
)
def test_closed_nights_have_no_rate(db: Conn, night: date) -> None:
    assert rate_on(db, "standard", night) is None


# --- minimum stays --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arrival", "nights"),
    [
        (date(2027, 4, 15), 1),
        (date(2027, 5, 31), 1),
        (date(2027, 6, 1), 2),
        (date(2027, 7, 14), 2),
        (date(2027, 7, 15), 3),
        (date(2027, 8, 9), 3),
        (date(2027, 8, 10), 5),
        (date(2027, 8, 20), 5),
        (date(2027, 8, 21), 3),
        (date(2027, 9, 1), 2),
        (date(2027, 10, 31), 1),
    ],
)
def test_min_stay_spot_checks(db: Conn, arrival: date, nights: int) -> None:
    assert min_stay_on(db, arrival) == nights


def test_every_open_arrival_date_has_exactly_one_min_stay_rule(db: Conn) -> None:
    assert open_arrivals_without_exactly_one_rule(db) == []


def test_no_min_stay_rule_covers_a_closed_date(db: Conn) -> None:
    assert closed_dates_with_a_rule(db) == []


def test_min_stay_checks_detect_broken_data(db: Conn) -> None:
    """The two checks above can fail: break the data on purpose and see them report it."""
    db.execute("DELETE FROM min_stay_rules WHERE arrivals @> '2027-08-15'::date")
    db.execute(
        "INSERT INTO min_stay_rules (arrivals, min_nights) VALUES ('[2027-11-01,2027-11-03)', 1)"
    )

    missing = open_arrivals_without_exactly_one_rule(db)
    assert [night for night, _ in missing] == [date(2027, 8, d) for d in range(10, 21)]
    assert closed_dates_with_a_rule(db) == [date(2027, 11, 1), date(2027, 11, 2)]


# --- idempotence ----------------------------------------------------------------------


def snapshot(db: Conn) -> dict[str, list[tuple[Any, ...]]]:
    """All seeded content, without generated ids (period rows are deleted and re-inserted)."""
    queries = {
        "room_types": "SELECT * FROM room_types ORDER BY id",
        "rooms": "SELECT * FROM rooms ORDER BY id",
        "seasons": "SELECT * FROM seasons ORDER BY id",
        "room_rates": "SELECT * FROM room_rates ORDER BY 1, 2",
        "season_periods": "SELECT season_id, nights::text FROM season_periods ORDER BY nights",
        "min_stay_rules": "SELECT arrivals::text, min_nights FROM min_stay_rules ORDER BY arrivals",
        "extra_bed_rates": "SELECT * FROM extra_bed_rates ORDER BY band",
    }
    return {table: db.execute(query).fetchall() for table, query in queries.items()}


def test_seeding_twice_gives_the_same_rows(db: Conn) -> None:
    before = snapshot(db)
    seed(db)
    seed(db)
    assert snapshot(db) == before


def test_seeding_restores_changed_reference_data(db: Conn) -> None:
    before = snapshot(db)
    db.execute("UPDATE room_rates SET rate_cents = 1 WHERE room_type_id = 'sea_view'")
    db.execute("UPDATE rooms SET room_type_id = 'standard' WHERE id = 107")
    db.execute("DELETE FROM season_periods WHERE season_id = 'high'")
    db.execute("UPDATE extra_bed_rates SET rate_cents = 1 WHERE band = 'age_12_plus'")
    seed(db)
    assert snapshot(db) == before


def test_seeding_does_not_touch_bookings(db: Conn) -> None:
    created = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    db.execute(
        "INSERT INTO bookings (room_id, stay, status, price_cents, created_at)"
        " VALUES (201, '[2027-07-12,2027-07-15)', 'confirmed', 57000, %s)",
        (created,),
    )
    seed(db)
    assert db.execute("SELECT count(*) FROM bookings").fetchone() == (1,)
