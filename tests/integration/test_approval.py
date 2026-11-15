"""The approval transaction (D10, D44, D47, D52, D54, D63; states.md).

Drafts come from the stub drafter, so these tests exercise the same rows the
production path creates. Races with two connections are in step 13.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import TupleRow

import anemos.approval as approval
from anemos.approval import Approved, Invalidated, PriceWarning, approve_draft
from anemos.clock import FixedClock
from anemos.owner_actions import DraftExpired, Edited, edit_draft
from anemos.reservations.loader import load_hotel_data
from anemos.states import StaleVersion, TransitionRefused
from anemos.stub_drafter import Drafted, draft_reply

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

RECEIVED = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)
NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
JUNE = "[2027-06-01,2027-06-04)"
SEA_VIEW = (201, 202, 203, 204)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def drafted(
    db: Conn, rows: Rows, clock: FixedClock, *, language: str = "en", **request: Any
) -> int:
    """A draft from the stub drafter for a request (default: 2 adults, 1-4 Jun)."""
    email_id = rows.email(received_at=RECEIVED, language=language, subject="Room in June")
    columns: dict[str, Any] = {"stay": JUNE, "adults": 2} | request
    rows.request(email_id, **columns)
    result = draft_reply(db, load_hotel_data(db), clock, email_id)
    assert isinstance(result, Drafted), result
    return result.draft_id


def book(rows: Rows, rooms: Iterable[int], stay: str = JUNE, **kw: Any) -> list[int]:
    return [rows.booking(room_id=room, stay=stay, **kw) for room in rooms]


def status(db: Conn, draft_id: int) -> tuple[str, str, list[str]]:
    row = db.execute(
        "SELECT d.status, e.status, e.reason_codes FROM drafts d"
        " JOIN emails e ON e.id = d.email_id WHERE d.id = %s",
        (draft_id,),
    ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def holds_of(db: Conn, draft_id: int) -> list[tuple[int, str, datetime, int]]:
    return db.execute(
        "SELECT room_id, status, expires_at, price_cents FROM bookings"
        " WHERE draft_id = %s ORDER BY room_id",
        (draft_id,),
    ).fetchall()


def outbox_of(db: Conn, draft_id: int) -> tuple[str, str, str, str] | None:
    return db.execute(
        "SELECT to_address, subject, body, status FROM outbox WHERE draft_id = %s", (draft_id,)
    ).fetchone()


def body_of(db: Conn, draft_id: int) -> str:
    outbox = outbox_of(db, draft_id)
    assert outbox is not None, "no outbox row"
    return outbox[2]


def snapshot(db: Conn) -> dict[str, list[Any]]:
    tables = ("emails", "drafts", "draft_versions", "bookings", "outbox", "audit_log")
    query = sql.SQL("SELECT to_jsonb(t)::text AS row FROM {} t ORDER BY row")
    return {t: db.execute(query.format(sql.Identifier(t))).fetchall() for t in tables}


# --- approved -------------------------------------------------------------------------


def test_held_quote_is_held_sent_and_approved(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    result = approve_draft(db, clock, draft_id, version=1)
    assert isinstance(result, Approved)
    hold_until = NOW + timedelta(hours=48)
    assert result.hold_until == hold_until
    # D38: the lowest free Sea View; 48 h hold; the quote's accommodation total.
    assert holds_of(db, draft_id) == [(201, "hold", hold_until, 39_000)]
    outbox = outbox_of(db, draft_id)
    assert outbox is not None
    to_address, subject, body, outbox_status = outbox
    assert (to_address, subject, outbox_status) == (
        "guest@example.test",
        "Re: Room in June",
        "pending",
    )
    # {{HOLD_UNTIL}} filled in hotel time: 3 Mar 09:00 UTC = 11:00 Athens.
    assert "We are holding this room for you until Wednesday 3 March 2027" in body
    assert "{{HOLD_UNTIL}}" not in body
    assert status(db, draft_id) == ("approved", "answered", [])
    last = db.execute("SELECT actor, event FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert last == ("owner", "draft.approved")


def test_deferred_foreign_keys_hold_at_commit(db: Conn, rows: Rows, clock: FixedClock) -> None:
    """Bookings and outbox were written before the draft became approved (D10 order)."""
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    assert isinstance(approve_draft(db, clock, draft_id, version=1), Approved)
    rows.check_deferred_now("bookings_draft_approved_fkey")
    rows.check_deferred_now("outbox_draft_approved_fkey")


def test_two_rooms_take_the_two_lowest_numbers(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id = drafted(db, rows, clock, adults=4, rooms=2, room_type_id="standard")
    result = approve_draft(db, clock, draft_id, version=1)
    assert isinstance(result, Approved)
    holds = holds_of(db, draft_id)
    assert [room for room, *_ in holds] == [101, 102]
    assert sum(price for *_, price in holds) == 57_000  # 95 x 3 nights x 2 rooms
    assert "We are holding these rooms for you until" in body_of(db, draft_id)


def test_quote_without_hold_is_sent_but_nothing_is_held(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    """Ex 4, the min-stay version: hold_allowed is false (D9, cross-rule 4)."""
    draft_id = drafted(db, rows, clock, stay="[2027-08-10,2027-08-13)", room_type_id="sea_view")
    result = approve_draft(db, clock, draft_id, version=1)
    assert isinstance(result, Approved)
    assert (result.booking_ids, result.hold_until) == ((), None)
    assert holds_of(db, draft_id) == []
    assert "No room is held yet" in body_of(db, draft_id)


def test_alternatives_are_sent_and_never_held(db: Conn, rows: Rows, clock: FixedClock) -> None:
    book(rows, SEA_VIEW)
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    assert isinstance(approve_draft(db, clock, draft_id, version=1), Approved)
    assert holds_of(db, draft_id) == []


@pytest.mark.parametrize(
    "request_columns",
    [{"children": 2, "child_ages": [8]}, {"stay": "[2027-11-05,2027-11-08)"}],
    ids=["ask-info", "decline"],
)
def test_drafts_without_offers_are_sent(
    db: Conn, rows: Rows, clock: FixedClock, request_columns: dict[str, Any]
) -> None:
    draft_id = drafted(db, rows, clock, **request_columns)
    result = approve_draft(db, clock, draft_id, version=1)
    assert isinstance(result, Approved)
    assert result.booking_ids == ()
    assert outbox_of(db, draft_id) is not None


def test_the_latest_version_is_sent(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id = drafted(db, rows, clock, children=2, child_ages=[8])
    assert edit_draft(
        db, clock, draft_id, "Dear Maria, how old are the children?", base_version=1
    ) == Edited(2, None)
    assert isinstance(approve_draft(db, clock, draft_id, version=2), Approved)
    assert outbox_of(db, draft_id)[2] == "Dear Maria, how old are the children?"  # type: ignore[index]


def test_greek_hold_until(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id = drafted(db, rows, clock, language="el", room_type_id="sea_view")
    assert isinstance(approve_draft(db, clock, draft_id, version=1), Approved)
    assert "έως την Τετάρτη 3 Μαρτίου 2027" in body_of(db, draft_id)


# --- price warning (D63, D10: 422) ----------------------------------------------------


def test_edited_price_needs_confirmation(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    body = db.execute(
        "SELECT body FROM draft_versions WHERE draft_id = %s AND version = 1", (draft_id,)
    ).fetchone()
    assert body is not None
    edited = edit_draft(db, clock, draft_id, body[0].replace("€390.00", "€350.00"), base_version=1)
    assert isinstance(edited, Edited)
    assert edited.price_warning is not None

    before = snapshot(db)
    result = approve_draft(db, clock, draft_id, version=2)
    [(quote_id,)] = db.execute(
        "SELECT quote_id FROM draft_offers WHERE draft_id = %s", (draft_id,)
    ).fetchall()
    assert result == PriceWarning(edited.price_warning, version=2, quote_ids=(quote_id,))
    assert snapshot(db) == before  # nothing written

    confirmed = approve_draft(db, clock, draft_id, confirm_warnings=True, version=2)
    assert isinstance(confirmed, Approved)
    assert "€350.00" in body_of(db, draft_id)
    audit = db.execute(
        "SELECT output->>'confirmed_warning' FROM audit_log WHERE event = 'draft.approved'"
    ).fetchone()
    assert audit == ("true",)


# --- re-validation fails: invalidated (D44, D47) --------------------------------------


def assert_invalidated(db: Conn, draft_id: int, result: object, reason_part: str) -> None:
    assert isinstance(result, Invalidated), result
    assert any(reason_part in reason for reason in result.reasons), result.reasons
    assert status(db, draft_id) == ("invalidated", "escalated", ["NO_LONGER_AVAILABLE"])
    assert holds_of(db, draft_id) == []
    assert outbox_of(db, draft_id) is None
    last = db.execute("SELECT actor, event FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert last == ("owner", "draft.invalidated")


def test_price_changed_since_the_quote(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    db.execute(
        "UPDATE room_rates SET rate_cents = 13500 WHERE room_type_id = 'sea_view'"
        " AND season_id = 'mid'"
    )
    result = approve_draft(db, clock, draft_id, version=1)
    assert_invalidated(db, draft_id, result, "price changed")


def test_room_taken_since_the_quote(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    book(rows, SEA_VIEW)
    assert_invalidated(
        db, draft_id, approve_draft(db, clock, draft_id, version=1), "0 of 1 room(s) free"
    )


def test_one_room_taken_another_is_held(db: Conn, rows: Rows, clock: FixedClock) -> None:
    """D47 checks the offer (type, stay, rooms), not a room number: the next free room."""
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    book(rows, [201])
    assert isinstance(approve_draft(db, clock, draft_id, version=1), Approved)
    assert [room for room, *_ in holds_of(db, draft_id)] == [202]


def test_an_alternative_no_longer_free_invalidates_the_draft(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    """D47: every offered option, held or not, must still be available."""
    book(rows, SEA_VIEW)
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")  # Standard + Family Suite
    book(rows, (301, 302, 303))
    assert_invalidated(db, draft_id, approve_draft(db, clock, draft_id, version=1), "family_suite")


def test_exclusion_violation_in_the_savepoint_invalidates(
    db: Conn, rows: Rows, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D52, without a second connection: re-validation is made to believe room 201 is
    free while a confirmed booking holds it; the hold insert hits the exclusion
    constraint inside the savepoint, and the transaction carries on to invalidate."""
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    book(rows, [201])
    monkeypatch.setattr(approval, "rooms_for_plan", lambda *args: (201,))
    result = approve_draft(db, clock, draft_id, version=1)
    assert_invalidated(db, draft_id, result, "taken by another approval")


# --- stale holds (D10, D22) -----------------------------------------------------------


def test_stale_hold_is_flipped_and_its_room_held_again(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    """Room 201 is held by a hold that expired an hour ago; the other Sea Views are
    booked. The quote saw 201 as free (D22); approval flips the old hold, then holds 201."""
    [stale] = book(rows, [201], status="hold", expires_at=NOW - timedelta(hours=1))
    book(rows, (202, 203, 204))
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    assert isinstance(approve_draft(db, clock, draft_id, version=1), Approved)
    old = db.execute("SELECT status FROM bookings WHERE id = %s", (stale,)).fetchone()
    assert old == ("expired",)
    assert [room for room, *_ in holds_of(db, draft_id)] == [201]
    flips = db.execute(
        "SELECT actor, output->>'booking_id' FROM audit_log WHERE event = 'hold.expired'"
    ).fetchall()
    assert flips == [("system", str(stale))]


def test_a_live_hold_is_not_flipped(db: Conn, rows: Rows, clock: FixedClock) -> None:
    [live] = book(rows, [201], status="hold", expires_at=NOW + timedelta(minutes=1))
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    assert isinstance(approve_draft(db, clock, draft_id, version=1), Approved)
    assert db.execute("SELECT status FROM bookings WHERE id = %s", (live,)).fetchone() == ("hold",)
    assert [room for room, *_ in holds_of(db, draft_id)] == [202]


# --- refused ----------------------------------------------------------------------------


def test_double_approval_is_refused(db: Conn, rows: Rows, clock: FixedClock) -> None:
    """D10: 409. The second call changes nothing."""
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    assert isinstance(approve_draft(db, clock, draft_id, version=1), Approved)
    before = snapshot(db)
    with pytest.raises(TransitionRefused) as refused:
        approve_draft(db, clock, draft_id, version=1)
    assert refused.value.current == "approved"
    assert snapshot(db) == before


def test_approval_after_72_hours_expires_the_draft(db: Conn, rows: Rows, clock: FixedClock) -> None:
    """D54: flipped to expired, nothing held or sent; the caller commits the flip."""
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    late = FixedClock(NOW + timedelta(hours=72, seconds=1))
    assert approve_draft(db, late, draft_id, version=1) == DraftExpired(draft_id)
    assert status(db, draft_id)[:2] == ("expired", "expired")
    assert holds_of(db, draft_id) == []
    assert outbox_of(db, draft_id) is None
    events = db.execute("SELECT actor, event FROM audit_log ORDER BY id DESC LIMIT 2").fetchall()
    assert events == [("owner", "draft.action_refused"), ("system", "draft.expired")]


def test_hold_until_placeholder_without_a_hold_is_a_bug(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    """A text promising a hold that does not exist is never sent."""
    draft_id = drafted(db, rows, clock, children=2, child_ages=[8])
    edit_draft(db, clock, draft_id, "We hold a room for you until {{HOLD_UNTIL}}.", base_version=1)
    with pytest.raises(ValueError, match="nothing is held"):
        approve_draft(db, clock, draft_id, version=2)


# --- the version the owner saw (D67) ----------------------------------------------------


def latest_body(db: Conn, draft_id: int) -> str:
    row = db.execute(
        "SELECT body FROM draft_versions WHERE draft_id = %s ORDER BY version DESC LIMIT 1",
        (draft_id,),
    ).fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.parametrize("seen", [1, 3], ids=["older", "newer-than-latest"])
def test_approving_a_version_that_is_not_the_latest_is_refused(
    db: Conn, rows: Rows, clock: FixedClock, seen: int
) -> None:
    """The phone shows v1, the laptop saved v2: approving v1 would send v2 unseen."""
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    friendlier = latest_body(db, draft_id).replace("Dear", "Hello")
    edit_draft(db, clock, draft_id, friendlier, base_version=1)
    before = snapshot(db)
    with pytest.raises(StaleVersion) as stale:
        approve_draft(db, clock, draft_id, version=seen)
    assert (stale.value.seen, stale.value.latest, stale.value.action) == (seen, 2, "approve")
    assert snapshot(db) == before  # nothing held, sent or flipped
    assert isinstance(approve_draft(db, clock, draft_id, version=2), Approved)


def test_stale_version_comes_before_the_price_warning(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    """D67 order: the owner has not seen v2, so its warning is not theirs to confirm."""
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    cheaper = latest_body(db, draft_id).replace("€390.00", "€350.00")
    edited = edit_draft(db, clock, draft_id, cheaper, base_version=1)
    assert isinstance(edited, Edited)
    assert edited.price_warning is not None
    with pytest.raises(StaleVersion):
        approve_draft(db, clock, draft_id, version=1, confirm_warnings=True)


def test_expiry_comes_before_the_version_check(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    edit_draft(db, clock, draft_id, "Dear guest, edited.", base_version=1)
    late = FixedClock(NOW + timedelta(hours=72, seconds=1))
    assert approve_draft(db, late, draft_id, version=1) == DraftExpired(draft_id)


def test_a_refused_transition_comes_before_the_version_check(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    draft_id = drafted(db, rows, clock, room_type_id="sea_view")
    assert isinstance(approve_draft(db, clock, draft_id, version=1), Approved)
    with pytest.raises(TransitionRefused):
        approve_draft(db, clock, draft_id, version=7)


# --- arrival already passed (D64) -----------------------------------------------------

# Arrival 1 Jun 2027; check-in moment 15:00 Athens = 12:00 UTC (summer, UTC+3).
CHECK_IN = datetime(2027, 6, 1, 12, 0, tzinfo=UTC)


def test_offer_whose_check_in_has_passed_is_invalidated(db: Conn, rows: Rows) -> None:
    """Email 50 h before arrival (not SHORT_NOTICE), drafted an hour later, approved 60 h
    after the email, i.e. 10 h after check-in: invalidated, NO_LONGER_AVAILABLE."""
    received = CHECK_IN - timedelta(hours=50)
    email_id = rows.email(received_at=received, subject="Room tomorrow")
    rows.request(email_id, stay=JUNE, adults=2, room_type_id="sea_view")
    drafting = FixedClock(received + timedelta(hours=1))
    result = draft_reply(db, load_hotel_data(db), drafting, email_id)
    assert isinstance(result, Drafted)
    approving = FixedClock(received + timedelta(hours=60))
    outcome = approve_draft(db, approving, result.draft_id, version=1)
    assert_invalidated(db, result.draft_id, outcome, "check-in on 2027-06-01 at 15:00 has passed")


def test_approval_exactly_at_the_check_in_moment_is_still_valid(db: Conn, rows: Rows) -> None:
    """D64 says "before the approval time": equal is not before."""
    received = CHECK_IN - timedelta(hours=50)
    email_id = rows.email(received_at=received, subject="Room tomorrow")
    rows.request(email_id, stay=JUNE, adults=2, room_type_id="sea_view")
    result = draft_reply(
        db, load_hotel_data(db), FixedClock(received + timedelta(hours=1)), email_id
    )
    assert isinstance(result, Drafted)
    assert isinstance(approve_draft(db, FixedClock(CHECK_IN), result.draft_id, version=1), Approved)
