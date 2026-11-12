"""Seed data for Hotel Anemos, season 2027 (D21). All data is fictional.

Idempotent: running it again gives the same rows.
- Room types, rooms, seasons and rates are upserted (inserted or updated by key).
- The 2027 calendar (season periods, minimum stays) is replaced as a whole.
- Bookings are never touched.

Run: uv run --env-file .env python -m anemos.db.seed
"""

from datetime import date

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

from anemos.db.connection import connect

SEASON_YEAR = 2027

# id, name, standard occupancy, max occupancy, extra beds, offer only if needed
ROOM_TYPES = [
    ("standard", "Standard Double", 2, 2, "cot_only", False),
    ("sea_view", "Sea View Double", 2, 3, "extra_bed", False),
    ("family_suite", "Family Suite", 2, 4, "sofa_beds", False),
    ("accessible", "Accessible Double", 2, 2, "cot_only", True),
]

# room number -> room type
ROOMS = {
    **{number: "standard" for number in range(101, 107)},
    107: "accessible",
    **{number: "sea_view" for number in range(201, 205)},
    **{number: "family_suite" for number in range(301, 304)},
}

# season -> city tax per room-night, in cents
SEASONS = {"low": 150, "mid": 400, "high": 400}

# room type -> rate per room-night in cents for (low, mid, high)
RATES = {
    "standard": (7_000, 9_500, 14_000),
    "sea_view": (9_500, 13_000, 19_000),
    "family_suite": (14_000, 18_000, 26_000),
    "accessible": (7_000, 9_500, 14_000),
}

# season, first night, end (exclusive: the day after the last night).
# Open 15 Apr - 31 Oct: the last open night is 31 Oct (check-out 1 Nov).
SEASON_PERIODS = [
    ("low", date(2027, 4, 15), date(2027, 6, 1)),
    ("mid", date(2027, 6, 1), date(2027, 7, 15)),
    ("high", date(2027, 7, 15), date(2027, 9, 1)),
    ("mid", date(2027, 9, 1), date(2027, 10, 1)),
    ("low", date(2027, 10, 1), date(2027, 11, 1)),
]

# first arrival date, end (exclusive), minimum nights. By arrival date: Low 1, Mid 2,
# High 3, except arrivals 10-20 Aug: 5.
MIN_STAY_RULES = [
    (date(2027, 4, 15), date(2027, 6, 1), 1),
    (date(2027, 6, 1), date(2027, 7, 15), 2),
    (date(2027, 7, 15), date(2027, 8, 10), 3),
    (date(2027, 8, 10), date(2027, 8, 21), 5),
    (date(2027, 8, 21), date(2027, 9, 1), 3),
    (date(2027, 9, 1), date(2027, 10, 1), 2),
    (date(2027, 10, 1), date(2027, 11, 1), 1),
]


def seed(conn: psycopg.Connection[TupleRow]) -> None:
    """Write the seed data in one transaction (a savepoint if one is already open)."""
    year = (date(SEASON_YEAR, 1, 1), date(SEASON_YEAR + 1, 1, 1))
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO room_types"
            " (id, name, standard_occupancy, max_occupancy, extra_beds, offer_only_if_needed)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name,"
            " standard_occupancy = EXCLUDED.standard_occupancy,"
            " max_occupancy = EXCLUDED.max_occupancy, extra_beds = EXCLUDED.extra_beds,"
            " offer_only_if_needed = EXCLUDED.offer_only_if_needed",
            ROOM_TYPES,
        )
        cur.executemany(
            "INSERT INTO rooms (id, room_type_id) VALUES (%s, %s)"
            " ON CONFLICT (id) DO UPDATE SET room_type_id = EXCLUDED.room_type_id",
            list(ROOMS.items()),
        )
        cur.executemany(
            "INSERT INTO seasons (id, city_tax_cents) VALUES (%s, %s)"
            " ON CONFLICT (id) DO UPDATE SET city_tax_cents = EXCLUDED.city_tax_cents",
            list(SEASONS.items()),
        )
        cur.executemany(
            "INSERT INTO room_rates (room_type_id, season_id, rate_cents) VALUES (%s, %s, %s)"
            " ON CONFLICT (room_type_id, season_id) DO UPDATE SET rate_cents = EXCLUDED.rate_cents",
            [
                (room_type, season, cents)
                for room_type, rates in RATES.items()
                for season, cents in zip(SEASONS, rates, strict=True)
            ],
        )
        cur.execute("DELETE FROM season_periods WHERE nights && daterange(%s, %s)", year)
        cur.executemany(
            "INSERT INTO season_periods (season_id, nights) VALUES (%s, daterange(%s, %s))",
            SEASON_PERIODS,
        )
        cur.execute("DELETE FROM min_stay_rules WHERE arrivals && daterange(%s, %s)", year)
        cur.executemany(
            "INSERT INTO min_stay_rules (arrivals, min_nights) VALUES (daterange(%s, %s), %s)",
            MIN_STAY_RULES,
        )


def main() -> None:
    with connect() as conn:
        seed(conn)
        counts = {
            table: conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()
            for table in (
                "room_types",
                "rooms",
                "seasons",
                "room_rates",
                "season_periods",
                "min_stay_rules",
            )
        }
    print("Seeded:", ", ".join(f"{table} {row[0] if row else 0}" for table, row in counts.items()))


if __name__ == "__main__":
    main()
