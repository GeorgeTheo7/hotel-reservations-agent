"""Owner actions other than approve (docs/states.md). Each runs in the caller's
transaction and does not commit.

Rules:
- An action checks the row's current status first. A transition states.md does not
  list raises TransitionRefused before anything is written (nothing changes; the API
  answers 409).
- Draft and email actions take the sender's lock first (D61), the same order as
  storing an email and drafting (advisory lock, then rows), so they serialise with
  superseding and cannot deadlock with it.
- A draft more than 72 hours old is treated as expired by every action (states.md,
  time-based statuses): it is flipped to `expired` (email too) and the action returns
  DraftExpired instead of raising, so the caller commits the flip (D54).
- `confirm_hold` works while the booking is still `hold`, even past `expires_at`: until
  a hold is flipped, the exclusion constraint has kept the room for this guest.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.agent.evidence import recorded_policy_texts
from anemos.audit import AuditEvent, write_audit
from anemos.clock import Clock
from anemos.guardrails import PRICE_CHECK, edit_checks, rendered_blocks_unchanged
from anemos.inbox import lock_sender
from anemos.reservations.errors import ReasonCode
from anemos.reservations.quote_template import summary_line
from anemos.states import (
    BookingStatus,
    DraftStatus,
    EmailStatus,
    NotFound,
    StaleVersion,
    TransitionRefused,
)

type Conn = psycopg.Connection[TupleRow]

DRAFT_LIFETIME = timedelta(hours=72)  # D17, D42: older than this = expired


@dataclass(frozen=True)
class DraftExpired:
    """The draft was past 72 h: it is now `expired` (and its email); the action did
    not happen. Commit to keep the flip."""

    draft_id: int


@dataclass(frozen=True)
class Edited:
    version: int
    price_warning: str | None  # D63: set if a quote block changed; approval needs confirm
    warnings: tuple[str, ...] = ()  # D77: guardrails 2-5 on the changed words; confirm too


# --- drafts ---------------------------------------------------------------------------


def edit_draft(
    conn: Conn, clock: Clock, draft_id: int, body: str, *, base_version: int
) -> Edited | DraftExpired:
    """pending_approval -> pending_approval: a new text version by the owner (D41).
    The original versions are kept (write-once, D51).

    Price check (D63): the edit passes only if every quote block of the agent's version
    (draft_offers.rendered_block) appears verbatim in the new text. Otherwise the
    version's guardrail result records the edited-price warning, and approval refuses
    it unless confirm_warnings (D10). Guardrails 2-5 (D77) check the words the owner
    changed against version 1; each failure is a warning too. The edit itself is
    always stored.

    D67: `base_version` is the version the owner edited from; if a newer one exists,
    StaleVersion is raised under the draft lock and nothing is written (no lost edit).
    """
    if not body.strip():
        raise ValueError("an edited draft cannot be blank")
    now = clock.now()
    draft = lock_pending_draft(conn, now, draft_id, action="edit")
    if isinstance(draft, DraftExpired):
        return draft
    email_id = draft
    blocks = [
        str(r[0])
        for r in conn.execute(
            "SELECT rendered_block FROM draft_offers WHERE draft_id = %s ORDER BY quote_id",
            (draft_id,),
        ).fetchall()
    ]
    latest = latest_version_number(conn, draft_id)
    if base_version != latest:
        raise StaleVersion(draft_id, base_version, latest, action="edit", email_id=email_id)
    price_check = rendered_blocks_unchanged(body, blocks)
    results: dict[str, bool | str] = {PRICE_CHECK: price_check}
    results |= _edit_guardrails(conn, email_id, draft_id, body, blocks)
    version = latest + 1
    conn.execute(
        "INSERT INTO draft_versions (draft_id, version, author, body, guardrail_results,"
        " created_at) VALUES (%s, %s, 'owner', %s, %s, %s)",
        (draft_id, version, body, Jsonb(results), now),
    )
    warning = None if price_check is True else str(price_check)
    warnings = tuple(str(r) for k, r in results.items() if k != PRICE_CHECK and r is not True)
    write_audit(
        conn, now, "owner", AuditEvent.DRAFT_EDITED, email_id=email_id, draft_id=draft_id,
        output={"version": version, "price_warning": warning, "warnings": list(warnings)},
    )  # fmt: skip
    return Edited(version, warning, warnings)


def _edit_guardrails(
    conn: Conn, email_id: int, draft_id: int, body: str, blocks: list[str]
) -> dict[str, bool | str]:
    """Guardrails 2-5 on the words the owner changed against version 1 (D77)."""
    row = conn.execute(
        "SELECT v.body, e.language FROM draft_versions v JOIN emails e ON e.id = %s"
        " WHERE v.draft_id = %s AND v.version = 1",
        (email_id, draft_id),
    ).fetchone()
    assert row is not None  # every draft has version 1 (written with the draft)
    original, language = str(row[0]), "el" if row[1] == "el" else "en"
    return edit_checks(original, body, blocks=blocks, summary=summary_line(original),
                       policy_texts=recorded_policy_texts(conn, email_id),
                       language=language)  # fmt: skip


def reject_draft(conn: Conn, clock: Clock, draft_id: int) -> DraftExpired | None:
    """pending_approval -> rejected; email awaiting_approval -> closed (D40)."""
    now = clock.now()
    draft = lock_pending_draft(conn, now, draft_id, action="reject")
    if isinstance(draft, DraftExpired):
        return draft
    email_id = draft
    decide_draft(conn, draft_id, DraftStatus.REJECTED, now)
    set_email(conn, email_id, EmailStatus.CLOSED)
    write_audit(conn, now, "owner", AuditEvent.DRAFT_REJECTED, email_id=email_id, draft_id=draft_id)
    return None


def expire_draft_if_due(conn: Conn, clock: Clock, draft_id: int) -> bool:
    """Flip a pending draft older than 72 h to expired (email too). True if flipped.
    Used by every draft action; the week-10 tidy-up job will call it as well."""
    now = clock.now()
    email_id = _lock_draft(conn, draft_id)
    status, created_at = _draft_status(conn, draft_id)
    if status != DraftStatus.PENDING_APPROVAL or not _is_due(created_at, now):
        return False
    _expire(conn, now, draft_id, email_id)
    return True


# --- emails ---------------------------------------------------------------------------


def close_email(conn: Conn, clock: Clock, email_id: int) -> None:
    """escalated -> closed (D45) and expired -> closed (D42): the owner handled it.
    Escalation reasons are kept."""
    now = clock.now()
    status = _lock_email(conn, email_id)
    if status not in (EmailStatus.ESCALATED, EmailStatus.EXPIRED):
        raise TransitionRefused("email", email_id, status, EmailStatus.CLOSED, action="close")
    set_email(conn, email_id, EmailStatus.CLOSED)
    write_audit(
        conn, now, "owner", AuditEvent.EMAIL_CLOSED, email_id=email_id, output={"was": status}
    )


def reopen_email(conn: Conn, clock: Clock, email_id: int) -> None:
    """set_aside -> escalated with OWNER_REOPENED (D49, D50): "this was not spam".
    The owner handles it by hand; no draft (D45)."""
    now = clock.now()
    status = _lock_email(conn, email_id)
    if status != EmailStatus.SET_ASIDE:
        raise TransitionRefused("email", email_id, status, EmailStatus.ESCALATED, action="reopen")
    conn.execute(
        "UPDATE emails SET status = %s, reason_codes = %s WHERE id = %s",
        (EmailStatus.ESCALATED, [ReasonCode.OWNER_REOPENED.value], email_id),
    )
    write_audit(conn, now, "owner", AuditEvent.EMAIL_REOPENED, email_id=email_id)


# --- bookings (Holds page) ------------------------------------------------------------


def confirm_hold(conn: Conn, clock: Clock, booking_id: int) -> None:
    """hold -> confirmed: "deposit received" (D43). Allowed while the status is still
    `hold`, even after expires_at; an `expired` booking is final."""
    _move_booking(
        conn, clock, booking_id, BookingStatus.HOLD, BookingStatus.CONFIRMED,
        AuditEvent.HOLD_CONFIRMED, "confirm",
    )  # fmt: skip


def release_hold(conn: Conn, clock: Clock, booking_id: int) -> None:
    """hold -> cancelled: the owner releases the hold (D46)."""
    _move_booking(
        conn, clock, booking_id, BookingStatus.HOLD, BookingStatus.CANCELLED,
        AuditEvent.HOLD_RELEASED, "release",
    )  # fmt: skip


def cancel_booking(conn: Conn, clock: Clock, booking_id: int) -> None:
    """confirmed -> cancelled: the owner cancels a confirmed booking (D46)."""
    _move_booking(
        conn, clock, booking_id, BookingStatus.CONFIRMED, BookingStatus.CANCELLED,
        AuditEvent.BOOKING_CANCELLED, "cancel",
    )  # fmt: skip


# --- helpers ----------------------------------------------------------------------------


def _is_due(created_at: datetime, now: datetime) -> bool:
    """More than 72 hours old (states.md); exactly 72 h is not yet expired."""
    return now - created_at > DRAFT_LIFETIME


def _lock_draft(conn: Conn, draft_id: int) -> int:
    """Take the sender lock, then lock the draft and its email. Returns the email id."""
    row = conn.execute(
        "SELECT e.id, e.from_address FROM drafts d JOIN emails e ON e.id = d.email_id"
        " WHERE d.id = %s",
        (draft_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"no draft {draft_id}")
    email_id, address = int(row[0]), row[1]
    lock_sender(conn, address)
    conn.execute(
        "SELECT 1 FROM drafts d JOIN emails e ON e.id = d.email_id WHERE d.id = %s"
        " FOR UPDATE OF d, e",
        (draft_id,),
    )
    return email_id


def _draft_status(conn: Conn, draft_id: int) -> tuple[str, datetime]:
    row = conn.execute(
        "SELECT status, created_at FROM drafts WHERE id = %s", (draft_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"no draft {draft_id}")
    return row[0], row[1]


# The status each owner draft action would set (for TransitionRefused messages).
_ACTION_TARGET = {
    "edit": DraftStatus.PENDING_APPROVAL,
    "reject": DraftStatus.REJECTED,
    "approve": DraftStatus.APPROVED,
}


def lock_pending_draft(
    conn: Conn, now: datetime, draft_id: int, *, action: str
) -> int | DraftExpired:
    """Lock the draft; refuse unless pending_approval; expire it if past 72 h.
    Returns the email id when the action may go ahead.

    On expiry the audit log gets two entries: the flip (actor system: time did it,
    D17) and the owner's refused action (actor owner), so both are on record.
    """
    email_id = _lock_draft(conn, draft_id)
    status, created_at = _draft_status(conn, draft_id)
    if status != DraftStatus.PENDING_APPROVAL:
        raise TransitionRefused(
            "draft", draft_id, status, _ACTION_TARGET[action], action=action,
            email_id=email_id,
        )  # fmt: skip
    if _is_due(created_at, now):
        _expire(conn, now, draft_id, email_id)
        write_audit(
            conn, now, "owner", AuditEvent.DRAFT_ACTION_REFUSED, email_id=email_id,
            draft_id=draft_id, output={"action": action, "reason": "expired"},
        )  # fmt: skip
        return DraftExpired(draft_id)
    return email_id


def latest_version_number(conn: Conn, draft_id: int) -> int:
    """The highest version of a draft (every draft has version 1, written with it)."""
    row = conn.execute(
        "SELECT max(version) FROM draft_versions WHERE draft_id = %s", (draft_id,)
    ).fetchone()
    if row is None or row[0] is None:
        raise LookupError(f"draft {draft_id} has no version")  # a bug, not a 404
    return int(row[0])


def _expire(conn: Conn, now: datetime, draft_id: int, email_id: int) -> None:
    decide_draft(conn, draft_id, DraftStatus.EXPIRED, now)
    set_email(conn, email_id, EmailStatus.EXPIRED)
    write_audit(conn, now, "system", AuditEvent.DRAFT_EXPIRED, email_id=email_id, draft_id=draft_id)


def decide_draft(conn: Conn, draft_id: int, status: DraftStatus, now: datetime) -> None:
    conn.execute(
        "UPDATE drafts SET status = %s, decided_at = %s WHERE id = %s", (status, now, draft_id)
    )


def _lock_email(conn: Conn, email_id: int) -> str:
    row = conn.execute("SELECT from_address FROM emails WHERE id = %s", (email_id,)).fetchone()
    if row is None:
        raise NotFound(f"no email {email_id}")
    lock_sender(conn, row[0])
    status = conn.execute(
        "SELECT status FROM emails WHERE id = %s FOR UPDATE", (email_id,)
    ).fetchone()
    if status is None:  # cannot happen: emails are never deleted (D51)
        raise NotFound(f"no email {email_id}")
    return str(status[0])


def set_email(conn: Conn, email_id: int, status: EmailStatus) -> None:
    conn.execute("UPDATE emails SET status = %s WHERE id = %s", (status, email_id))


def _move_booking(
    conn: Conn,
    clock: Clock,
    booking_id: int,
    expected: BookingStatus,
    wanted: BookingStatus,
    event: AuditEvent,
    action: str,
) -> None:
    row = conn.execute(
        "SELECT status, email_id FROM bookings WHERE id = %s FOR UPDATE", (booking_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"no booking {booking_id}")
    status, email_id = row
    if status != expected:
        raise TransitionRefused(
            "booking", booking_id, status, wanted, action=action, email_id=email_id
        )
    conn.execute("UPDATE bookings SET status = %s WHERE id = %s", (wanted, booking_id))
    write_audit(
        conn, clock.now(), "owner", event, email_id=email_id, output={"booking_id": booking_id}
    )
