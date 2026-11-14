"""Storing emails: idempotent on message_id (D4) and superseding (D48, states.md).

Most tests use the rolled-back `db`; `ingest` and the per-sender lock need real
commits and two connections (`committed_db`).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.clock import FixedClock
from anemos.inbox import StoreResult, ingest, lock_sender, store_email
from anemos.mailbox import FakeMailbox, IncomingEmail, incoming

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import CommittedDb, Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

T0 = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)
GUEST = "guest@example.test"


def mail(n: int, *, sender: str = GUEST, received_at: datetime = T0) -> IncomingEmail:
    return incoming(
        message_id=f"<m{n}@example.test>", from_header=sender, subject=f"Email {n}",
        body=f"Body {n}", received_at=received_at,
    )  # fmt: skip


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0 + timedelta(hours=1))


def waiting_draft(
    rows: Rows, *, sender: str = GUEST, received_at: datetime = T0
) -> tuple[int, int]:
    """(email id, draft id): an email awaiting approval with its pending draft."""
    email_id = rows.email(
        from_address=sender, status="awaiting_approval", received_at=received_at,
        message_id=f"<old-{received_at.isoformat()}-{sender}>",
    )  # fmt: skip
    return email_id, rows.draft(email_id, None, route="policy", created_at=received_at)


def statuses(db: Conn, email_id: int, draft_id: int) -> tuple[str, str, Any]:
    row = db.execute(
        "SELECT e.status, d.status, d.decided_at FROM emails e JOIN drafts d"
        " ON d.email_id = e.id WHERE e.id = %s AND d.id = %s",
        (email_id, draft_id),
    ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


# --- storing --------------------------------------------------------------------------


def test_new_email_is_stored_as_received(db: Conn, clock: FixedClock) -> None:
    email = incoming(
        message_id="<new@example.test>", from_header="Anna K <Anna@Example.test>",
        subject="Room", body="Hello\r\n", received_at=T0,
    )  # fmt: skip
    result = store_email(db, clock, email)
    assert result.stored is True
    row = db.execute(
        "SELECT from_address, status, clean_body, received_at, created_at FROM emails"
        " WHERE id = %s",
        (result.email_id,),
    ).fetchone()
    assert row == ("anna@example.test", "received", "Hello", T0, clock.now())


def test_storing_the_same_message_twice_keeps_one_row(db: Conn, clock: FixedClock) -> None:
    first = store_email(db, clock, mail(1))
    second = store_email(db, clock, mail(1))
    assert second == StoreResult(first.email_id, stored=False)
    count = db.execute(
        "SELECT count(*) FROM emails WHERE message_id = '<m1@example.test>'"
    ).fetchone()
    assert count == (1,)


def test_storing_is_audited_once(db: Conn, clock: FixedClock) -> None:
    result = store_email(db, clock, mail(1))
    store_email(db, clock, mail(1))
    events = db.execute(
        "SELECT actor, event FROM audit_log WHERE email_id = %s", (result.email_id,)
    ).fetchall()
    assert events == [("system", "email.stored")]


# --- superseding (D48) ----------------------------------------------------------------


def test_newer_email_supersedes_the_waiting_draft(db: Conn, rows: Rows, clock: FixedClock) -> None:
    old_email, old_draft = waiting_draft(rows)
    result = store_email(db, clock, mail(2, received_at=T0 + timedelta(minutes=30)))
    assert result.superseded_draft_ids == (old_draft,)
    assert statuses(db, old_email, old_draft) == ("superseded", "superseded", clock.now())
    audit = db.execute(
        "SELECT event, draft_id, output->>'by_email_id' FROM audit_log"
        " WHERE event = 'draft.superseded'"
    ).fetchall()
    assert audit == [("draft.superseded", old_draft, str(result.email_id))]


def test_address_case_does_not_matter(db: Conn, rows: Rows, clock: FixedClock) -> None:
    old_email, old_draft = waiting_draft(rows)
    store_email(db, clock, mail(2, sender="GUEST@Example.Test", received_at=T0 + timedelta(1)))
    assert statuses(db, old_email, old_draft)[:2] == ("superseded", "superseded")


def test_another_sender_is_not_superseded(db: Conn, rows: Rows, clock: FixedClock) -> None:
    old_email, old_draft = waiting_draft(rows)
    store_email(db, clock, mail(2, sender="other@example.test", received_at=T0 + timedelta(1)))
    assert statuses(db, old_email, old_draft) == ("awaiting_approval", "pending_approval", None)


@pytest.mark.parametrize(
    ("status", "reasons"),
    [("set_aside", []), ("escalated", ["COMPLAINT"]), ("expired", []), ("received", []),
     ("processing", [])],
)  # fmt: skip
def test_emails_without_a_waiting_draft_are_left_alone(
    db: Conn, rows: Rows, clock: FixedClock, status: str, reasons: list[str]
) -> None:
    """states.md: only a draft waiting for approval is superseded."""
    old = rows.email(status=status, reason_codes=reasons, message_id="<old@example.test>")
    store_email(db, clock, mail(2, received_at=T0 + timedelta(hours=1)))
    assert db.execute("SELECT status FROM emails WHERE id = %s", (old,)).fetchone() == (status,)


@pytest.mark.parametrize(
    ("draft_status", "email_status", "reasons"),
    [("approved", "answered", []), ("rejected", "closed", []), ("expired", "expired", []),
     ("invalidated", "escalated", ["NO_LONGER_AVAILABLE"]), ("superseded", "superseded", [])],
)  # fmt: skip
def test_decided_drafts_are_left_alone(
    db: Conn,
    rows: Rows,
    clock: FixedClock,
    draft_status: str,
    email_status: str,
    reasons: list[str],
) -> None:
    """Only pending_approval is superseded; a decided draft (and its email) keeps its
    status. (Several of these are final: the D51 trigger would refuse the change.)"""
    old_email = rows.email(
        status=email_status, reason_codes=reasons, message_id="<old@example.test>"
    )
    old_draft = rows.draft(old_email, None, route="policy", status=draft_status, decided_at=T0)
    result = store_email(db, clock, mail(2, received_at=T0 + timedelta(hours=1)))
    assert result.superseded_draft_ids == ()
    assert statuses(db, old_email, old_draft)[:2] == (email_status, draft_status)


def test_email_stored_late_with_an_older_received_at_supersedes_nothing(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    """D48: 'newer' is received_at, not storage order. (Its own draft will be born
    superseded, step 10.)"""
    old_email, old_draft = waiting_draft(rows, received_at=T0)
    result = store_email(db, clock, mail(2, received_at=T0 - timedelta(hours=3)))
    assert result.superseded_draft_ids == ()
    assert statuses(db, old_email, old_draft)[:2] == ("awaiting_approval", "pending_approval")


def test_equal_received_at_the_later_stored_email_is_newer(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    """D48 tie-break: same received_at, higher id = newer."""
    _, old_draft = waiting_draft(rows, received_at=T0)
    result = store_email(db, clock, mail(2, received_at=T0))
    assert result.superseded_draft_ids == (old_draft,)


def test_redelivered_old_message_supersedes_nothing(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    """A duplicate is ignored entirely: m1 (received 09:00) is newer than the waiting
    draft's email (07:00), so as a new email it would supersede it; redelivered, it
    changes nothing."""
    store_email(db, clock, mail(1, received_at=T0 + timedelta(hours=2)))
    old_email, old_draft = waiting_draft(rows, received_at=T0)
    result = store_email(db, clock, mail(1, received_at=T0 + timedelta(hours=2)))
    assert result.stored is False
    assert statuses(db, old_email, old_draft)[:2] == ("awaiting_approval", "pending_approval")


def test_every_older_waiting_draft_of_the_sender_is_superseded(
    db: Conn, rows: Rows, clock: FixedClock
) -> None:
    first = waiting_draft(rows, received_at=T0 - timedelta(hours=2))
    second = waiting_draft(rows, received_at=T0 - timedelta(hours=1))
    result = store_email(db, clock, mail(3, received_at=T0))
    assert result.superseded_draft_ids == (first[1], second[1])


# --- real commits: ingest and the per-sender lock ----------------------------------


def test_ingest_commits_each_email_and_is_idempotent(committed_db: CommittedDb) -> None:
    mailbox = FakeMailbox([mail(1), mail(2, sender="other@example.test")])
    first = ingest(committed_db.connect, committed_db.clock, mailbox)
    second = ingest(committed_db.connect, committed_db.clock, mailbox)
    assert [r.stored for r in first] == [True, True]
    assert [r.stored for r in second] == [False, False]
    assert [r.email_id for r in second] == [r.email_id for r in first]
    reader = committed_db.connect()
    assert reader.execute("SELECT count(*) FROM emails").fetchone() == (2,)


def test_storing_waits_for_the_senders_lock(committed_db: CommittedDb) -> None:
    """Superseding and draft creation take the same per-sender lock, so they cannot
    interleave (the born-superseded race, step 10). Another sender is not blocked."""
    holder, storer, other = (committed_db.connect() for _ in range(3))
    lock_sender(holder, GUEST)

    store_email(other, committed_db.clock, mail(9, sender="other@example.test"))
    other.commit()  # a different sender went straight through

    done: list[StoreResult] = []

    def store_and_commit() -> None:
        done.append(store_email(storer, committed_db.clock, mail(1)))
        storer.commit()

    thread = threading.Thread(target=store_and_commit)
    thread.start()
    committed_db.wait_until_blocked(storer)
    assert done == []
    holder.commit()  # releases the advisory lock
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert done[0].stored is True
