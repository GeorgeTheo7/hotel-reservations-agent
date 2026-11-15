"""Window requests (Easter, part of a month; D7, D28): the request row stores no stay,
the DateSpec stays in `extracted`, and the tools resolve the window from there at the
email's received_at. quote_price offers up to 3 options, never held, or says why not;
the stub drafter writes them as a quote, a decline or an escalation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.approval import Approved, approve_draft
from anemos.clock import FixedClock
from anemos.email_cases import EmailCase, load_cases
from anemos.extraction_run import Claimed, store_extraction
from anemos.inbox import store_email
from anemos.reservations.errors import Closed, NeedsEscalation, NoneFound, ReasonCode
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.model import Stay
from anemos.reservations.offer import OfferChange
from anemos.reservations.tools import (
    LoadedWindow,
    WindowQuotes,
    load_request,
    quote_price,
    suggest_alternatives,
)
from anemos.stub_drafter import Drafted, draft_reply

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

CASES = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}
STANDARD_ROOMS = range(101, 107)


@pytest.fixture
def hotel(db: Conn) -> HotelData:
    return load_hotel_data(db)


def easter(year: int | None = None, tradition: str = "unspecified") -> dict[str, Any]:
    return {"kind": "holiday", "holiday": "easter", "tradition": tradition, "year": year,
            "nights": 4}  # fmt: skip


def approximate(part: str, month: int, nights: int, year: int | None = None) -> dict[str, Any]:
    return {"kind": "approximate", "part": part, "month": month, "year": year, "nights": nights}


def window_request(
    rows: Rows, date_spec: dict[str, Any], received_at: datetime, *, flags: tuple[str, ...] = (),
    **columns: Any,
) -> int:  # fmt: skip
    email_id = rows.email(received_at=received_at, message_id=f"<{date_spec}{flags}@example.test>")
    extracted = Jsonb({"date_spec": date_spec, "flags": list(flags)})
    return rows.request(email_id, extracted=extracted, **({"adults": 2} | columns))


def stays(result: object) -> list[Stay]:
    assert isinstance(result, WindowQuotes), result
    return [q.offer.quote.stay for q in result.options]


def s(m1: int, d1: int, m2: int, d2: int) -> Stay:
    return Stay(date(2027, m1, d1), date(2027, m2, d2))


EASTER_RECEIVED = datetime(2026, 12, 10, 19, 5, tzinfo=UTC)  # as en-easter-plain
CLOCK = FixedClock(EASTER_RECEIVED + timedelta(hours=1))


# --- loading the window -----------------------------------------------------------


def test_window_is_resolved_from_extracted_at_received_at(db: Conn, rows: Rows) -> None:
    request_id = window_request(rows, easter(), EASTER_RECEIVED)
    loaded = load_request(db, request_id)
    assert isinstance(loaded, LoadedWindow)
    # Orthodox Easter 2027 is 2 May: check-in 29 Apr - 5 May, 4 nights (D7, D28).
    assert (loaded.window.first_check_in, loaded.window.last_check_in, loaded.window.nights) == (
        date(2027, 4, 29), date(2027, 5, 5), 4,
    )  # fmt: skip
    assert (loaded.rooms, loaded.room_type_id) == (1, None)


def test_dates_that_need_asking_are_still_needs_info(db: Conn, rows: Rows) -> None:
    whole_month = approximate("whole_month", 6, 5)
    request_id = window_request(rows, whole_month, EASTER_RECEIVED)
    assert quote_price(db, load_hotel_data(db), CLOCK, request_id).code == "NEEDS_INFO"  # type: ignore[union-attr]


def test_a_stored_date_spec_that_does_not_parse_is_a_bug(db: Conn, rows: Rows) -> None:
    request_id = window_request(rows, {"kind": "holiday"}, EASTER_RECEIVED)
    with pytest.raises(ValueError, match="stored date_spec"):
        load_request(db, request_id)


# --- quote_price on a window ------------------------------------------------------


def test_easter_window_gives_three_options_never_held(
    db: Conn, rows: Rows, hotel: HotelData
) -> None:
    request_id = window_request(rows, easter(), EASTER_RECEIVED)
    result = quote_price(db, hotel, CLOCK, request_id)
    assert stays(result) == [s(4, 29, 5, 3), s(5, 1, 5, 5), s(5, 3, 5, 7)]  # >= 2 days apart
    assert isinstance(result, WindowQuotes)
    for option in result.options:
        assert option.offer.changes == {OfferChange.WINDOW_OPTION}
        assert option.offer.quote.room_type_id == "standard"
    stored = db.execute(
        "SELECT count(*), bool_or(hold_allowed), every('window_option' = ANY(changes))"
        " FROM quotes WHERE request_id = %s",
        (request_id,),
    ).fetchone()
    assert stored == (3, False, True)


def test_min_stay_fallback_offers_the_longer_length(db: Conn, rows: Rows, hotel: HotelData) -> None:
    # Mid August, 3 nights: arrivals 10-20 Aug need 5, so 3 fits nowhere (D7 fallback).
    request_id = window_request(rows, approximate("mid", 8, 3, 2027), EASTER_RECEIVED)
    result = quote_price(db, hotel, CLOCK, request_id)
    assert stays(result) == [s(8, 11, 8, 16), s(8, 13, 8, 18), s(8, 15, 8, 20)]
    assert isinstance(result, WindowQuotes)
    assert {o.offer.changes for o in result.options} == {
        frozenset({OfferChange.WINDOW_OPTION, OfferChange.MIN_STAY})
    }


def test_taken_check_ins_are_skipped(db: Conn, rows: Rows, hotel: HotelData) -> None:
    for room in STANDARD_ROOMS:  # every Standard room taken over 29 Apr - 3 May
        rows.booking(room_id=room, stay="[2027-04-29,2027-05-03)")
    request_id = window_request(rows, easter(), EASTER_RECEIVED)
    assert stays(quote_price(db, hotel, CLOCK, request_id)) == [s(5, 3, 5, 7), s(5, 5, 5, 9)]


def test_window_fully_taken_is_none_found(db: Conn, rows: Rows, hotel: HotelData) -> None:
    for room in STANDARD_ROOMS:
        rows.booking(room_id=room, stay="[2027-04-25,2027-05-12)")
    request_id = window_request(rows, easter(), EASTER_RECEIVED)
    before = db.execute("SELECT count(*) FROM quotes").fetchone()
    result = quote_price(db, hotel, CLOCK, request_id)
    assert result == NoneFound(date(2027, 4, 29), date(2027, 5, 9), 0, room_type_id="standard")
    assert db.execute("SELECT count(*) FROM quotes").fetchone() == before


def test_window_with_every_night_closed_is_closed(db: Conn, rows: Rows, hotel: HotelData) -> None:
    # Western Easter 2027 is 28 Mar: check-ins 25-31 Mar, all before opening (15 Apr).
    request_id = window_request(rows, easter(2027, "western"), EASTER_RECEIVED)
    result = quote_price(db, hotel, CLOCK, request_id)
    assert isinstance(result, Closed)
    assert (result.nights[0], result.nights[-1]) == (date(2027, 3, 25), date(2027, 4, 3))


def test_window_where_no_stay_passes_the_calendar_is_closed(
    db: Conn, rows: Rows, hotel: HotelData
) -> None:
    # Early April, 7 nights: check-ins 1-10 Apr; every stay has closed nights.
    request_id = window_request(rows, approximate("early", 4, 7, 2027), EASTER_RECEIVED)
    result = quote_price(db, hotel, CLOCK, request_id)
    assert isinstance(result, Closed)


def test_window_in_a_year_without_calendar_escalates(
    db: Conn, rows: Rows, hotel: HotelData
) -> None:
    request_id = window_request(rows, approximate("late", 6, 7, 2028), EASTER_RECEIVED)
    result = quote_price(db, hotel, CLOCK, request_id)
    assert isinstance(result, NeedsEscalation) and result.reason is ReasonCode.NO_CALENDAR


def test_window_with_only_short_notice_check_ins_escalates(
    db: Conn, rows: Rows, hotel: HotelData
) -> None:
    received = datetime(2027, 6, 29, 6, 0, tzinfo=UTC)  # late June: 29 and 30 Jun left
    request_id = window_request(rows, approximate("late", 6, 2), received)
    result = quote_price(db, hotel, FixedClock(received), request_id)
    assert isinstance(result, NeedsEscalation) and result.reason is ReasonCode.SHORT_NOTICE


def test_party_escalation_comes_through(db: Conn, rows: Rows, hotel: HotelData) -> None:
    request_id = window_request(
        rows, easter(), EASTER_RECEIVED, flags=("accessibility_need",), adults=3
    )
    result = quote_price(db, hotel, CLOCK, request_id)
    assert isinstance(result, NeedsEscalation) and result.reason is ReasonCode.ACCESSIBILITY


def test_suggest_alternatives_refuses_a_window(db: Conn, rows: Rows, hotel: HotelData) -> None:
    request_id = window_request(rows, easter(), EASTER_RECEIVED)
    with pytest.raises(ValueError, match="window"):
        suggest_alternatives(db, hotel, CLOCK, request_id)


# --- the gold cases through extraction storage and the stub drafter ---------------


def drafted(db: Conn, hotel: HotelData, case: EmailCase) -> tuple[Drafted, str, list[bool]]:
    """Store the case's email and gold extraction as the real run does, then draft."""
    clock = FixedClock(case.email.received_at + timedelta(hours=1))
    stored = store_email(db, clock, case.incoming())
    gold = case.gold_output()
    assert gold is not None
    claimed = Claimed(stored.email_id, case.email.from_, case.email.subject,
                      case.email.body, case.email.received_at)  # fmt: skip
    store_extraction(db, clock, claimed, gold)
    result = draft_reply(db, hotel, clock, stored.email_id)
    assert isinstance(result, Drafted), result
    body, holds = db.execute(
        "SELECT v.body, (SELECT array_agg(o.hold ORDER BY o.quote_id) FROM draft_offers o"
        " WHERE o.draft_id = v.draft_id) FROM draft_versions v WHERE v.draft_id = %s",
        (result.draft_id,),
    ).fetchone()  # type: ignore[misc]
    return result, body, holds or []


def test_en_easter_plain_is_a_quote_with_window_options(db: Conn, hotel: HotelData) -> None:
    result, body, holds = drafted(db, hotel, CASES["en-easter-plain"])
    assert result.route == "quote"
    assert holds == [False, False, False]
    assert "check-in between" in body  # the summary restates the window (guardrail 3)
    assert body.count("one of the options within the dates you gave us") == 3
    assert "within the dates you mentioned" in body


def test_el_approximate_late_june_is_a_quote_with_window_options(
    db: Conn, hotel: HotelData
) -> None:
    result, body, holds = drafted(db, hotel, CASES["el-approximate-late-june"])
    assert result.route == "quote"
    assert holds == [False, False, False]
    assert "άφιξη από" in body
    assert body.count("μία από τις επιλογές μέσα στις ημερομηνίες") == 3


def test_en_western_easter_closed_is_a_decline(db: Conn, hotel: HotelData) -> None:
    result, body, holds = drafted(db, hotel, CASES["en-western-easter-closed"])
    assert result.route == "decline"
    assert holds == []
    assert "15 April" in body  # the open season of 2027 (D62)


def test_a_window_draft_can_be_approved_and_holds_nothing(db: Conn, hotel: HotelData) -> None:
    case = CASES["en-easter-plain"]
    result, _, _ = drafted(db, hotel, case)
    approved = approve_draft(db, FixedClock(case.email.received_at + timedelta(hours=2)),
                             result.draft_id, version=1)  # fmt: skip
    assert isinstance(approved, Approved), approved
    assert db.execute(
        "SELECT (SELECT count(*) FROM bookings WHERE draft_id = %s),"
        " (SELECT count(*) FROM outbox WHERE draft_id = %s)",
        (result.draft_id, result.draft_id),
    ).fetchone() == (0, 1)


def test_window_fully_taken_declines_naming_only_that_room_type(
    db: Conn, rows: Rows, hotel: HotelData
) -> None:
    """Only the plan's type was looked at and no dates were shifted: the decline must
    not say the hotel is full, nor that a week either side was searched."""
    for room in STANDARD_ROOMS:
        rows.booking(room_id=room, stay="[2027-04-25,2027-05-12)")
    result, body, holds = drafted(db, hotel, CASES["en-easter-plain"])
    assert (result.route, holds) == ("decline", [])
    assert "not have enough Standard Double rooms free for any check-in date" in body
    assert "week" not in body
    assert "no suitable room" not in body
