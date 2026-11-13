"""Build the HotelData snapshot from the database (reads only).

One of the two places in `reservations/` that touch the database (the other is
availability). Everything downstream works on the returned snapshot, without SQL.
"""

import psycopg
from psycopg.rows import TupleRow

from anemos.reservations.hotel_data import HotelData, MinStayRule, SeasonPeriod
from anemos.reservations.model import ExtraBedBand, RoomType, Season
from anemos.reservations.money import Cents


def load_hotel_data(conn: psycopg.Connection[TupleRow]) -> HotelData:
    room_types = {
        str(row[0]): RoomType(str(row[0]), str(row[1]), int(row[2]), int(row[3]), row[4], row[5])
        for row in conn.execute(
            "SELECT id, name, standard_occupancy, max_occupancy, extra_beds,"
            " offer_only_if_needed FROM room_types"
        )
    }
    # Postgres returns a daterange as psycopg's Range; [lower, upper) by our CHECKs.
    season_periods = tuple(
        SeasonPeriod(Season(row[0]), row[1].lower, row[1].upper)
        for row in conn.execute("SELECT season_id, nights FROM season_periods")
    )
    city_tax = {
        Season(row[0]): Cents(int(row[1]))
        for row in conn.execute("SELECT id, city_tax_cents FROM seasons")
    }
    rates = {
        (str(row[0]), Season(row[1])): Cents(int(row[2]))
        for row in conn.execute("SELECT room_type_id, season_id, rate_cents FROM room_rates")
    }
    min_stay_rules = tuple(
        MinStayRule(row[0].lower, row[0].upper, int(row[1]))
        for row in conn.execute("SELECT arrivals, min_nights FROM min_stay_rules")
    )
    extra_bed_rates = {
        ExtraBedBand(row[0]): Cents(int(row[1]))
        for row in conn.execute("SELECT band, rate_cents FROM extra_bed_rates")
    }
    return HotelData(
        room_types=room_types,
        season_periods=season_periods,
        city_tax_cents=city_tax,
        rates=rates,
        min_stay_rules=min_stay_rules,
        extra_bed_rates=extra_bed_rates,
    )
