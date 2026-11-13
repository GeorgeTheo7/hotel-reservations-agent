"""Constraints of migration 0003 (extra_bed_rates). Each test runs in a savepoint."""

from collections.abc import Callable
from contextlib import AbstractContextManager

import psycopg
import pytest
from psycopg.errors import CheckViolation, UniqueViolation
from psycopg.rows import TupleRow

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]
type ExpectViolation = Callable[[type[psycopg.Error], str], AbstractContextManager[None]]


def test_seeded_rates(db: Conn) -> None:
    rows = db.execute("SELECT band, rate_cents FROM extra_bed_rates ORDER BY band").fetchall()
    assert rows == [("age_12_plus", 3500), ("child_2_11", 2000)]


def test_unknown_band_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "extra_bed_rates_band_valid"):
        db.execute("INSERT INTO extra_bed_rates (band, rate_cents) VALUES ('infant', 0)")


@pytest.mark.parametrize("cents", [0, -100])
def test_rate_must_be_positive(db: Conn, expect_violation: ExpectViolation, cents: int) -> None:
    with expect_violation(CheckViolation, "extra_bed_rates_rate_positive"):
        db.execute("UPDATE extra_bed_rates SET rate_cents = %s WHERE band = 'child_2_11'", (cents,))


def test_one_row_per_band(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(UniqueViolation, "extra_bed_rates_pkey"):
        db.execute("INSERT INTO extra_bed_rates (band, rate_cents) VALUES ('child_2_11', 2500)")
