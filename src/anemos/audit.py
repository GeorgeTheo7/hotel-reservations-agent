"""The audit log (spec Data model; write-once, D51): every event name in one place.

Event names have the form `area.action`; the database CHECK `audit_log_event_format`
refuses anything else, and a test inserts every name below to prove each is accepted.
Add a name here before using it.
"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

type Actor = Literal["owner", "agent", "system"]


class AuditEvent(StrEnum):
    EMAIL_STORED = "email.stored"
    EMAIL_PROCESSING = "email.processing"
    EMAIL_ESCALATED = "email.escalated"
    EMAIL_CLOSED = "email.closed"
    EMAIL_REOPENED = "email.reopened"
    DRAFT_CREATED = "draft.created"
    DRAFT_EDITED = "draft.edited"
    DRAFT_REJECTED = "draft.rejected"
    DRAFT_EXPIRED = "draft.expired"
    DRAFT_ACTION_REFUSED = "draft.action_refused"
    DRAFT_SUPERSEDED = "draft.superseded"
    HOLD_CONFIRMED = "hold.confirmed"
    HOLD_RELEASED = "hold.released"
    BOOKING_CANCELLED = "booking.cancelled"


def write_audit(
    conn: psycopg.Connection[TupleRow],
    at: datetime,
    actor: Actor,
    event: AuditEvent,
    *,
    email_id: int | None = None,
    draft_id: int | None = None,
    output: Mapping[str, object] | None = None,
) -> None:
    """Append one entry in the caller's transaction (no commit)."""
    conn.execute(
        "INSERT INTO audit_log (at, actor, event, email_id, draft_id, output)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (at, actor, event.value, email_id, draft_id, None if output is None else Jsonb(output)),
    )
