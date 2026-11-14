"""The stub drafter: request -> tools -> draft with code-rendered blocks, in one
transaction (states.md: processing -> awaiting_approval / superseded / escalated).
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import TupleRow

import anemos.stub_drafter as stub_drafter
from anemos.clock import FixedClock
from anemos.inbox import lock_sender, store_email
from anemos.mailbox import incoming
from anemos.reservations.errors import ReasonCode
from anemos.reservations.hotel_data import HotelData, MinStayRule
from anemos.reservations.loader import load_hotel_data
from anemos.states import DraftStatus, TransitionRefused
from anemos.stub_drafter import Drafted, Escalated, draft_reply

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import CommittedDb, Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

RECEIVED = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)
NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
JUNE = "[2027-06-01,2027-06-04)"
SEA_VIEW = (201, 202, 203, 204)


@pytest.fixture
def hotel(db: Conn) -> HotelData:
    return load_hotel_data(db)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def with_min_stay(hotel: HotelData, nights: int, arrival: date) -> HotelData:
    """The hotel with the minimum stay of `arrival`'s rule changed (the 2027 seed has a
    1-night minimum at opening and closing, so D34's two-step path needs this)."""
    rules = tuple(
        MinStayRule(r.first_arrival, r.end, nights) if r.first_arrival <= arrival < r.end else r
        for r in hotel.min_stay_rules
    )
    return dataclasses.replace(hotel, min_stay_rules=rules)


def email_with_request(rows: Rows, *, language: str = "en", **request: Any) -> int:
    email_id = rows.email(
        received_at=RECEIVED, language=language,
        message_id=f"<{language}-{sorted(request.items())}@example.test>",
    )  # fmt: skip
    columns: dict[str, Any] = {"stay": JUNE, "adults": 2} | request
    rows.request(email_id, **columns)
    return email_id


def book(rows: Rows, rooms: Iterable[int], stay: str) -> None:
    for room in rooms:
        rows.booking(room_id=room, stay=stay)


def draft_row(db: Conn, draft_id: int) -> dict[str, Any]:
    cur = db.execute(
        "SELECT d.status, d.route, d.decided_at, e.status AS email_status, v.version,"
        " v.author, v.body, v.guardrail_results,"
        " (SELECT array_agg(o.hold ORDER BY o.quote_id) FROM draft_offers o"
        "   WHERE o.draft_id = d.id) AS holds"
        " FROM drafts d JOIN emails e ON e.id = d.email_id"
        " JOIN draft_versions v ON v.draft_id = d.id WHERE d.id = %s",
        (draft_id,),
    )
    assert cur.description is not None
    row = cur.fetchone()
    assert row is not None
    return dict(zip([c.name for c in cur.description], row, strict=True))


def email_state(db: Conn, email_id: int) -> tuple[str, list[str], int]:
    row = db.execute(
        "SELECT status, reason_codes, (SELECT count(*) FROM drafts WHERE email_id = %s)"
        " FROM emails WHERE id = %s",
        (email_id, email_id),
    ).fetchone()
    assert row is not None
    return row[0], row[1], int(row[2])


# --- quote route ----------------------------------------------------------------------


def test_exact_request_gives_a_held_quote_waiting_for_approval(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    email_id = email_with_request(rows, room_type_id="sea_view")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    assert (result.status, result.route) == (DraftStatus.PENDING_APPROVAL, "quote")
    row = draft_row(db, result.draft_id)
    assert (row["status"], row["email_status"], row["decided_at"]) == (
        "pending_approval", "awaiting_approval", None,
    )  # fmt: skip
    assert (row["version"], row["author"], row["holds"]) == (1, "agent", [True])
    assert row["guardrail_results"] == {"g1_quote_block_once": True, "g7_holds_valid": True}
    body = row["body"]
    assert "Your request: Sea View Double, 1 room" in body  # {{REQUEST_SUMMARY}} rendered
    assert "Our offer: Sea View Double, 1 room" in body  # {{QUOTE_BLOCK}} rendered
    assert "Accommodation total: €390.00" in body
    assert "{{HOLD_UNTIL}}" in body  # filled at approval (D8)
    assert "{{QUOTE_BLOCK}}" not in body
    assert "{{REQUEST_SUMMARY}}" not in body


def test_changed_offer_is_quoted_without_hold_and_with_its_note(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """Ex 4: the min-stay version; no hold (D9), the note says why."""
    email_id = email_with_request(rows, stay="[2027-08-10,2027-08-13)", room_type_id="sea_view")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    row = draft_row(db, result.draft_id)
    assert row["holds"] == [False]
    assert "the minimum stay for arrival on" in row["body"]
    assert "No room is held yet" in row["body"]
    assert "{{HOLD_UNTIL}}" not in row["body"]


def test_capacity_substitution_gets_the_room_type_note(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """Ex 8: Sea View asked for 4 adults -> Family Suite, with the D27 note."""
    email_id = email_with_request(rows, adults=4, room_type_id="sea_view")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    body = draft_row(db, result.draft_id)["body"]
    assert "the Sea View Double sleeps at most 3 people" in body
    assert "Our offer: Family Suite" in body


def test_fully_booked_gives_alternatives_never_held(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    book(rows, SEA_VIEW, JUNE)
    email_id = email_with_request(rows, room_type_id="sea_view")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    row = draft_row(db, result.draft_id)
    assert row["holds"] == [False, False]
    body = row["body"]
    assert "These are the closest options we can offer:" in body
    assert body.count("Our offer:") == 2
    assert body.count("{{QUOTE_BLOCK}}") == 0
    assert "sleeps at most" not in body  # availability, not capacity
    assert body.count("the Sea View Double is not available for these dates") == 2


def test_greek_email_gets_a_greek_draft(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    email_id = email_with_request(rows, language="el", room_type_id="sea_view")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    body = draft_row(db, result.draft_id)["body"]
    assert body.startswith("Αγαπητέ επισκέπτη,")
    assert "Σας ευχαριστούμε για το μήνυμά σας." in body
    assert "Dear guest" not in body


# --- ask_info route -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "expected"),
    [("en", "the ages of the children"), ("el", "τις ηλικίες των παιδιών")],
)
def test_missing_ages_ask_for_info(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock, language: str, expected: str
) -> None:
    email_id = email_with_request(rows, language=language, children=2, child_ages=[8])
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    assert result.route == "ask_info"
    row = draft_row(db, result.draft_id)
    assert row["holds"] is None  # no offers
    assert expected in row["body"]
    assert row["guardrail_results"]["g1_quote_block_once"] is True


# --- escalations: no draft --------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_columns", "reason"),
    [
        pytest.param({"adults": 6, "rooms": 3}, ReasonCode.GROUP, id="group"),
        pytest.param({"stay": "[2028-06-01,2028-06-04)"}, ReasonCode.NO_CALENDAR, id="no-calendar"),
    ],
)  # fmt: skip
def test_escalation_writes_no_draft(
    db: Conn,
    rows: Rows,
    hotel: HotelData,
    clock: FixedClock,
    request_columns: dict[str, Any],
    reason: ReasonCode,
) -> None:
    email_id = email_with_request(rows, **request_columns)
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Escalated)
    assert result.reason is reason
    assert email_state(db, email_id) == ("escalated", [reason.value], 0)


# --- decline route (D62) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "Our season runs from Thursday 15 April 2027 to Sunday 31 October 2027"),
        ("el", "από την Πέμπτη 15 Απριλίου 2027 έως την Κυριακή 31 Οκτωβρίου 2027"),
    ],
)
def test_closed_dates_get_a_decline_stating_the_season(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock, language: str, expected: str
) -> None:
    email_id = email_with_request(rows, language=language, stay="[2027-11-05,2027-11-08)")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    assert (result.route, result.status) == ("decline", DraftStatus.PENDING_APPROVAL)
    row = draft_row(db, result.draft_id)
    assert expected in row["body"]
    assert "{{REQUEST_SUMMARY}}" not in row["body"]
    assert row["holds"] is None
    assert row["guardrail_results"] == {"g1_quote_block_once": True, "g7_holds_valid": True}


@pytest.mark.parametrize(
    ("language", "expected"),
    [("en", "the same stay up to a week earlier or later is not available either"),
     ("el", "έως μία εβδομάδα νωρίτερα ή αργότερα δεν είναι επίσης διαθέσιμη")],
)  # fmt: skip
def test_nothing_free_gets_a_decline(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock, language: str, expected: str
) -> None:
    for rooms in (range(101, 108), range(201, 205), range(301, 304)):
        book(rows, rooms, "[2027-05-24,2027-06-12)")
    email_id = email_with_request(rows, language=language, room_type_id="standard")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    assert result.route == "decline"
    body = draft_row(db, result.draft_id)["body"]
    assert expected in body
    assert {"en": "Your request:", "el": "Το αίτημά σας:"}[language] in body


def test_min_stay_closed_still_escalates(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """MIN_STAY_CLOSED (D57) has no template yet: escalated, no draft."""
    hotel = with_min_stay(hotel, 3, date(2027, 10, 31))
    email_id = email_with_request(rows, stay="[2027-10-31,2027-11-01)", room_type_id="standard")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Escalated)
    assert result.reason is ReasonCode.MODEL
    assert email_state(db, email_id)[2] == 0


# --- notes: one per change ------------------------------------------------------------


def test_open_part_and_min_stay_give_both_notes(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    """13-17 Apr with a 3-night minimum: trimmed to 15-17 Apr, then 15-18 Apr. The
    draft shows both notes, open part first."""
    hotel = with_min_stay(hotel, 3, date(2027, 4, 15))
    email_id = email_with_request(rows, stay="[2027-04-13,2027-04-17)", room_type_id="standard")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    body = draft_row(db, result.draft_id)["body"]
    open_part = body.index("closed on some of the nights you asked for")
    min_stay = body.index("the minimum stay for arrival on Thursday 15 April 2027 is 3 nights")
    assert open_part < min_stay


def test_guardrail_failure_escalates_and_stores_no_draft(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stub_drafter, "quote_block_once", lambda text, route: "planted failure")
    email_id = email_with_request(rows, room_type_id="sea_view")
    result = draft_reply(db, hotel, clock, email_id)
    assert result == Escalated(ReasonCode.GUARDRAIL_FAIL, "planted failure")
    assert email_state(db, email_id) == ("escalated", ["GUARDRAIL_FAIL"], 0)


# --- born superseded (D48) --------------------------------------------------------------


def test_draft_is_born_superseded_when_a_newer_email_exists(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    email_id = email_with_request(rows, room_type_id="sea_view")
    rows.email(received_at=RECEIVED + timedelta(minutes=10), message_id="<newer@example.test>")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    assert result.status is DraftStatus.SUPERSEDED
    row = draft_row(db, result.draft_id)
    assert (row["status"], row["email_status"], row["decided_at"]) == (
        "superseded", "superseded", NOW,
    )  # fmt: skip
    assert row["holds"] == [True]  # kept for the audit trail, never shown for approval


def test_an_older_email_of_the_sender_does_not_supersede(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    rows.email(received_at=RECEIVED - timedelta(days=1), message_id="<older@example.test>")
    email_id = email_with_request(rows, room_type_id="sea_view")
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    assert result.status is DraftStatus.PENDING_APPROVAL


def test_another_senders_newer_email_does_not_supersede(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    email_id = email_with_request(rows, room_type_id="sea_view")
    rows.email(
        from_address="other@example.test", received_at=RECEIVED + timedelta(hours=1),
        message_id="<other@example.test>",
    )  # fmt: skip
    result = draft_reply(db, hotel, clock, email_id)
    assert isinstance(result, Drafted)
    assert result.status is DraftStatus.PENDING_APPROVAL


# --- states.md: only from received ----------------------------------------------------


def test_only_a_received_email_can_be_drafted(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock
) -> None:
    email_id = email_with_request(rows, room_type_id="sea_view")
    first = draft_reply(db, hotel, clock, email_id)
    assert isinstance(first, Drafted)
    with pytest.raises(TransitionRefused, match="'awaiting_approval'; it cannot become"):
        draft_reply(db, hotel, clock, email_id)
    assert email_state(db, email_id) == ("awaiting_approval", [], 1)


@pytest.mark.parametrize("status", ["set_aside", "processing", "closed"])
def test_other_statuses_are_refused(
    db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock, status: str
) -> None:
    email_id = rows.email(status=status, message_id=f"<{status}@example.test>")
    with pytest.raises(TransitionRefused):
        draft_reply(db, hotel, clock, email_id)


def test_everything_is_audited(db: Conn, rows: Rows, hotel: HotelData, clock: FixedClock) -> None:
    email_id = email_with_request(rows, room_type_id="sea_view")
    draft_reply(db, hotel, clock, email_id)
    events = db.execute(
        "SELECT actor, event FROM audit_log WHERE email_id = %s ORDER BY id", (email_id,)
    ).fetchall()
    assert events == [("system", "email.processing"), ("agent", "draft.created")]


# --- the D61 race, with real commits ------------------------------------------------


def test_newer_email_stored_while_drafting_makes_the_draft_born_superseded(
    committed_db: CommittedDb,
) -> None:
    """The race D61 closes: store_email holds the sender's lock; the drafter waits,
    then sees the newer email and the draft is born superseded, never left pending."""
    setup = committed_db.connect()
    rows = committed_db.rows(setup)
    hotel = load_hotel_data(setup)
    email_id = email_with_request(rows, room_type_id="sea_view")
    setup.commit()

    storer, drafter = committed_db.connect(), committed_db.connect()
    clock = FixedClock(NOW)
    lock_sender(storer, "guest@example.test")  # store_email takes this lock first

    results: list[Drafted | Escalated] = []

    def draft_and_commit() -> None:
        results.append(draft_reply(drafter, hotel, clock, email_id))
        drafter.commit()

    thread = threading.Thread(target=draft_and_commit)
    thread.start()
    committed_db.wait_until_blocked(drafter)
    newer = incoming(
        message_id="<newer@example.test>", from_header="guest@example.test",
        subject="Change", body="Actually two rooms", received_at=RECEIVED + timedelta(hours=1),
    )  # fmt: skip
    stored = store_email(storer, clock, newer)
    assert stored.superseded_draft_ids == ()  # the draft does not exist yet
    storer.commit()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert isinstance(results[0], Drafted)
    assert results[0].status is DraftStatus.SUPERSEDED
