"""Approval races with real commits and two connections (step 13; D10, D52, D54, D61).

Pattern: connection A runs its approval and does not commit yet; connection B starts
in a thread and blocks; `wait_until_blocked` proves B is waiting (not guessed with a
sleep); then A commits and B's outcome is checked. The 2-room test also runs both
approvals truly at once, released together by a barrier.

Run repeatedly: uv run --env-file .env pytest tests/integration/test_approval_races.py --count=20
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.approval import Approved, Invalidated, approve_draft
from anemos.clock import FixedClock
from anemos.owner_actions import DraftExpired
from anemos.reservations.loader import load_hotel_data
from anemos.states import TransitionRefused
from anemos.stub_drafter import Drafted, draft_reply

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

RECEIVED = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)
NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
JUNE = "[2027-06-01,2027-06-04)"
SEA_VIEW = (201, 202, 203, 204)
STANDARD = (101, 102, 103, 104, 105, 106)


class Outcome:
    """Result or exception of a call made in another thread."""

    def __init__(self) -> None:
        self.value: object = None
        self.error: BaseException | None = None


def in_thread(work: Callable[[], object]) -> tuple[threading.Thread, Outcome]:
    outcome = Outcome()

    def run() -> None:
        try:
            outcome.value = work()
        except BaseException as error:  # handed to the test thread, which asserts on it
            outcome.error = error

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def committed_draft(committed_db: CommittedDb, sender: str, **request: Any) -> int:
    """A stub draft, committed, for `sender` (a different sender = a different D61 lock)."""
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    email_id = rows.email(
        from_address=sender, received_at=RECEIVED, message_id=f"<{sender}-{request}>"
    )
    columns: dict[str, Any] = {"stay": JUNE, "adults": 2} | request
    rows.request(email_id, **columns)
    result = draft_reply(conn, load_hotel_data(conn), FixedClock(NOW), email_id)
    assert isinstance(result, Drafted), result
    conn.commit()
    return result.draft_id


def book(committed_db: CommittedDb, rooms: tuple[int, ...], stay: str = JUNE) -> None:
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    for room in rooms:
        rows.booking(room_id=room, stay=stay)
    conn.commit()


def approve_and_commit(conn: Conn, draft_id: int) -> object:
    """What the API will do: approve, commit any outcome; roll back on refusal."""
    try:
        result = approve_draft(conn, FixedClock(NOW), draft_id)
    except TransitionRefused:
        conn.rollback()
        raise
    conn.commit()
    return result


def counts(conn: Conn) -> dict[str, Any]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM outbox),"
        " (SELECT array_agg(room_id ORDER BY room_id) FROM bookings WHERE status = 'hold'),"
        " (SELECT array_agg(status ORDER BY id) FROM drafts)"
    ).fetchone()
    assert row is not None
    conn.commit()
    return {"outbox": row[0], "held_rooms": row[1], "drafts": row[2]}


# --- 1. double approval ---------------------------------------------------------------


def test_double_approval_sends_once(committed_db: CommittedDb) -> None:
    """A approves and has not committed; B's approval of the same draft waits on the
    sender lock (D61); after A commits, B sees `approved` and is refused (409)."""
    draft_id = committed_draft(committed_db, "guest@example.test", room_type_id="sea_view")
    first, second = committed_db.connect(), committed_db.connect()

    assert isinstance(approve_draft(first, FixedClock(NOW), draft_id), Approved)
    thread, outcome = in_thread(lambda: approve_and_commit(second, draft_id))
    committed_db.wait_until_blocked(second)
    first.commit()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert isinstance(outcome.error, TransitionRefused)
    assert outcome.error.current == "approved"
    assert counts(committed_db.connect()) == {
        "outbox": 1, "held_rooms": [201], "drafts": ["approved"],
    }  # fmt: skip


# --- 2. two drafts, the last room -------------------------------------------------------


def test_two_approvals_competing_for_the_last_room(committed_db: CommittedDb) -> None:
    """D52: both drafts offer the last Sea View (204). A holds it but has not committed.
    B's re-validation cannot see A's uncommitted hold, so B passes it; B then waits on
    the room lock for 204, its insert fails once A commits (exclusion constraint), and
    the savepoint turns the failure into an invalidation instead of a crash."""
    book(committed_db, (201, 202, 203))
    draft_a = committed_draft(committed_db, "anna@example.test", room_type_id="sea_view")
    draft_b = committed_draft(committed_db, "ben@example.test", room_type_id="sea_view")
    conn_a, conn_b = committed_db.connect(), committed_db.connect()

    assert isinstance(approve_draft(conn_a, FixedClock(NOW), draft_a), Approved)
    thread, outcome = in_thread(lambda: approve_and_commit(conn_b, draft_b))
    committed_db.wait_until_blocked(conn_b)  # blocked on the room lock for 204
    conn_a.commit()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert outcome.error is None
    assert isinstance(outcome.value, Invalidated)
    assert outcome.value.reasons == (
        f"quote {quote_of(committed_db, draft_b)}: a room was taken by another approval",
    )
    reader = committed_db.connect()
    assert counts(reader) == {
        "outbox": 1, "held_rooms": [204], "drafts": ["approved", "invalidated"],
    }  # fmt: skip
    reasons = reader.execute(
        "SELECT e.reason_codes FROM emails e JOIN drafts d ON d.email_id = e.id WHERE d.id = %s",
        (draft_b,),
    ).fetchone()
    assert reasons == (["NO_LONGER_AVAILABLE"],)


def quote_of(committed_db: CommittedDb, draft_id: int) -> int:
    row = (
        committed_db.connect()
        .execute("SELECT quote_id FROM draft_offers WHERE draft_id = %s", (draft_id,))
        .fetchone()
    )
    assert row is not None
    return int(row[0])


# --- 3. approval after 72 hours ---------------------------------------------------------


def test_approval_after_72_hours_commits_the_expiry(committed_db: CommittedDb) -> None:
    """D54: refused, but the flip is committed and seen by another connection."""
    draft_id = committed_draft(committed_db, "guest@example.test", room_type_id="sea_view")
    conn = committed_db.connect()
    late = FixedClock(NOW + timedelta(hours=72, seconds=1))
    assert approve_draft(conn, late, draft_id) == DraftExpired(draft_id)
    conn.commit()
    assert counts(committed_db.connect()) == {
        "outbox": 0, "held_rooms": None, "drafts": ["expired"],
    }  # fmt: skip


# --- 4. a room taken in between ---------------------------------------------------------


def test_last_room_taken_in_between_invalidates(committed_db: CommittedDb) -> None:
    """Another connection commits a booking on the only free Sea View after the draft
    was written; approval re-validates (D47) and invalidates."""
    book(committed_db, (201, 202, 203))
    draft_id = committed_draft(committed_db, "guest@example.test", room_type_id="sea_view")
    book(committed_db, (204,))
    result = approve_and_commit(committed_db.connect(), draft_id)
    assert isinstance(result, Invalidated)
    assert counts(committed_db.connect())["drafts"] == ["invalidated"]


def test_one_room_taken_in_between_holds_the_next(committed_db: CommittedDb) -> None:
    draft_id = committed_draft(committed_db, "guest@example.test", room_type_id="sea_view")
    book(committed_db, (201,))
    result = approve_and_commit(committed_db.connect(), draft_id)
    assert isinstance(result, Approved)
    assert counts(committed_db.connect())["held_rooms"] == [202]


# --- 5. two drafts that each need two rooms -------------------------------------------


@pytest.fixture
def two_room_drafts(committed_db: CommittedDb) -> tuple[int, int]:
    """Standard 104-106 booked, so 101-103 are free: room for one 2-room hold only."""
    book(committed_db, STANDARD[3:])
    a = committed_draft(
        committed_db, "anna@example.test", adults=4, rooms=2, room_type_id="standard"
    )
    b = committed_draft(
        committed_db, "ben@example.test", adults=4, rooms=2, room_type_id="standard"
    )
    return a, b


def test_two_room_approvals_take_rooms_in_one_order(
    committed_db: CommittedDb, two_room_drafts: tuple[int, int]
) -> None:
    """Deterministic: A holds 101 and 102 uncommitted. B, also after (101, 102), blocks
    on the lock of its first room, 101. Rooms are locked ascending, so B never holds
    102 while waiting for 101 (which, against A taking 101 then 102, would deadlock)."""
    draft_a, draft_b = two_room_drafts
    conn_a, conn_b = committed_db.connect(), committed_db.connect()
    assert isinstance(approve_draft(conn_a, FixedClock(NOW), draft_a), Approved)
    thread, outcome = in_thread(lambda: approve_and_commit(conn_b, draft_b))
    committed_db.wait_until_blocked(conn_b)
    blocked_on = (
        committed_db.connect()
        .execute(
            "SELECT count(*) FROM pg_locks WHERE pid = %s AND NOT granted",
            (conn_b.info.backend_pid,),
        )
        .fetchone()
    )
    assert blocked_on == (1,)  # waiting on exactly one lock: A's first room
    conn_a.commit()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert isinstance(outcome.value, Invalidated)
    assert counts(committed_db.connect())["held_rooms"] == [101, 102]


def test_two_room_approvals_at_the_same_moment_never_deadlock(
    committed_db: CommittedDb, two_room_drafts: tuple[int, int]
) -> None:
    """Both approvals released together. Whichever wins holds 101 and 102; the other is
    invalidated (by re-validation or the exclusion constraint). No DeadlockDetected,
    no exception at all. Run with --count=20 to repeat the race."""
    start = threading.Barrier(2)
    conns = (committed_db.connect(), committed_db.connect())

    def racer(conn: Conn, draft_id: int) -> Callable[[], object]:
        def go() -> object:
            start.wait(timeout=5)
            return approve_and_commit(conn, draft_id)

        return go

    runs = [in_thread(racer(conn, d)) for conn, d in zip(conns, two_room_drafts, strict=True)]
    for thread, _ in runs:
        thread.join(timeout=15)
    outcomes = [outcome for _, outcome in runs]

    assert all(not thread.is_alive() for thread, _ in runs)
    assert [o.error for o in outcomes] == [None, None]
    kinds = sorted(type(o.value).__name__ for o in outcomes)
    assert kinds == ["Approved", "Invalidated"]
    assert counts(committed_db.connect())["held_rooms"] == [101, 102]
