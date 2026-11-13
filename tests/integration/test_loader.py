"""The DB loader returns the same HotelData snapshot as the seed constants."""

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.db.seed import hotel_data
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.model import ExtraBedBand

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]


def test_loaded_snapshot_equals_seed_snapshot(db: Conn) -> None:
    assert load_hotel_data(db) == hotel_data()


def test_loader_reads_the_database_not_the_constants(db: Conn) -> None:
    db.execute("UPDATE extra_bed_rates SET rate_cents = 2100 WHERE band = 'child_2_11'")
    loaded = load_hotel_data(db)
    assert loaded.extra_bed_rates[ExtraBedBand.CHILD_2_11] == 2100
    assert loaded != hotel_data()


def test_loaded_amounts_are_int(db: Conn) -> None:
    loaded = load_hotel_data(db)
    amounts = [
        *loaded.rates.values(),
        *loaded.city_tax_cents.values(),
        *loaded.extra_bed_rates.values(),
    ]
    assert amounts
    assert all(type(cents) is int for cents in amounts)
