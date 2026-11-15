"""The simulated sender (step 15; states.md, Outbox: pending -> sent).

`send_due` delivers what the owner approved: it only moves outbox rows from `pending`
to `sent` and never creates one (only approval does, rule 2). It is not "the passing of
time" (rule 5): a row exists only because the owner approved a draft.

One transaction per message, so one failure never undoes another's result:
1. claim one due row: `FOR UPDATE SKIP LOCKED` locks it and skips rows another sender
   run has locked, so two runs never send the same row;
2. send it through the transport (fake in v1);
3. record the result and an audit entry, commit.

A failure of any kind (TransportError or anything else the transport raises) is a
failed attempt: attempts + 1, last_error, next_attempt_at = now + backoff. There is no
maximum; a `failed` status is Later (states.md). v1 holds the row lock during the send;
v2 replaces it with a lease (D66).
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import psycopg
from psycopg.rows import TupleRow

from anemos.audit import AuditEvent, write_audit
from anemos.clock import Clock
from anemos.states import OutboxStatus

type Conn = psycopg.Connection[TupleRow]

MAX_BACKOFF_MINUTES = 60
MESSAGE_ID_DOMAIN = "anemos.invalid"  # .invalid: reserved, never a real domain (RFC 2606)


@dataclass(frozen=True)
class OutgoingMessage:
    message_id: str  # stable per outbox row (D66)
    to: str
    subject: str
    body: str
    in_reply_to: str | None  # the guest's Message-ID, so the reply threads
    references: str | None  # same: the thread so far (one message in v1)


class TransportError(Exception):
    """A send that failed in an expected way (mailbox down, refused, timeout)."""


class Transport(Protocol):
    def send(self, message: OutgoingMessage) -> None:
        """Deliver the message or raise. Must not return before the send is done."""
        ...


@dataclass
class SendReport:
    sent: list[int] = field(default_factory=list)  # outbox ids
    failed: list[int] = field(default_factory=list)


def message_id_for(outbox_id: int) -> str:
    """D66: the same Message-ID on every attempt, so a real send can be checked later."""
    return f"<outbox-{outbox_id}@{MESSAGE_ID_DOMAIN}>"


def backoff(attempts: int) -> timedelta:
    """Wait after failed attempt `attempts` (1, 2, 3, ...): 1, 2, 4 ... minutes, max 60."""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    return timedelta(minutes=min(2 ** (attempts - 1), MAX_BACKOFF_MINUTES))


# Filters and sorts on exactly the index expression of migration 0011, so the planner
# can use outbox_pending_due_idx.
CLAIM_SQL = """
SELECT o.id, o.to_address, o.subject, o.body, o.attempts, e.message_id, e.id
FROM outbox o
JOIN drafts d ON d.id = o.draft_id
JOIN emails e ON e.id = d.email_id
WHERE o.status = 'pending'
  AND coalesce(o.next_attempt_at, o.created_at) <= %(now)s
ORDER BY coalesce(o.next_attempt_at, o.created_at), o.id
LIMIT 1
FOR UPDATE OF o SKIP LOCKED
"""


def send_due(
    connect: Callable[[], Conn], clock: Clock, transport: Transport, *, limit: int = 100
) -> SendReport:
    """Send up to `limit` due messages, each in its own committed transaction."""
    report = SendReport()
    with connect() as conn:
        for _ in range(limit):
            with conn.transaction():
                claimed = _send_one(conn, clock, transport)
            if claimed is None:
                break
            outbox_id, ok = claimed
            (report.sent if ok else report.failed).append(outbox_id)
    return report


def _send_one(conn: Conn, clock: Clock, transport: Transport) -> tuple[int, bool] | None:
    now = clock.now()
    row = conn.execute(CLAIM_SQL, {"now": now}).fetchone()
    if row is None:
        return None
    outbox_id, to, subject, body, attempts, guest_message_id, email_id = row
    message = OutgoingMessage(
        message_id=message_id_for(int(outbox_id)),
        to=str(to),
        subject=str(subject),
        body=str(body),
        in_reply_to=guest_message_id,
        references=guest_message_id,
    )
    attempt = int(attempts) + 1
    try:
        transport.send(message)
    except Exception as error:  # any failure is a failed attempt, never a crash
        _record_failure(conn, now, int(outbox_id), int(email_id), attempt, error)
        return int(outbox_id), False
    _record_sent(conn, now, int(outbox_id), int(email_id), attempt, message.message_id)
    return int(outbox_id), True


def _record_sent(
    conn: Conn, now: datetime, outbox_id: int, email_id: int, attempt: int, message_id: str
) -> None:
    conn.execute(
        "UPDATE outbox SET status = %s, sent_at = %s, attempts = %s WHERE id = %s",
        (OutboxStatus.SENT, now, attempt, outbox_id),
    )
    write_audit(
        conn, now, "system", AuditEvent.OUTBOX_SENT, email_id=email_id,
        output={"outbox_id": outbox_id, "attempt": attempt, "message_id": message_id},
    )  # fmt: skip


def _record_failure(
    conn: Conn, now: datetime, outbox_id: int, email_id: int, attempt: int, error: Exception
) -> None:
    last_error = f"{type(error).__name__}: {error}"
    retry_at = now + backoff(attempt)
    conn.execute(
        "UPDATE outbox SET attempts = %s, last_error = %s, next_attempt_at = %s WHERE id = %s",
        (attempt, last_error, retry_at, outbox_id),
    )
    write_audit(
        conn, now, "system", AuditEvent.OUTBOX_SEND_FAILED, email_id=email_id,
        output={"outbox_id": outbox_id, "attempt": attempt, "error": last_error,
                "next_attempt_at": retry_at.isoformat()},
    )  # fmt: skip


class FakeTransport:
    """A transport whose behaviour each test scripts (v1 sends nothing for real).

    - `fail_first=N`: the first N sends raise `error` (default TransportError).
    - `always_fail`: Message-IDs that fail on every attempt.
    - `gate`: if set, a send waits on it after setting `entered`, so a test can hold a
      sender in the middle of a send (race tests).
    `sent` records every successful message; it is safe to share between threads.
    """

    def __init__(
        self,
        *,
        fail_first: int = 0,
        error: Exception | None = None,
        always_fail: frozenset[str] = frozenset(),
        gate: threading.Event | None = None,
    ) -> None:
        self.failures_left = fail_first
        self.error = error or TransportError("simulated mailbox failure")
        self.always_fail = always_fail
        self.gate = gate
        self.entered = threading.Event()
        self.sent: list[OutgoingMessage] = []
        self._lock = threading.Lock()

    def send(self, message: OutgoingMessage) -> None:
        if self.gate is not None:
            self.entered.set()
            if not self.gate.wait(timeout=10):
                raise TransportError("gate never opened")
        with self._lock:
            if message.message_id in self.always_fail:
                raise self.error
            if self.failures_left > 0:
                self.failures_left -= 1
                raise self.error
            self.sent.append(message)
