"""Fixed-date holidays (D68) through extraction storage, quote_price and the stub
drafter: Christmas 2027 is a decline with the 2027 season, Christmas 2026 escalates
NO_CALENDAR. Nothing is priced and no quote is stored."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.clock import FixedClock
from anemos.email_cases import EmailCase, load_cases
from anemos.extraction_run import Claimed, store_extraction
from anemos.inbox import store_email
from anemos.reservations.errors import ReasonCode
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.tools import LoadedHoliday, load_request
from anemos.stub_drafter import Drafted, Escalated, draft_reply

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

CASES = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}


@pytest.fixture
def hotel(db: Conn) -> HotelData:
    return load_hotel_data(db)


def stored(db: Conn, case: EmailCase) -> tuple[int, FixedClock]:
    """The email and its gold extraction, stored as the real run stores them."""
    clock = FixedClock(case.email.received_at + timedelta(hours=1))
    email_id = store_email(db, clock, case.incoming()).email_id
    gold = case.gold_output()
    assert gold is not None
    claimed = Claimed(email_id, case.email.from_, case.email.subject, case.email.body,
                      case.email.received_at)  # fmt: skip
    store_extraction(db, clock, claimed, gold)
    return email_id, clock


def quotes(db: Conn) -> int:
    row = db.execute("SELECT count(*) FROM quotes").fetchone()
    assert row is not None
    return int(row[0])


def test_christmas_2027_declines_with_the_open_season(db: Conn, hotel: HotelData) -> None:
    email_id, clock = stored(db, CASES["el-christmas-2027"])
    row = db.execute("SELECT id FROM requests WHERE email_id = %s", (email_id,)).fetchone()
    assert row is not None
    assert isinstance(load_request(db, row[0]), LoadedHoliday)
    before = quotes(db)
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted) and result.route == "decline"
    body = db.execute(
        "SELECT body FROM draft_versions WHERE draft_id = %s", (result.draft_id,)
    ).fetchone()
    assert body is not None
    # The summary restates the holiday's days (guardrail 3); the reply names the season.
    assert "Χριστούγεννα, Παρασκευή 24 Δεκεμβρίου 2027 – Κυριακή 26 Δεκεμβρίου 2027" in body[0]
    assert "15 Απριλίου 2027" in body[0] and "31 Οκτωβρίου 2027" in body[0]
    assert quotes(db) == before


def test_christmas_2026_escalates_no_calendar(db: Conn, hotel: HotelData) -> None:
    email_id, clock = stored(db, CASES["el-christmas-holiday"])
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Escalated) and result.reason is ReasonCode.NO_CALENDAR
    row = db.execute(
        "SELECT status, reason_codes FROM emails WHERE id = %s", (email_id,)
    ).fetchone()
    assert row == ("escalated", ["NO_CALENDAR"])
