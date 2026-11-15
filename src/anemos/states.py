"""Statuses of emails, drafts, bookings and outbox messages (docs/states.md).

The database has the same lists twice: in each table's status CHECK and in the
final-status triggers (D51). Integration tests compare both with these enums, so the
three cannot drift apart. Transitions live in docs/states.md and in the code that
performs them, not here.
"""

from enum import StrEnum
from typing import Literal


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


type Entity = Literal["email", "draft", "booking"]


class ActionRefused(Exception):
    """An owner action was refused before anything was written.

    The caller rolls back; the API then records the attempt as one
    `<entity>.action_refused` audit row in its own short transaction and answers 409.
    Carries what that row needs: the entity, its id, the action, the email and draft
    it belongs to (when known) and `details` (why).
    """

    reason: str  # set by each subclass; the API's "error" and the audit "reason"

    def __init__(
        self,
        message: str,
        *,
        entity: Entity,
        entity_id: int,
        action: str,
        email_id: int | None = None,
        draft_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.entity: Entity = entity
        self.entity_id = entity_id
        self.action = action
        self.email_id = entity_id if entity == "email" else email_id
        self.draft_id = entity_id if entity == "draft" else draft_id

    def details(self) -> dict[str, object]:
        raise NotImplementedError

    def response_details(self) -> dict[str, object]:
        """What the 409 body shows besides "error"; by default the same as the audit."""
        return self.details()

    def audit_output(self) -> dict[str, object]:
        ids = {"booking_id": self.entity_id} if self.entity == "booking" else {}
        return {"action": self.action, "reason": self.reason} | ids | self.details()


class TransitionRefused(ActionRefused):
    """An action asked for a transition docs/states.md does not allow from the row's
    current status. Nothing was changed; the API turns this into 409."""

    reason = "transition_refused"

    def __init__(
        self,
        entity: Entity,
        entity_id: int,
        current: str,
        wanted: str,
        *,
        action: str,
        email_id: int | None = None,
        draft_id: int | None = None,
    ) -> None:
        super().__init__(
            f"{entity} {entity_id} is {current!r}; it cannot become {wanted!r}",
            entity=entity, entity_id=entity_id, action=action, email_id=email_id,
            draft_id=draft_id,
        )  # fmt: skip
        self.current = current
        self.wanted = wanted

    def details(self) -> dict[str, object]:
        return {"current": self.current, "wanted": self.wanted}


class StaleVersion(ActionRefused):
    """D67: the owner approved or edited from a version that is no longer the latest
    (another screen saved a newer one). Nothing was changed; the API answers 409."""

    reason = "stale_version"

    def __init__(self, draft_id: int, seen: int, latest: int, *, action: str, email_id: int):
        super().__init__(
            f"draft {draft_id}: version {seen} was seen, the latest is {latest}",
            entity="draft", entity_id=draft_id, action=action, email_id=email_id,
        )  # fmt: skip
        self.seen = seen
        self.latest = latest

    def details(self) -> dict[str, object]:
        return {"seen": self.seen, "latest": self.latest}

    def response_details(self) -> dict[str, object]:
        return {"latest": self.latest}  # D67's body; `seen` is in the audit row


class NotFound(LookupError):
    """No row with the id the caller named (the API turns this into 404).

    A subclass, not plain LookupError, because code also raises LookupError for rows
    that should exist and do not (a draft without a version): those are bugs, and the
    API must answer 500 for them, not 404.
    """
