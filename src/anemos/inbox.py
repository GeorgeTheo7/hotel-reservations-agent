"""Storing guest emails (states.md: (new) -> received; D4, D48).

`store_email` is one transaction's worth of work and does not commit: it inserts the
email (or recognises it as already stored) and, in the same transaction, supersedes
the sender's draft waiting for approval. `ingest` stores a mailbox's emails, one
committed transaction each.

Superseding and draft creation (step 10, "born superseded") must not interleave:
otherwise a draft committed a moment after a newer email could stay pending. Both
take the same per-sender lock (`lock_sender`) first, so they run one after another
for one address and never block other senders.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import TupleRow

from anemos.audit import Actor, AuditEvent, write_audit
from anemos.clock import Clock
from anemos.mailbox import IncomingEmail, Mailbox
from anemos.reservations.errors import ReasonCode
from anemos.states import DraftStatus, EmailStatus

type Conn = psycopg.Connection[TupleRow]


@dataclass(frozen=True)
class StoreResult:
    email_id: int
    stored: bool  # False: this message_id was already stored; nothing changed
    superseded_draft_ids: tuple[int, ...] = ()


def lock_sender(conn: Conn, from_address: str) -> None:
    """Serialise work on one sender's emails until the transaction ends.

    pg_advisory_xact_lock takes an application-defined lock on a number (here a hash
    of the address); it is released automatically at COMMIT or ROLLBACK. A hash
    collision between two addresses only makes them wait for each other.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (from_address,))


INSERT_EMAIL_SQL = """
INSERT INTO emails (message_id, in_reply_to, mail_thread_id, from_address, subject,
                    raw_body, clean_body, received_at, created_at, status)
VALUES (%(message_id)s, %(in_reply_to)s, %(mail_thread_id)s, %(from_address)s,
        %(subject)s, %(raw_body)s, %(clean_body)s, %(received_at)s, %(created_at)s,
        %(status)s)
ON CONFLICT (message_id) DO NOTHING
RETURNING id, received_at
"""

# D48: an older email of the same sender = earlier (received_at, id). Only a draft
# waiting for approval is superseded; set_aside / escalated / expired / final emails
# are left alone (states.md).
OLDER_PENDING_DRAFTS_SQL = """
SELECT d.id, e.id
FROM drafts d
JOIN emails e ON e.id = d.email_id
WHERE e.from_address = %(from_address)s
  AND (e.received_at, e.id) < (%(received_at)s, %(email_id)s)
  AND d.status = %(pending)s
ORDER BY d.id
FOR UPDATE OF d, e
"""


def store_email(conn: Conn, clock: Clock, email: IncomingEmail) -> StoreResult:
    """Store `email` as `received`, superseding older pending drafts of its sender.

    A message_id already stored is ignored (D4): nothing is inserted or superseded,
    and the existing email's id is returned.
    """
    lock_sender(conn, email.from_address)
    now = clock.now()
    row = conn.execute(
        INSERT_EMAIL_SQL,
        {
            "message_id": email.message_id,
            "in_reply_to": email.in_reply_to,
            "mail_thread_id": email.mail_thread_id,
            "from_address": email.from_address,
            "subject": email.subject,
            "raw_body": email.raw_body,
            "clean_body": email.clean_body,
            "received_at": email.received_at,
            "created_at": now,
            "status": EmailStatus.RECEIVED,
        },
    ).fetchone()
    if row is None:
        existing = conn.execute(
            "SELECT id FROM emails WHERE message_id = %s", (email.message_id,)
        ).fetchone()
        if existing is None:
            raise RuntimeError(f"{email.message_id!r} conflicted but is not stored")
        return StoreResult(int(existing[0]), stored=False)

    email_id, received_at = int(row[0]), row[1]
    write_audit(
        conn, now, "system", AuditEvent.EMAIL_STORED, email_id=email_id,
        output={"message_id": email.message_id},
    )  # fmt: skip

    superseded: list[int] = []
    for draft_id, old_email_id in conn.execute(
        OLDER_PENDING_DRAFTS_SQL,
        {
            "from_address": email.from_address,
            "received_at": received_at,
            "email_id": email_id,
            "pending": DraftStatus.PENDING_APPROVAL,
        },
    ).fetchall():
        conn.execute(
            "UPDATE drafts SET status = %s, decided_at = %s WHERE id = %s",
            (DraftStatus.SUPERSEDED, now, draft_id),
        )
        conn.execute(
            "UPDATE emails SET status = %s WHERE id = %s",
            (EmailStatus.SUPERSEDED, old_email_id),
        )
        write_audit(
            conn, now, "system", AuditEvent.DRAFT_SUPERSEDED, email_id=old_email_id,
            draft_id=draft_id, output={"by_email_id": email_id},
        )  # fmt: skip
        superseded.append(int(draft_id))
    return StoreResult(email_id, stored=True, superseded_draft_ids=tuple(superseded))


def ingest(connect: Callable[[], Conn], clock: Clock, mailbox: Mailbox) -> list[StoreResult]:
    """Store every fetched email, each in its own committed transaction, so one bad
    email cannot undo the others. Safe to run again: duplicates are ignored."""
    results: list[StoreResult] = []
    with connect() as conn:
        for email in mailbox.fetch():
            with conn.transaction():
                results.append(store_email(conn, clock, email))
    return results


def escalate(
    conn: Conn, now: datetime, email_id: int, codes: Sequence[ReasonCode], detail: str,
    *, actor: Actor, summary: str | None = None, model_added: Sequence[ReasonCode] = (),
) -> None:  # fmt: skip
    """processing -> escalated with every reason code (spec Escalation rules), audited.
    The caller holds the sender lock and has checked the status. `summary` and the codes
    the summary call added (D81) are stored and audited when there are any."""
    values = list(dict.fromkeys([c.value for c in codes] + [c.value for c in model_added]))
    conn.execute(
        "UPDATE emails SET status = %s, reason_codes = %s, escalation_summary = %s WHERE id = %s",
        (EmailStatus.ESCALATED, values, summary, email_id),
    )
    output: dict[str, object] = {"reason": values[0], "reasons": values, "detail": detail}
    if summary is not None or model_added:
        output |= {"summary": summary, "added_by_model": [c.value for c in model_added]}
    write_audit(conn, now, actor, AuditEvent.EMAIL_ESCALATED, email_id=email_id,
                output=output)  # fmt: skip


def set_aside(conn: Conn, now: datetime, email_id: int) -> None:
    """processing -> set_aside (spam: no draft, no reply; found through the inbox
    filter, D50), audited."""
    conn.execute("UPDATE emails SET status = %s WHERE id = %s", (EmailStatus.SET_ASIDE, email_id))
    write_audit(conn, now, "system", AuditEvent.EMAIL_SET_ASIDE, email_id=email_id)
