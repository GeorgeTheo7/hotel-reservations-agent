"""Read endpoints: the inbox, one email with its draft, the Holds page list.

Reads never write and never flip a status (D22): an expired draft or hold is only
changed by an owner action or the tidy-up job.
"""

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql
from psycopg.rows import dict_row

from anemos.api.deps import ClockDep, Conn, ConnDep, require_owner
from anemos.api.models import (
    Booking,
    Draft,
    DraftOffer,
    DraftVersion,
    EmailDetail,
    EmailPage,
    EmailSummary,
    Outbox,
)
from anemos.owner_actions import DRAFT_LIFETIME
from anemos.states import BookingStatus, EmailStatus, NotFound

router = APIRouter(dependencies=[Depends(require_owner)])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


# --- inbox -------------------------------------------------------------------------------


def encode_cursor(received_at: datetime, email_id: int) -> str:
    raw = json.dumps([received_at.isoformat(), email_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    """The (received_at, id) of the last email on the previous page; 422 if malformed."""
    try:
        at, email_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        received_at = datetime.fromisoformat(at)
        if received_at.tzinfo is None or type(email_id) is not int:
            raise ValueError("a cursor holds an aware time and an integer id")
    except (ValueError, TypeError, binascii.Error) as exc:  # JSONDecodeError is a ValueError
        raise HTTPException(status_code=422, detail="malformed cursor") from exc
    return received_at, email_id


def inbox_query(
    status: EmailStatus | None, after: tuple[datetime, int] | None, limit: int, now: datetime
) -> sql.Composed:
    """The inbox page query, one row more than `limit` (is there a next page?).

    Each form matches an index of migration 0012: a status filter uses
    emails_status_received_at_id_idx; the default view must spell out
    `status <> 'set_aside'` exactly as the partial index emails_visible_received_at_id_idx
    does, or the planner cannot prove the index covers the query.
    """
    where = [
        sql.SQL("e.status = {}").format(status.value)
        if status is not None
        else sql.SQL("e.status <> 'set_aside'")
    ]
    if after is not None:
        where.append(sql.SQL("(e.received_at, e.id) < ({}, {})").format(*after))
    return sql.SQL(
        "SELECT e.id, e.from_address, e.subject, e.received_at, e.status, e.reason_codes,"
        " d.id AS draft_id, d.status AS draft_status,"
        " COALESCE(d.status = 'pending_approval' AND d.created_at < {cutoff}, false)"
        " AS draft_past_expiry"
        " FROM emails e LEFT JOIN drafts d ON d.email_id = e.id"
        " WHERE {where} ORDER BY e.received_at DESC, e.id DESC LIMIT {n}"
    ).format(cutoff=expiry_cutoff(now), where=sql.SQL(" AND ").join(where), n=limit + 1)


def expiry_cutoff(now: datetime) -> datetime:
    """A pending draft created before this is past 72 h: `now - created_at > 72 h`,
    the same rule as the owner actions (exactly 72 h is not yet past, D42, D54)."""
    return now - DRAFT_LIFETIME


@router.get("/emails")
def list_emails(
    conn: ConnDep,
    clock: ClockDep,
    status: EmailStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> EmailPage:
    """Newest first. Without `status`: every email except set-aside ones, which have
    their own filter (D50). Keyset pagination: a page is "the emails after this
    (received_at, id)", so an email arriving between two pages cannot shift rows into
    or out of the next page."""
    after = decode_cursor(cursor) if cursor is not None else None
    found = (
        conn.cursor(row_factory=dict_row)
        .execute(inbox_query(status, after, limit, clock.now()))
        .fetchall()
    )
    items = [EmailSummary(**row) for row in found[:limit]]
    last = items[-1] if len(found) > limit else None
    return EmailPage(
        items=items, next_cursor=encode_cursor(last.received_at, last.id) if last else None
    )


# --- one email ---------------------------------------------------------------------------


def _one(conn: Conn, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    return conn.cursor(row_factory=dict_row).execute(query, params).fetchone()


def _all(conn: Conn, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return conn.cursor(row_factory=dict_row).execute(query, params).fetchall()


@router.get("/emails/{email_id}")
def get_email(conn: ConnDep, clock: ClockDep, email_id: int) -> EmailDetail:
    email = _one(
        conn,
        "SELECT id, message_id, from_address, subject, raw_body, clean_body, language,"
        " received_at, status, reason_codes, escalation_summary FROM emails WHERE id = %s",
        (email_id,),
    )
    if email is None:
        raise NotFound(f"no email {email_id}")
    draft = _one(
        conn,
        "SELECT id, route, status, created_at, decided_at,"
        " (status = 'pending_approval' AND created_at < %s) AS past_expiry"
        " FROM drafts WHERE email_id = %s",
        (expiry_cutoff(clock.now()), email_id),
    )
    if draft is None:
        return EmailDetail(**email, draft=None, outbox=None)
    versions = _all(
        conn,
        "SELECT version, author, body, guardrail_results, created_at FROM draft_versions"
        " WHERE draft_id = %s ORDER BY version",
        (draft["id"],),
    )
    offers = _all(
        conn,
        "SELECT q.id AS quote_id, q.room_type_id, t.name AS room_type_name,"
        " lower(q.stay) AS check_in,"
        " upper(q.stay) AS check_out, q.rooms, o.hold, o.quote_hold_allowed AS hold_allowed,"
        " q.changes, q.accommodation_subtotal_cents, q.city_tax_cents, q.deposit_cents,"
        " q.free_cancellation_until, o.rendered_block"
        " FROM draft_offers o JOIN quotes q ON q.id = o.quote_id"
        " JOIN room_types t ON t.id = q.room_type_id"
        " WHERE o.draft_id = %s ORDER BY q.id",
        (draft["id"],),
    )
    outbox = _one(
        conn,
        "SELECT id, status, attempts, last_error, next_attempt_at, sent_at FROM outbox"
        " WHERE draft_id = %s",
        (draft["id"],),
    )
    return EmailDetail(
        **email,
        draft=Draft(
            **draft,
            versions=[DraftVersion(**v) for v in versions],
            offers=[DraftOffer(**o) for o in offers],
        ),
        outbox=Outbox(**outbox) if outbox else None,
    )


# --- Holds page --------------------------------------------------------------------------


@router.get("/holds")
def list_holds(
    conn: ConnDep, clock: ClockDep, status: Literal["hold", "confirmed"] | None = None
) -> list[Booking]:
    """Holds and confirmed bookings (both by default), by check-in date then room.
    Cancelled and expired bookings are history and not on this page."""
    statuses = [status] if status else [BookingStatus.HOLD.value, BookingStatus.CONFIRMED.value]
    found = _all(
        conn,
        "SELECT b.id, b.room_id, r.room_type_id, t.name AS room_type_name,"
        " lower(b.stay) AS check_in,"
        " upper(b.stay) AS check_out, b.status, b.expires_at,"
        " (b.status = 'hold' AND b.expires_at < %s) AS past_expiry,"
        " b.price_cents, b.email_id, e.from_address AS guest_address, b.draft_id"
        " FROM bookings b JOIN rooms r ON r.id = b.room_id"
        " JOIN room_types t ON t.id = r.room_type_id"
        " LEFT JOIN emails e ON e.id = b.email_id"
        " WHERE b.status = ANY(%s) ORDER BY lower(b.stay), b.room_id, b.id",
        (clock.now(), statuses),
    )
    return [Booking(**row) for row in found]
