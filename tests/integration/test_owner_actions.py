"""Owner actions other than approve, against docs/states.md.

Every transition states.md allows for these actions has a test; every other
(action, status) pair is refused with TransitionRefused and changes nothing
(`test_unlisted_transitions_are_refused_and_change_nothing`, generated from
anemos.states so a new status is covered automatically).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import TupleRow

from anemos.clock import FixedClock
from anemos.inbox import lock_sender, store_email
from anemos.mailbox import incoming
from anemos.owner_actions import (
    DraftExpired,
    Edited,
    cancel_booking,
    close_email,
    confirm_hold,
    edit_draft,
    expire_draft_if_due,
    reject_draft,
    release_hold,
    reopen_email,
)
from anemos.reservations.loader import load_hotel_data
from anemos.states import (
    BookingStatus,
    DraftStatus,
    EmailStatus,
    StaleVersion,
    TransitionRefused,
)
from anemos.stub_drafter import Drafted, draft_reply

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import CommittedDb, Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

T0 = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)  # = Rows.T0
LATER = T0 + timedelta(hours=1)
PAST_72H = T0 + timedelta(hours=72, seconds=1)

# A decided draft and the email status states.md pairs it with.
DECIDED = {
    DraftStatus.APPROVED: (EmailStatus.ANSWERED, []),
    DraftStatus.REJECTED: (EmailStatus.CLOSED, []),
    DraftStatus.EXPIRED: (EmailStatus.EXPIRED, []),
    DraftStatus.SUPERSEDED: (EmailStatus.SUPERSEDED, []),
    DraftStatus.INVALIDATED: (EmailStatus.ESCALATED, ["NO_LONGER_AVAILABLE"]),
}


def pending_draft(rows: Rows, *, created_at: datetime = T0) -> tuple[int, int]:
    """(email id, draft id): awaiting approval, version 1 by the agent."""
    email_id = rows.email(status="awaiting_approval", message_id=f"<p-{created_at}@x.test>")
    draft_id = rows.draft(email_id, None, route="policy", created_at=created_at)
    rows.version(draft_id, 1, created_at=created_at)
    return email_id, draft_id


def email_in(rows: Rows, status: str) -> int:
    reasons = ["COMPLAINT"] if status == EmailStatus.ESCALATED else []
    return rows.email(status=status, reason_codes=reasons, message_id=f"<{status}@x.test>")


def booking_in(rows: Rows, status: str) -> int:
    expires_at = T0 + timedelta(hours=48) if status == BookingStatus.HOLD else None
    return rows.booking(status=status, expires_at=expires_at)


def state(db: Conn, draft_id: int) -> tuple[str, str, Any, int]:
    row = db.execute(
        "SELECT d.status, e.status, d.decided_at,"
        " (SELECT count(*) FROM draft_versions v WHERE v.draft_id = d.id)"
        " FROM drafts d JOIN emails e ON e.id = d.email_id WHERE d.id = %s",
        (draft_id,),
    ).fetchone()
    assert row is not None
    return row[0], row[1], row[2], int(row[3])


def events(db: Conn) -> list[tuple[str, str]]:
    return db.execute("SELECT actor, event FROM audit_log ORDER BY id").fetchall()


def snapshot(db: Conn) -> dict[str, list[Any]]:
    """Every row the actions could touch, for "nothing changed" checks."""
    tables = ("emails", "drafts", "draft_versions", "bookings", "audit_log")
    query = sql.SQL("SELECT to_jsonb(t)::text AS row FROM {} t ORDER BY row")
    return {t: db.execute(query.format(sql.Identifier(t))).fetchall() for t in tables}


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(LATER)


# --- edit -----------------------------------------------------------------------------


def test_edit_adds_an_owner_version_and_keeps_the_original(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    _, draft_id = pending_draft(rows)
    assert edit_draft(db, clock, draft_id, "Dear guest, edited once.", base_version=1) == Edited(
        2, None
    )
    assert edit_draft(db, clock, draft_id, "Dear guest, edited twice.", base_version=2) == Edited(
        3, None
    )
    versions = db.execute(
        "SELECT version, author, body FROM draft_versions WHERE draft_id = %s ORDER BY version",
        (draft_id,),
    ).fetchall()
    assert [(v, a) for v, a, _ in versions] == [(1, "agent"), (2, "owner"), (3, "owner")]
    assert versions[2][2] == "Dear guest, edited twice."
    assert state(db, draft_id)[:3] == ("pending_approval", "awaiting_approval", None)
    assert events(db)[-1] == ("owner", "draft.edited")


@pytest.mark.parametrize("blank", ["", "  \n\t"])
def test_blank_edit_is_refused_before_writing(
    db: Conn, rows: Rows, clock: FixedClock, blank: str
) -> None:
    _, draft_id = pending_draft(rows)
    before = snapshot(db)
    with pytest.raises(ValueError, match="blank"):
        edit_draft(db, clock, draft_id, blank, base_version=1)
    assert snapshot(db) == before


# --- price check on edits (D63) -----------------------------------------------------


def quoted_draft(rows: Rows) -> tuple[int, str]:
    """(draft id, the rendered block of its one offer); version 1 shows the block."""
    email_id = rows.email(status="awaiting_approval", message_id="<quoted@x.test>")
    request_id = rows.request(email_id)
    draft_id = rows.draft(email_id, request_id)
    quote_id = rows.quote(request_id)
    rows.offer(draft_id, request_id, quote_id, hold=True)
    block = db_block(rows, draft_id)
    rows.version(draft_id, 1, body=f"Dear guest,\n\n{block}\n\nKind regards")
    return draft_id, block


def db_block(rows: Rows, draft_id: int) -> str:
    row = rows.db.execute(
        "SELECT rendered_block FROM draft_offers WHERE draft_id = %s", (draft_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


def price_check(db: Conn, draft_id: int, version: int) -> object:
    row = db.execute(
        "SELECT guardrail_results->'price_check' FROM draft_versions"
        " WHERE draft_id = %s AND version = %s",
        (draft_id, version),
    ).fetchone()
    assert row is not None
    return row[0]


def test_edit_keeping_the_block_verbatim_passes(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id, block = quoted_draft(rows)
    edited = f"Dear Maria,\n\nLovely to hear from you again!\n\n{block}\n\nSee you soon"
    assert edit_draft(db, clock, draft_id, edited, base_version=1) == Edited(2, None)
    assert price_check(db, draft_id, 2) is True


def test_edit_changing_a_price_warns(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id, block = quoted_draft(rows)
    cheaper = block.replace("€390.00", "€350.00")
    result = edit_draft(db, clock, draft_id, f"Dear guest,\n\n{cheaper}", base_version=1)
    assert isinstance(result, Edited)
    assert result.price_warning == "edited-price warning: quote block 1 of 1 changed or removed"
    assert price_check(db, draft_id, 2) == result.price_warning
    assert state(db, draft_id)[:2] == ("pending_approval", "awaiting_approval")  # stored


def test_edit_deleting_the_block_warns(db: Conn, rows: Rows, clock: FixedClock) -> None:
    draft_id, _ = quoted_draft(rows)
    result = edit_draft(db, clock, draft_id, "Dear guest, call us for a price.", base_version=1)
    assert isinstance(result, Edited)
    assert result.price_warning is not None
    assert "changed or removed" in result.price_warning


def test_price_check_against_a_real_stub_draft(db: Conn, rows: Rows, clock: FixedClock) -> None:
    """End to end: the stub stores each rendered block; editing a number in the
    owner's copy of version 1 warns, editing only the greeting does not."""
    email_id = rows.email(received_at=T0 - timedelta(hours=1), message_id="<stub@x.test>")
    rows.request(email_id, stay="[2027-06-01,2027-06-04)", adults=2, room_type_id="sea_view")
    drafted = draft_reply(db, load_hotel_data(db), clock, email_id)
    assert isinstance(drafted, Drafted)
    body = db.execute(
        "SELECT body FROM draft_versions WHERE draft_id = %s AND version = 1",
        (drafted.draft_id,),
    ).fetchone()
    assert body is not None
    friendlier = edit_draft(
        db, clock, drafted.draft_id, body[0].replace("Dear guest,", "Hi,"), base_version=1
    )
    assert isinstance(friendlier, Edited)
    assert friendlier.price_warning is None
    cheaper = edit_draft(
        db, clock, drafted.draft_id, body[0].replace("€390.00", "€350.00"), base_version=2
    )
    assert isinstance(cheaper, Edited)
    assert cheaper.price_warning is not None


@pytest.mark.parametrize("base", [1, 3], ids=["older", "newer-than-latest"])
def test_an_edit_from_an_old_version_is_refused(
    db: Conn, rows: Rows, clock: FixedClock, base: int
) -> None:
    """D67: editing from v1 after v2 exists would silently drop v2. Nothing written."""
    _, draft_id = pending_draft(rows)
    assert edit_draft(db, clock, draft_id, "Dear guest, v2.", base_version=1) == Edited(2, None)
    before = snapshot(db)
    with pytest.raises(StaleVersion) as stale:
        edit_draft(db, clock, draft_id, "Dear guest, from v1.", base_version=base)
    assert (stale.value.seen, stale.value.latest, stale.value.action) == (base, 2, "edit")
    assert stale.value.audit_output() == {
        "action": "edit", "reason": "stale_version", "seen": base, "latest": 2,
    }  # fmt: skip
    assert snapshot(db) == before


def test_an_expired_draft_is_expired_before_the_version_check(db: Conn, rows: Rows) -> None:
    _, draft_id = pending_draft(rows)
    late = FixedClock(PAST_72H)
    assert edit_draft(db, late, draft_id, "Too late.", base_version=9) == DraftExpired(draft_id)


# --- reject -----------------------------------------------------------------------------


def test_reject_closes_the_email(db: Conn, rows: Rows, clock: FixedClock) -> None:
    _, draft_id = pending_draft(rows)
    assert reject_draft(db, clock, draft_id) is None
    assert state(db, draft_id) == ("rejected", "closed", LATER, 1)
    assert events(db)[-1] == ("owner", "draft.rejected")


# --- 72 hours ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [lambda db, clock, d: edit_draft(db, clock, d, "Too late.", base_version=1), reject_draft],
    ids=["edit", "reject"],
)
def test_action_on_a_draft_past_72_hours_expires_it_instead(
    db: Conn, rows: Rows, action: Callable[[Conn, FixedClock, int], object]
) -> None:
    _, draft_id = pending_draft(rows)
    clock = FixedClock(PAST_72H)
    assert action(db, clock, draft_id) == DraftExpired(draft_id)
    assert state(db, draft_id) == ("expired", "expired", PAST_72H, 1)  # no version added
    # Time flipped it (system); the owner's attempt is recorded separately.
    assert events(db)[-2:] == [("system", "draft.expired"), ("owner", "draft.action_refused")]
    refused = db.execute(
        "SELECT output FROM audit_log WHERE event = 'draft.action_refused'"
    ).fetchone()
    assert refused is not None
    assert refused[0]["reason"] == "expired"
    assert refused[0]["action"] in ("edit", "reject")


def test_exactly_72_hours_is_not_yet_expired(db: Conn, rows: Rows) -> None:
    """states.md: "more than 72 hours old"."""
    _, draft_id = pending_draft(rows)
    clock = FixedClock(T0 + timedelta(hours=72))
    assert reject_draft(db, clock, draft_id) is None
    assert state(db, draft_id)[0] == "rejected"


def test_expire_draft_if_due(db: Conn, rows: Rows) -> None:
    _, draft_id = pending_draft(rows)
    assert expire_draft_if_due(db, FixedClock(LATER), draft_id) is False
    assert state(db, draft_id)[0] == "pending_approval"
    assert expire_draft_if_due(db, FixedClock(PAST_72H), draft_id) is True
    assert state(db, draft_id)[:2] == ("expired", "expired")
    assert expire_draft_if_due(db, FixedClock(PAST_72H), draft_id) is False  # already final


def test_expiry_flip_survives_the_refused_action(committed_db: CommittedDb) -> None:
    """D54: the refusal is a return value, so the caller commits the flip."""
    conn = committed_db.connect()
    _, draft_id = pending_draft(committed_db.rows(conn))
    conn.commit()
    assert reject_draft(conn, FixedClock(PAST_72H), draft_id) == DraftExpired(draft_id)
    conn.commit()
    reader = committed_db.connect()
    assert state(reader, draft_id)[:2] == ("expired", "expired")


# --- emails -----------------------------------------------------------------------------


@pytest.mark.parametrize("status", [EmailStatus.ESCALATED, EmailStatus.EXPIRED])
def test_owner_closes_an_escalated_or_expired_email(
    db: Conn, rows: Rows, clock: FixedClock, status: EmailStatus
) -> None:
    email_id = email_in(rows, status)
    close_email(db, clock, email_id)
    row = db.execute("SELECT status, reason_codes FROM emails WHERE id = %s", (email_id,))
    expected_reasons = ["COMPLAINT"] if status is EmailStatus.ESCALATED else []
    assert row.fetchone() == ("closed", expected_reasons)  # reasons kept
    assert events(db)[-1] == ("owner", "email.closed")


def test_not_spam_reopens_a_set_aside_email_as_escalated(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    email_id = email_in(rows, EmailStatus.SET_ASIDE)
    reopen_email(db, clock, email_id)
    row = db.execute("SELECT status, reason_codes FROM emails WHERE id = %s", (email_id,))
    assert row.fetchone() == ("escalated", ["OWNER_REOPENED"])
    assert events(db)[-1] == ("owner", "email.reopened")


# --- bookings ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "start", "end", "event"),
    [
        (confirm_hold, "hold", "confirmed", "hold.confirmed"),
        (release_hold, "hold", "cancelled", "hold.released"),
        (cancel_booking, "confirmed", "cancelled", "booking.cancelled"),
    ],
    ids=["deposit-received", "release", "cancel-confirmed"],
)
def test_holds_page_actions(
    db: Conn,
    rows: Rows,
    clock: FixedClock,
    action: Callable[[Conn, FixedClock, int], None],
    start: str,
    end: str,
    event: str,
) -> None:
    booking_id = booking_in(rows, start)
    action(db, clock, booking_id)
    assert db.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,)).fetchone() == (
        end,
    )
    assert events(db)[-1] == ("owner", event)


def test_a_hold_past_expires_at_can_still_be_confirmed(db: Conn, rows: Rows) -> None:
    """states.md: allowed while the status is still `hold`; the constraint kept the room."""
    booking_id = booking_in(rows, "hold")  # expires T0 + 48 h
    confirm_hold(db, FixedClock(T0 + timedelta(days=5)), booking_id)
    status = db.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,)).fetchone()
    assert status == ("confirmed",)


# --- every unlisted transition is refused and changes nothing ---------------------


def _cases() -> list[Any]:
    cases: list[Any] = []
    draft_actions: dict[str, Callable[[Conn, FixedClock, int], object]] = {
        "edit": lambda db, clock, d: edit_draft(db, clock, d, "Changed.", base_version=1),
        "reject": reject_draft,
    }
    for name, action in draft_actions.items():
        for draft_status in DECIDED:
            cases.append(
                pytest.param("draft", draft_status, action, id=f"{name}-draft-{draft_status}")
            )
    email_actions: dict[str, tuple[Callable[[Conn, FixedClock, int], None], set[EmailStatus]]] = {
        "close": (close_email, {EmailStatus.ESCALATED, EmailStatus.EXPIRED}),
        "reopen": (reopen_email, {EmailStatus.SET_ASIDE}),
    }
    for name, (email_action, allowed) in email_actions.items():
        for email_status in sorted(set(EmailStatus) - allowed):
            cases.append(
                pytest.param("email", email_status, email_action, id=f"{name}-email-{email_status}")
            )
    booking_actions: dict[str, tuple[Callable[[Conn, FixedClock, int], None], BookingStatus]] = {
        "confirm_hold": (confirm_hold, BookingStatus.HOLD),
        "release_hold": (release_hold, BookingStatus.HOLD),
        "cancel_booking": (cancel_booking, BookingStatus.CONFIRMED),
    }
    for name, (booking_action, from_status) in booking_actions.items():
        for booking_status in sorted(set(BookingStatus) - {from_status}):
            cases.append(
                pytest.param(
                    "booking",
                    booking_status,
                    booking_action,
                    id=f"{name}-booking-{booking_status}",
                )
            )
    return cases


@pytest.mark.parametrize(("kind", "status", "action"), _cases())
def test_unlisted_transitions_are_refused_and_change_nothing(
    db: Conn,
    rows: Rows,
    clock: FixedClock,
    kind: str,
    status: str,
    action: Callable[[Conn, FixedClock, int], object],
) -> None:
    if kind == "draft":
        email_status, reasons = DECIDED[DraftStatus(status)]
        email_id = rows.email(status=email_status, reason_codes=reasons)
        target = rows.draft(email_id, None, route="policy", status=status, decided_at=T0)
    elif kind == "email":
        target = email_in(rows, status)
    else:
        target = booking_in(rows, status)
    before = snapshot(db)
    with pytest.raises(TransitionRefused):
        action(db, clock, target)
    assert snapshot(db) == before


def test_refusal_on_a_pending_draft_past_72h_still_refuses_unlisted_actions(
    db: Conn, rows: Rows
) -> None:
    """Status is checked first: an already-decided draft is refused, not 'expired'."""
    email_id = rows.email(status="closed")
    draft_id = rows.draft(email_id, None, route="policy", status="rejected", decided_at=T0)
    with pytest.raises(TransitionRefused):
        reject_draft(db, FixedClock(PAST_72H), draft_id)


# --- serialised with superseding (D61) ----------------------------------------------


def test_reject_waits_for_the_sender_lock_then_sees_the_supersede(
    committed_db: CommittedDb,
) -> None:
    """While store_email holds the sender lock and supersedes the draft, a reject
    waits; afterwards the draft is superseded, so the reject is refused."""
    setup = committed_db.connect()
    _, draft_id = pending_draft(committed_db.rows(setup))
    setup.commit()

    storer, owner = committed_db.connect(), committed_db.connect()
    lock_sender(storer, "guest@example.test")
    outcome: list[object] = []

    def reject() -> None:
        try:
            outcome.append(reject_draft(owner, committed_db.clock, draft_id))
        except TransitionRefused as refused:
            outcome.append(refused)
        owner.rollback()

    thread = threading.Thread(target=reject)
    thread.start()
    committed_db.wait_until_blocked(owner)
    newer = incoming(
        message_id="<newer@x.test>", from_header="guest@example.test", subject="s",
        body="b", received_at=T0 + timedelta(hours=2),
    )  # fmt: skip
    assert store_email(storer, committed_db.clock, newer).superseded_draft_ids == (draft_id,)
    storer.commit()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert isinstance(outcome[0], TransitionRefused)
    assert outcome[0].current == "superseded"
