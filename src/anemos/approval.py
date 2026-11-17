"""The approval transaction (D10, states.md: pending_approval -> approved / invalidated).

`approve_draft` is the only code that creates bookings and outbox rows (states.md).
It runs in the caller's transaction and does not commit; every result except
TransitionRefused is a return value, so the caller commits what happened (an expiry
flip, an invalidation, or the approval itself).

Steps, in D10's order:
 1. Lock: the sender lock (D61), then the draft and its email (FOR UPDATE). Not
    pending_approval -> TransitionRefused (409, covers double approval). Older than
    72 h -> flipped to expired, DraftExpired (409 "expired", D54).
 2. Version (D67): the version the owner saw must be the latest, else StaleVersion
    (409, nothing written). Then warnings: the latest version's price check (D63).
    A warning -> PriceWarning (422) unless confirm_warnings; nothing is written.
 3. Flip stale holds: every `hold` past expires_at becomes `expired` (D10, D22).
 4. Re-validate everything the draft states (D47): each offered quote is priced again
    from the database and must match exactly, and each offered plan must still be
    free (rooms_for_plan, D59), and its check-in moment (15:00 Athens on the arrival
    date) must not have passed (D64). Any failure -> invalidated (D44).
 5. Holds: for the held offer only (D9), the lowest free room numbers (D38), expiring
    48 h after approval, inside a SAVEPOINT, after a per-room lock taken in ascending
    order (D52). An exclusion violation (another approval took the room since step 4)
    -> invalidated.
 6. Outbox: the latest version's text with {{HOLD_UNTIL}} filled in (D8), one row.
 7. Audit, then draft approved and email answered.

The foreign keys from bookings and outbox to the approved draft are deferred, so
writing them before the draft becomes `approved` (D10's order) is fine; they are
checked at COMMIT.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.errors import ExclusionViolation
from psycopg.rows import TupleRow

from anemos.audit import AuditEvent, write_audit
from anemos.clock import Clock
from anemos.guardrails import PRICE_CHECK, missing_blocks
from anemos.owner_actions import DraftExpired, decide_draft, lock_pending_draft, set_email
from anemos.repricing import amounts, price_again
from anemos.reservations.availability import rooms_for_plan
from anemos.reservations.errors import FullyBooked, NeedsInfo, ReasonCode
from anemos.reservations.formatting import Lang
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.hotel_time import check_in_moment
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.model import Party, Stay
from anemos.reservations.quote_template import render_hold_until
from anemos.reservations.tools import (
    LoadedHoliday,
    LoadedRequest,
    LoadedWindow,
    load_request,
)
from anemos.states import BookingStatus, DraftStatus, EmailStatus, StaleVersion

type Conn = psycopg.Connection[TupleRow]

HOLD_FOR = timedelta(hours=48)  # D9
HOLD_UNTIL = "{{HOLD_UNTIL}}"


@dataclass(frozen=True)
class Approved:
    draft_id: int
    outbox_id: int
    booking_ids: tuple[int, ...]  # empty when nothing was held
    hold_until: datetime | None


@dataclass(frozen=True)
class PriceWarning:
    """The latest version changed a quote block (D63); approve again with
    confirm_warnings=True to send it anyway (D10: 422). Nothing was written."""

    message: str  # for people: which block numbers changed
    version: int  # the version carrying the warning
    quote_ids: tuple[int, ...]  # the offers whose rendered_block is no longer in the text


@dataclass(frozen=True)
class Invalidated:
    """Re-validation failed (D44, D47, D52): draft invalidated, email escalated with
    NO_LONGER_AVAILABLE, nothing held or sent. Commit to keep it."""

    draft_id: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _Offer:
    quote_id: int
    hold: bool
    room_type_id: str
    stay: Stay
    rooms: int
    stored: dict[str, Any]  # the stored amounts and lines, to compare with a fresh price


def approve_draft(
    conn: Conn, clock: Clock, draft_id: int, *, version: int, confirm_warnings: bool = False
) -> Approved | PriceWarning | Invalidated | DraftExpired:
    """Approve the draft as the owner saw it: `version` is the version number on the
    owner's screen (D67)."""
    now = clock.now()

    # 1. Lock; refuse unless pending; expire if past 72 h.
    locked = lock_pending_draft(conn, now, draft_id, action="approve")
    if isinstance(locked, DraftExpired):
        return locked
    email_id = locked

    # 2. The version the owner saw must be the one that would be sent (D67); then its
    # warnings.
    latest, body, results = _latest_version(conn, draft_id)
    if version != latest:
        raise StaleVersion(draft_id, version, latest, action="approve", email_id=email_id)
    price_check = results.get(PRICE_CHECK, True)
    if price_check is not True and not confirm_warnings:
        return PriceWarning(str(price_check), latest, _changed_offers(conn, draft_id, body))

    # 3. Flip stale holds (in id order, so two approvals lock them in the same order).
    _flip_stale_holds(conn, now)

    # 4. Re-validate every offer against a fresh read of the database.
    offers = _offers(conn, draft_id)
    reasons: list[str] = []
    rooms_by_quote: dict[int, tuple[int, ...]] = {}
    if offers:
        hotel = load_hotel_data(conn)  # rates and calendar as they are now
        loaded = _request_of(conn, draft_id)
        for offer in offers:
            arrival = check_in_moment(offer.stay.check_in)
            if arrival < now:  # D64: the guest's check-in (15:00 Athens) has passed
                reasons.append(
                    f"quote {offer.quote_id}: check-in on {offer.stay.check_in} at 15:00 has passed"
                )
            reason = _price_changed(hotel, loaded.party, offer)
            if reason:
                reasons.append(reason)
            free = rooms_for_plan(conn, clock, offer.room_type_id, offer.stay, offer.rooms)
            if isinstance(free, FullyBooked):
                reasons.append(
                    f"quote {offer.quote_id}: {free.room_type_id} has {free.rooms_free} of"
                    f" {free.rooms_needed} room(s) free"
                )
            else:
                rooms_by_quote[offer.quote_id] = free
    if reasons:
        return _invalidate(conn, now, draft_id, email_id, reasons)

    # 5. Holds for the held offer only, in a savepoint (D52).
    hold_until = now + HOLD_FOR
    held = [o for o in offers if o.hold]
    booking_ids: list[int] = []
    if held:
        try:
            with conn.transaction():  # SAVEPOINT: a failure undoes only the holds
                booking_ids = _insert_holds(
                    conn, now, hold_until, draft_id, email_id, held[0], rooms_by_quote
                )
        except ExclusionViolation:
            return _invalidate(
                conn, now, draft_id, email_id,
                [f"quote {held[0].quote_id}: a room was taken by another approval"],
            )  # fmt: skip

    # 6. Outbox: the text exactly as it will be sent.
    lang = _language(conn, email_id)
    text = _final_text(body, hold_until if held else None, lang)
    outbox_id = _insert_outbox(conn, now, draft_id, email_id, text)

    # 7. Audit, then the statuses.
    write_audit(
        conn, now, "owner", AuditEvent.DRAFT_APPROVED, email_id=email_id, draft_id=draft_id,
        output={"version": version, "outbox_id": outbox_id, "booking_ids": booking_ids,
                "confirmed_warning": price_check is not True},
    )  # fmt: skip
    decide_draft(conn, draft_id, DraftStatus.APPROVED, now)
    set_email(conn, email_id, EmailStatus.ANSWERED)
    return Approved(draft_id, outbox_id, tuple(booking_ids), hold_until if held else None)


# --- steps ----------------------------------------------------------------------------


def _latest_version(conn: Conn, draft_id: int) -> tuple[int, str, dict[str, Any]]:
    row = conn.execute(
        "SELECT version, body, guardrail_results FROM draft_versions"
        " WHERE draft_id = %s ORDER BY version DESC LIMIT 1",
        (draft_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"draft {draft_id} has no version")
    return int(row[0]), str(row[1]), dict(row[2])


def _changed_offers(conn: Conn, draft_id: int, body: str) -> tuple[int, ...]:
    """Quote ids of the offers whose rendered block is missing from `body` (D63)."""
    offers = conn.execute(
        "SELECT quote_id, rendered_block FROM draft_offers WHERE draft_id = %s ORDER BY quote_id",
        (draft_id,),
    ).fetchall()
    missing = missing_blocks(body, [str(block) for _, block in offers])
    return tuple(int(offers[i][0]) for i in missing)


def _flip_stale_holds(conn: Conn, now: datetime) -> None:
    """D10/D22: a hold past expires_at no longer blocks; make it official before the
    new holds, or the exclusion constraint would still see it. Locks in id order."""
    flipped = conn.execute(
        "UPDATE bookings SET status = %s WHERE id IN ("
        "  SELECT id FROM bookings WHERE status = %s AND expires_at < %s"
        "  ORDER BY id FOR UPDATE)"
        " RETURNING id, email_id",
        (BookingStatus.EXPIRED, BookingStatus.HOLD, now),
    ).fetchall()
    for booking_id, booking_email in flipped:
        write_audit(
            conn, now, "system", AuditEvent.HOLD_EXPIRED, email_id=booking_email,
            output={"booking_id": booking_id},
        )  # fmt: skip


def _offers(conn: Conn, draft_id: int) -> list[_Offer]:
    rows = conn.execute(
        "SELECT o.quote_id, o.hold, q.room_type_id, lower(q.stay), upper(q.stay), q.rooms,"
        " q.lines, q.room_subtotal_cents, q.extra_bed_subtotal_cents,"
        " q.accommodation_subtotal_cents, q.city_tax_cents, q.deposit_cents,"
        " q.free_cancellation_until"
        " FROM draft_offers o JOIN quotes q ON q.id = o.quote_id"
        " WHERE o.draft_id = %s ORDER BY o.quote_id",
        (draft_id,),
    ).fetchall()
    return [
        _Offer(
            quote_id=int(r[0]),
            hold=bool(r[1]),
            room_type_id=str(r[2]),
            stay=Stay(r[3], r[4]),
            rooms=int(r[5]),
            stored=amounts(r[6], *r[7:13]),
        )
        for r in rows
    ]


def _request_of(conn: Conn, draft_id: int) -> LoadedRequest | LoadedWindow:
    """The draft's request; re-validation needs only its party. A window request
    (D7) has options as its offers, priced and checked like any other."""
    row = conn.execute("SELECT request_id FROM drafts WHERE id = %s", (draft_id,)).fetchone()
    if row is None or row[0] is None:
        raise LookupError(f"draft {draft_id} offers quotes but has no request")
    loaded = load_request(conn, int(row[0]))
    if isinstance(loaded, NeedsInfo):
        raise RuntimeError(f"request of draft {draft_id} is incomplete: {loaded}")
    if isinstance(loaded, LoadedHoliday):  # D68 drafts decline or ask: no offers
        raise RuntimeError(f"draft {draft_id} offers quotes for a fixed-date holiday")
    return loaded


def _price_changed(hotel: HotelData, party: Party, offer: _Offer) -> str | None:
    """D47: the offer priced again now must equal the stored quote, line by line."""
    fresh = price_again(hotel, party, offer.quote_id, offer.room_type_id, offer.stay,
                        offer.rooms, offer.stored)  # fmt: skip
    return fresh if isinstance(fresh, str) else None


def _insert_holds(
    conn: Conn,
    now: datetime,
    hold_until: datetime,
    draft_id: int,
    email_id: int,
    offer: _Offer,
    rooms_by_quote: dict[int, tuple[int, ...]],
) -> list[int]:
    """One hold per room, lowest numbers first (D38); the accommodation total split
    over the rooms in whole cents, the remainder on the first room."""
    rooms = sorted(rooms_by_quote[offer.quote_id])  # ascending: every approval locks rooms
    _lock_rooms(conn, rooms)
    total = int(offer.stored["accommodation_subtotal_cents"])
    share, remainder = divmod(total, len(rooms))
    request_id = _request_id(conn, draft_id)
    ids: list[int] = []
    for i, room_id in enumerate(rooms):
        row = conn.execute(
            "INSERT INTO bookings (room_id, stay, status, expires_at, request_id, email_id,"
            " draft_id, price_cents, created_at)"
            " VALUES (%s, daterange(%s, %s, '[)'), %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                room_id, offer.stay.check_in, offer.stay.check_out, BookingStatus.HOLD,
                hold_until, request_id, email_id, draft_id,
                share + (remainder if i == 0 else 0), now,
            ),
        ).fetchone()  # fmt: skip
        if row is None:
            raise RuntimeError("INSERT ... RETURNING returned no row")
        ids.append(int(row[0]))
    return ids


ROOM_LOCK = 1  # first key of the two-key advisory lock: "a room's holds" (D52)


def _lock_rooms(conn: Conn, rooms: list[int]) -> None:
    """Serialise hold inserts per room, in ascending room order, until commit.

    Why not rely on the exclusion constraint alone: it checks for conflicts *after*
    inserting the row, so two transactions inserting a hold on the same room at the
    same moment each find the other's uncommitted row and wait for each other, a
    deadlock (seen in the 2-room race test). With this lock the second approval waits
    before its insert; once the first commits, its insert fails cleanly with an
    exclusion violation, which the savepoint turns into an invalidation (D52).
    The two-key form is a key space of its own: it cannot collide with the one-key
    sender lock (D61). Ascending order keeps multi-room approvals deadlock-free.
    """
    for room_id in rooms:
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (ROOM_LOCK, room_id))


def _request_id(conn: Conn, draft_id: int) -> int:
    row = conn.execute("SELECT request_id FROM drafts WHERE id = %s", (draft_id,)).fetchone()
    if row is None or row[0] is None:
        raise LookupError(f"draft {draft_id} has no request")
    return int(row[0])


def _language(conn: Conn, email_id: int) -> Lang:
    row = conn.execute("SELECT language FROM emails WHERE id = %s", (email_id,)).fetchone()
    return "el" if row is not None and row[0] == "el" else "en"


def _final_text(body: str, hold_until: datetime | None, lang: Lang) -> str:
    """{{HOLD_UNTIL}} is filled only for a held offer; a placeholder without a hold
    would promise a hold that does not exist, so it is a bug, not a text to send."""
    if hold_until is None:
        if HOLD_UNTIL in body:
            raise ValueError("text mentions {{HOLD_UNTIL}} but nothing is held")
        return body
    return body.replace(HOLD_UNTIL, render_hold_until(hold_until, lang))


def _insert_outbox(conn: Conn, now: datetime, draft_id: int, email_id: int, text: str) -> int:
    row = conn.execute(
        "SELECT from_address, subject FROM emails WHERE id = %s", (email_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no email {email_id}")
    address, subject = str(row[0]), str(row[1])
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
    out = conn.execute(
        "INSERT INTO outbox (draft_id, to_address, subject, body, status, created_at)"
        " VALUES (%s, %s, %s, %s, 'pending', %s) RETURNING id",
        (draft_id, address, reply_subject, text, now),
    ).fetchone()
    if out is None:
        raise RuntimeError("INSERT ... RETURNING returned no row")
    return int(out[0])


def _invalidate(
    conn: Conn, now: datetime, draft_id: int, email_id: int, reasons: list[str]
) -> Invalidated:
    """D44: draft invalidated, email escalated NO_LONGER_AVAILABLE; nothing held or
    sent. Stale holds flipped in step 3 stay flipped: they were expired anyway."""
    decide_draft(conn, draft_id, DraftStatus.INVALIDATED, now)
    conn.execute(
        "UPDATE emails SET status = %s, reason_codes = %s WHERE id = %s",
        (EmailStatus.ESCALATED, [ReasonCode.NO_LONGER_AVAILABLE.value], email_id),
    )
    write_audit(
        conn, now, "owner", AuditEvent.DRAFT_INVALIDATED, email_id=email_id,
        draft_id=draft_id, output={"reasons": reasons},
    )  # fmt: skip
    return Invalidated(draft_id, tuple(reasons))
