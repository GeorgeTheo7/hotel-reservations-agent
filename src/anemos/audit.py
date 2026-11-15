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
    EMAIL_ACTION_REFUSED = "email.action_refused"
    DRAFT_CREATED = "draft.created"
    DRAFT_EDITED = "draft.edited"
    DRAFT_REJECTED = "draft.rejected"
    DRAFT_EXPIRED = "draft.expired"
    DRAFT_ACTION_REFUSED = "draft.action_refused"
    DRAFT_PRICE_WARNING_SHOWN = "draft.price_warning_shown"
    DRAFT_SUPERSEDED = "draft.superseded"
    DRAFT_APPROVED = "draft.approved"
    DRAFT_INVALIDATED = "draft.invalidated"
    HOLD_EXPIRED = "hold.expired"
    HOLD_CONFIRMED = "hold.confirmed"
    HOLD_RELEASED = "hold.released"
    BOOKING_CANCELLED = "booking.cancelled"
    BOOKING_ACTION_REFUSED = "booking.action_refused"
    OUTBOX_SENT = "outbox.sent"
    OUTBOX_SEND_FAILED = "outbox.send_failed"


# Written only by the API, after it rolled back a refused or unconfirmed action
# (api/transactions.py). Domain code never writes them, so the cross-rules scenario,
# which calls the domain functions directly, does not either; tests/integration/
# test_api_actions.py covers them.
API_ONLY_EVENTS = frozenset(
    {
        AuditEvent.EMAIL_ACTION_REFUSED,
        AuditEvent.BOOKING_ACTION_REFUSED,
        AuditEvent.DRAFT_PRICE_WARNING_SHOWN,
    }
)


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
