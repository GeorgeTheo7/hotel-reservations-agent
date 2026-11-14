"""Statuses of emails, drafts, bookings and outbox messages (docs/states.md).

The database has the same lists twice: in each table's status CHECK and in the
final-status triggers (D51). Integration tests compare both with these enums, so the
three cannot drift apart. Transitions live in docs/states.md and in the code that
performs them, not here.
"""

from enum import StrEnum


class EmailStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    SET_ASIDE = "set_aside"
    ESCALATED = "escalated"
    AWAITING_APPROVAL = "awaiting_approval"
    ANSWERED = "answered"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    CLOSED = "closed"


class DraftStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class BookingStatus(StrEnum):
    HOLD = "hold"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"


# "Final" = no transition leaves it (docs/states.md). The database freezes these rows.
FINAL_EMAIL_STATUSES = frozenset({EmailStatus.ANSWERED, EmailStatus.SUPERSEDED, EmailStatus.CLOSED})
FINAL_DRAFT_STATUSES = frozenset(
    {
        DraftStatus.APPROVED,
        DraftStatus.REJECTED,
        DraftStatus.INVALIDATED,
        DraftStatus.EXPIRED,
        DraftStatus.SUPERSEDED,
    }
)
FINAL_BOOKING_STATUSES = frozenset({BookingStatus.CANCELLED, BookingStatus.EXPIRED})
FINAL_OUTBOX_STATUSES = frozenset({OutboxStatus.SENT})
