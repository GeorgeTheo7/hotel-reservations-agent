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


class TransitionRefused(Exception):
    """An action asked for a transition docs/states.md does not allow from the row's
    current status. Nothing was changed; the API turns this into 409."""

    def __init__(self, what: str, current: str, wanted: str) -> None:
        super().__init__(f"{what} is {current!r}; it cannot become {wanted!r}")
        self.current = current
        self.wanted = wanted


class NotFound(LookupError):
    """No row with the id the caller named (the API turns this into 404).

    A subclass, not plain LookupError, because code also raises LookupError for rows
    that should exist and do not (a draft without a version): those are bugs, and the
    API must answer 500 for them, not 404.
    """
