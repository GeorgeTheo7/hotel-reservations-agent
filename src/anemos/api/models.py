"""Response bodies. Every field is a value the database already holds.

The API computes no price and no date: money is the integer cents stored in `quotes`
and `bookings`, the offer text is the code-rendered `draft_offers.rendered_block`, and
check-in/check-out are the two ends of the stored stay range. (A unit test checks that
api/ never imports anemos.reservations.)
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from anemos.states import BookingStatus, DraftStatus, EmailStatus, OutboxStatus


class EmailSummary(BaseModel):
    id: int
    from_address: str
    subject: str
    received_at: datetime
    status: EmailStatus
    reason_codes: list[str]
    draft_id: int | None
    draft_status: DraftStatus | None


class EmailPage(BaseModel):
    items: list[EmailSummary]
    next_cursor: str | None  # pass back as `cursor` for the next page; None = last page


class DraftVersion(BaseModel):
    version: int
    author: str
    body: str
    guardrail_results: dict[str, Any]
    created_at: datetime


class DraftOffer(BaseModel):
    quote_id: int
    room_type_id: str
    check_in: date
    check_out: date
    rooms: int
    hold: bool
    hold_allowed: bool
    changes: list[str]
    accommodation_subtotal_cents: int
    city_tax_cents: int
    deposit_cents: int
    free_cancellation_until: date
    rendered_block: str


class Draft(BaseModel):
    id: int
    route: str
    status: DraftStatus
    created_at: datetime
    decided_at: datetime | None
    versions: list[DraftVersion]  # oldest first; the last one is the current text
    offers: list[DraftOffer]


class Outbox(BaseModel):
    id: int
    status: OutboxStatus
    attempts: int
    last_error: str | None
    next_attempt_at: datetime | None
    sent_at: datetime | None


class EmailDetail(BaseModel):
    id: int
    message_id: str
    from_address: str
    subject: str
    raw_body: str
    clean_body: str
    language: str | None
    received_at: datetime
    status: EmailStatus
    reason_codes: list[str]
    draft: Draft | None
    outbox: Outbox | None


class Booking(BaseModel):
    id: int
    room_id: int
    room_type_id: str
    check_in: date
    check_out: date
    status: BookingStatus
    expires_at: datetime | None
    # A hold whose expires_at is before the clock: shown, never flipped by a read (D22).
    past_expiry: bool
    price_cents: int
    email_id: int | None
    guest_address: str | None  # None for a booking entered without an email
    draft_id: int | None
