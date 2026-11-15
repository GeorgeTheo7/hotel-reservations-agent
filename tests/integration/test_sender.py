"""The simulated sender on committed_db: send_due commits one transaction per message.

Outbox rows are made directly with the `rows` helpers (tests may insert outbox rows;
in src/ only approval.py does, test_write_boundaries).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.clock import FixedClock
from anemos.sender import CLAIM_SQL, FakeTransport, SendReport, message_id_for, send_due

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

T0 = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)  # = Rows.T0 (outbox created_at default)


def outbox_rows(
    committed_db: CommittedDb, n: int, *, created_at: datetime = T0, **kw: Any
) -> list[int]:
    """n outbox rows for n approved drafts, committed. Returns their ids."""
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    ids = []
    for _ in range(n):
        _, _, draft_id = rows.approved_draft()
        ids.append(rows.outbox(draft_id, created_at=created_at, **kw))
    conn.commit()
    return ids


def row(committed_db: CommittedDb, outbox_id: int) -> dict[str, Any]:
    conn = committed_db.connect()
    cur = conn.execute(
        "SELECT status, attempts, last_error, next_attempt_at, sent_at FROM outbox WHERE id = %s",
        (outbox_id,),
    )
    assert cur.description is not None
    found = cur.fetchone()
    assert found is not None
    conn.commit()
    return dict(zip([c.name for c in cur.description], found, strict=True))


def events(committed_db: CommittedDb) -> list[tuple[str, Any]]:
    conn = committed_db.connect()
    found = conn.execute(
        "SELECT event, output FROM audit_log WHERE event LIKE 'outbox.%%' ORDER BY id"
    ).fetchall()
    conn.commit()
    return [(e, o) for e, o in found]


def run(committed_db: CommittedDb, at: datetime, transport: FakeTransport, **kw: Any) -> SendReport:
    return send_due(committed_db.connect, FixedClock(at), transport, **kw)


# --- sending ----------------------------------------------------------------------------


def test_a_due_message_is_sent_once(committed_db: CommittedDb) -> None:
    [outbox_id] = outbox_rows(committed_db, 1)
    transport = FakeTransport()
    now = T0 + timedelta(minutes=5)
    assert run(committed_db, now, transport) == SendReport(sent=[outbox_id])
    assert row(committed_db, outbox_id) == {
        "status": "sent", "attempts": 1, "last_error": None, "next_attempt_at": None,
        "sent_at": now,
    }  # fmt: skip
    [message] = transport.sent
    assert message.message_id == message_id_for(outbox_id)
    assert (message.to, message.subject) == ("guest@example.test", "Re: Room in July")
    guest = (
        committed_db.connect()
        .execute(
            "SELECT e.message_id FROM outbox o JOIN drafts d ON d.id = o.draft_id"
            " JOIN emails e ON e.id = d.email_id WHERE o.id = %s",
            (outbox_id,),
        )
        .fetchone()
    )
    assert guest is not None
    assert message.in_reply_to == message.references == guest[0]  # the reply threads
    assert [e for e, _ in events(committed_db)] == ["outbox.sent"]
    # A second run finds nothing: sent is final.
    assert run(committed_db, now + timedelta(days=1), transport) == SendReport()
    assert len(transport.sent) == 1


def test_fails_twice_with_backoff_then_is_sent(committed_db: CommittedDb) -> None:
    [outbox_id] = outbox_rows(committed_db, 1)
    transport = FakeTransport(fail_first=2)
    t = T0 + timedelta(minutes=5)

    assert run(committed_db, t, transport) == SendReport(failed=[outbox_id])
    first = row(committed_db, outbox_id)
    assert (first["status"], first["attempts"]) == ("pending", 1)
    assert first["last_error"] == "TransportError: simulated mailbox failure"
    assert first["next_attempt_at"] == t + timedelta(minutes=1)

    # Not due yet: nothing is tried.
    assert run(committed_db, t + timedelta(seconds=59), transport) == SendReport()

    t2 = t + timedelta(minutes=1)
    assert run(committed_db, t2, transport) == SendReport(failed=[outbox_id])
    assert row(committed_db, outbox_id)["next_attempt_at"] == t2 + timedelta(minutes=2)

    t3 = t2 + timedelta(minutes=2)
    assert run(committed_db, t3, transport) == SendReport(sent=[outbox_id])
    final = row(committed_db, outbox_id)
    assert (final["status"], final["attempts"], final["sent_at"]) == ("sent", 3, t3)
    assert final["last_error"] == "TransportError: simulated mailbox failure"  # history
    assert [e for e, _ in events(committed_db)] == [
        "outbox.send_failed", "outbox.send_failed", "outbox.sent",
    ]  # fmt: skip


def test_an_unexpected_exception_is_a_failed_attempt(committed_db: CommittedDb) -> None:
    """Not a TransportError: still recorded (attempts + 1, last_error, backoff), not a
    crash that leaves the row unchanged."""
    [outbox_id] = outbox_rows(committed_db, 1)
    transport = FakeTransport(fail_first=1, error=ValueError("bad header"))
    t = T0 + timedelta(minutes=5)
    assert run(committed_db, t, transport) == SendReport(failed=[outbox_id])
    assert row(committed_db, outbox_id) == {
        "status": "pending", "attempts": 1, "last_error": "ValueError: bad header",
        "next_attempt_at": t + timedelta(minutes=1), "sent_at": None,
    }  # fmt: skip


def test_one_failing_message_does_not_stop_the_others(committed_db: CommittedDb) -> None:
    first, second, third = outbox_rows(committed_db, 3)
    transport = FakeTransport(always_fail=frozenset({message_id_for(second)}))
    report = run(committed_db, T0 + timedelta(minutes=5), transport)
    assert report == SendReport(sent=[first, third], failed=[second])
    assert row(committed_db, second)["status"] == "pending"


def test_limit_caps_one_run(committed_db: CommittedDb) -> None:
    ids = outbox_rows(committed_db, 3)
    report = run(committed_db, T0 + timedelta(minutes=5), FakeTransport(), limit=2)
    assert report == SendReport(sent=ids[:2])


def test_a_sent_row_is_never_claimed(committed_db: CommittedDb) -> None:
    [outbox_id] = outbox_rows(committed_db, 1, status="sent", attempts=1, sent_at=T0)
    transport = FakeTransport()
    assert run(committed_db, T0 + timedelta(days=30), transport) == SendReport()
    assert transport.sent == []
    assert row(committed_db, outbox_id)["attempts"] == 1


def test_time_alone_sends_nothing_new(committed_db: CommittedDb) -> None:
    """Rule 5: with no pending row, any clock and any number of runs change nothing;
    the sender never creates outbox rows (rule 2)."""
    outbox_rows(committed_db, 2, status="sent", attempts=1, sent_at=T0)
    reader = committed_db.connect()
    before = reader.execute("SELECT to_jsonb(o)::text FROM outbox o ORDER BY id").fetchall()
    reader.commit()
    for days in (1, 30, 365):
        assert run(committed_db, T0 + timedelta(days=days), FakeTransport()) == SendReport()
    after = reader.execute("SELECT to_jsonb(o)::text FROM outbox o ORDER BY id").fetchall()
    assert after == before
    assert events(committed_db) == []


def test_works_with_a_clock_before_todays_real_date(committed_db: CommittedDb) -> None:
    """All outbox times come from the injected clock (D20): a clock in March 2025, well
    before the real date, still goes fail -> wait -> retry -> sent."""
    early = datetime(2025, 3, 1, 9, 0, tzinfo=UTC)
    [outbox_id] = outbox_rows(committed_db, 1, created_at=early)
    transport = FakeTransport(fail_first=1)
    assert run(committed_db, early, transport) == SendReport(failed=[outbox_id])
    assert run(committed_db, early + timedelta(seconds=30), transport) == SendReport()
    retry = early + timedelta(minutes=1)
    assert run(committed_db, retry, transport) == SendReport(sent=[outbox_id])
    assert row(committed_db, outbox_id)["sent_at"] == retry


def test_the_claim_query_uses_the_due_index(committed_db: CommittedDb) -> None:
    """The WHERE and ORDER BY repeat the index expression exactly. With sequential scans
    switched off, the plan must use outbox_pending_due_idx *with an index condition on
    the coalesce expression*; the index name alone is not enough, because the partial
    index (WHERE status = 'pending') can also be used just to find pending rows."""
    outbox_rows(committed_db, 3)
    conn = committed_db.connect()
    # Fresh statistics, and sequential scans off for this transaction only: the test
    # then asks "can this query use the index expression?", not "is the index cheapest
    # for three rows?", which could differ between machines or CI runs.
    conn.execute("ANALYZE outbox")
    conn.execute("SET LOCAL enable_seqscan = off")
    plan = [line for (line,) in conn.execute("EXPLAIN " + CLAIM_SQL, {"now": T0})]
    conn.rollback()
    scan = next((i for i, line in enumerate(plan) if "outbox_pending_due_idx" in line), None)
    assert scan is not None, plan
    assert "Index Cond: (COALESCE(next_attempt_at, created_at) <=" in plan[scan + 1], plan


# --- two sender runs at once (SKIP LOCKED) ---------------------------------------------


def test_a_second_sender_skips_the_row_the_first_is_sending(committed_db: CommittedDb) -> None:
    """Sender A is held inside the transport with row 1 locked; sender B skips row 1
    (SKIP LOCKED) and sends row 2; A then finishes row 1. Each row is sent once."""
    first, second = outbox_rows(committed_db, 2)
    gate = threading.Event()
    held = FakeTransport(gate=gate)
    free = FakeTransport()
    now = T0 + timedelta(minutes=5)
    reports: list[SendReport] = []
    thread = threading.Thread(target=lambda: reports.append(run(committed_db, now, held)))
    thread.start()
    assert held.entered.wait(timeout=10), "sender A never reached the transport"

    assert run(committed_db, now, free) == SendReport(sent=[second])  # B skipped row 1
    gate.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert reports == [SendReport(sent=[first])]  # row 2 was already sent when A looked
    assert [m.message_id for m in held.sent] == [message_id_for(first)]
    assert [m.message_id for m in free.sent] == [message_id_for(second)]


def test_two_senders_released_together_send_each_row_exactly_once(
    committed_db: CommittedDb,
) -> None:
    """20 due rows, two sender runs started by a barrier. Run with --count=20."""
    ids = outbox_rows(committed_db, 20)
    start = threading.Barrier(2)
    transports = (FakeTransport(), FakeTransport())
    now = T0 + timedelta(minutes=5)

    def worker(transport: FakeTransport) -> None:
        start.wait(timeout=5)
        run(committed_db, now, transport)

    threads = [threading.Thread(target=worker, args=(t,)) for t in transports]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    sent = [m.message_id for t in transports for m in t.sent]
    assert sorted(sent) == sorted(message_id_for(i) for i in ids)  # each exactly once
    reader = committed_db.connect()
    statuses = reader.execute("SELECT DISTINCT status FROM outbox").fetchall()
    assert statuses == [("sent",)]
