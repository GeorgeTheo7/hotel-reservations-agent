"""Migration 0008, D51 triggers: final rows are frozen, some tables are write-once, and
the outbox text never changes. Also cross-rule 6 ("nothing changes a final status") at
the database level.

Triggers raise restrict_violation (23001) with the rule's name as constraint name, so
`expect_violation(RestrictViolation, "<rule>")` checks which rule fired.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import StrEnum
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import RestrictViolation
from psycopg.rows import TupleRow

from anemos.states import (
    FINAL_BOOKING_STATUSES,
    FINAL_DRAFT_STATUSES,
    FINAL_EMAIL_STATUSES,
    FINAL_OUTBOX_STATUSES,
    BookingStatus,
    DraftStatus,
    EmailStatus,
)

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]
type ExpectViolation = Callable[[type[psycopg.Error], str], AbstractContextManager[None]]


# --- the trigger lists agree with anemos.states -------------------------------------


@pytest.mark.parametrize(
    ("table", "final"),
    [
        ("emails", FINAL_EMAIL_STATUSES),
        ("drafts", FINAL_DRAFT_STATUSES),
        ("bookings", FINAL_BOOKING_STATUSES),
        ("outbox", FINAL_OUTBOX_STATUSES),
    ],
)
def test_trigger_final_statuses_equal_the_code(
    db: Conn, table: str, final: frozenset[StrEnum]
) -> None:
    """Same idea as the ReasonCode test: a final status added in code but not in the
    trigger (or the reverse) fails here. pg_trigger.tgargs holds the trigger's
    arguments as one byte string, each argument ending in a zero byte."""
    row = db.execute(
        "SELECT tgargs FROM pg_trigger WHERE tgname = %s AND tgrelid = %s::regclass",
        (f"{table}_final_status", table),
    ).fetchone()
    assert row is not None, f"no {table}_final_status trigger"
    args = {arg.decode() for arg in bytes(row[0]).split(b"\x00") if arg}
    assert args == {status.value for status in final}


@pytest.mark.parametrize(
    "table", ["emails", "drafts", "bookings", "outbox", "quotes", "draft_versions",
              "draft_offers", "audit_log"],
)  # fmt: skip
def test_triggers_fire_before_update_and_delete(db: Conn, table: str) -> None:
    """tgtype bits (pg_trigger docs): 2 = BEFORE, 8 = DELETE, 16 = UPDATE, 1 = ROW."""
    rows = db.execute(
        "SELECT tgname, tgtype FROM pg_trigger"
        " WHERE tgrelid = %s::regclass AND NOT tgisinternal"
        "   AND (tgname LIKE '%%_final_status' OR tgname LIKE '%%_write_once')"
        "   AND tgname <> 'outbox_text_write_once'",
        (table,),
    ).fetchall()
    assert len(rows) == 1
    tgtype = rows[0][1]
    assert tgtype & 1 and tgtype & 2 and tgtype & 8 and tgtype & 16


# --- final rows are frozen (cross-rule 6) -------------------------------------------


@pytest.mark.parametrize("status", sorted(FINAL_EMAIL_STATUSES))
@pytest.mark.parametrize("op", ["update", "delete"])
def test_final_email_frozen(
    db: Conn, rows: Rows, expect_violation: ExpectViolation, status: EmailStatus, op: str
) -> None:
    email_id = rows.email(status=status)
    with expect_violation(RestrictViolation, "emails_final_status"):
        if op == "update":
            db.execute("UPDATE emails SET subject = 'changed' WHERE id = %s", (email_id,))
        else:
            db.execute("DELETE FROM emails WHERE id = %s", (email_id,))


@pytest.mark.parametrize("status", sorted(set(EmailStatus) - FINAL_EMAIL_STATUSES))
def test_non_final_email_can_change(db: Conn, rows: Rows, status: EmailStatus) -> None:
    reasons = ["GROUP"] if status is EmailStatus.ESCALATED else []
    email_id = rows.email(status=status, reason_codes=reasons)
    db.execute("UPDATE emails SET subject = 'changed' WHERE id = %s", (email_id,))


@pytest.mark.parametrize("status", sorted(FINAL_DRAFT_STATUSES))
@pytest.mark.parametrize("op", ["update", "delete"])
def test_final_draft_frozen(
    db: Conn, rows: Rows, expect_violation: ExpectViolation, status: DraftStatus, op: str
) -> None:
    draft_id = rows.draft(rows.email(), None, route="policy", status=status, decided_at=rows.T0)
    with expect_violation(RestrictViolation, "drafts_final_status"):
        if op == "update":
            db.execute("UPDATE drafts SET route = 'ask_info' WHERE id = %s", (draft_id,))
        else:
            db.execute("DELETE FROM drafts WHERE id = %s", (draft_id,))


def test_pending_draft_can_be_decided(db: Conn, rows: Rows) -> None:
    draft_id = rows.draft(rows.email(), None, route="policy")
    db.execute(
        "UPDATE drafts SET status = 'rejected', decided_at = created_at WHERE id = %s",
        (draft_id,),
    )


@pytest.mark.parametrize("status", sorted(FINAL_BOOKING_STATUSES))
@pytest.mark.parametrize("op", ["update", "delete"])
def test_final_booking_frozen(
    db: Conn, rows: Rows, expect_violation: ExpectViolation, status: BookingStatus, op: str
) -> None:
    booking_id = rows.booking(status=status)
    with expect_violation(RestrictViolation, "bookings_final_status"):
        if op == "update":
            db.execute("UPDATE bookings SET status = 'confirmed' WHERE id = %s", (booking_id,))
        else:
            db.execute("DELETE FROM bookings WHERE id = %s", (booking_id,))


@pytest.mark.parametrize(
    ("start", "end"), [("hold", "confirmed"), ("hold", "cancelled"), ("hold", "expired"),
                       ("confirmed", "cancelled")],
)  # fmt: skip
def test_states_md_booking_transitions_allowed(db: Conn, rows: Rows, start: str, end: str) -> None:
    expires_at = rows.T0 if start == "hold" else None
    booking_id = rows.booking(status=start, expires_at=expires_at)
    db.execute("UPDATE bookings SET status = %s WHERE id = %s", (end, booking_id))


@pytest.mark.parametrize("op", ["update", "delete"])
def test_sent_outbox_row_frozen(
    db: Conn, rows: Rows, expect_violation: ExpectViolation, op: str
) -> None:
    _, _, draft_id = rows.approved_draft()
    outbox_id = rows.outbox(draft_id, status="sent", attempts=1, sent_at=rows.T0)
    with expect_violation(RestrictViolation, "outbox_final_status"):
        if op == "update":
            db.execute("UPDATE outbox SET attempts = 2 WHERE id = %s", (outbox_id,))
        else:
            db.execute("DELETE FROM outbox WHERE id = %s", (outbox_id,))


@pytest.mark.parametrize(
    ("column", "value"),
    [("attempts", 3), ("last_error", "timeout"), ("next_attempt_at", "2026-11-13T09:00Z")],
)
def test_pending_outbox_retry_columns_may_change(
    db: Conn, rows: Rows, column: str, value: object
) -> None:
    """Step 15: the sender updates these on a pending row; the D51 triggers allow it."""
    _, _, draft_id = rows.approved_draft()
    outbox_id = rows.outbox(draft_id)
    db.execute(
        sql.SQL("UPDATE outbox SET {} = %s WHERE id = %s").format(sql.Identifier(column)),
        (value, outbox_id),
    )


@pytest.mark.parametrize(
    ("column", "value"),
    [("attempts", 3), ("last_error", "timeout"), ("next_attempt_at", "2026-11-13T09:00Z")],
)
def test_sent_outbox_retry_columns_frozen(
    db: Conn, rows: Rows, expect_violation: ExpectViolation, column: str, value: object
) -> None:
    _, _, draft_id = rows.approved_draft()
    outbox_id = rows.outbox(draft_id, status="sent", attempts=1, sent_at=rows.T0)
    with expect_violation(RestrictViolation, "outbox_final_status"):
        db.execute(
            sql.SQL("UPDATE outbox SET {} = %s WHERE id = %s").format(sql.Identifier(column)),
            (value, outbox_id),
        )


def test_pending_outbox_row_can_retry_and_be_sent(db: Conn, rows: Rows) -> None:
    """states.md: a failed send stays pending with an attempt count and the last error."""
    _, _, draft_id = rows.approved_draft()
    outbox_id = rows.outbox(draft_id)
    db.execute("UPDATE outbox SET attempts = 1, last_error = 'timeout' WHERE id = %s", (outbox_id,))
    db.execute(
        "UPDATE outbox SET status = 'sent', attempts = 2, sent_at = %s WHERE id = %s",
        (rows.T0, outbox_id),
    )


# --- the outbox text never changes ------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("body", "Dear guest, a different offer."),
        ("subject", "Re: something else"),
        ("to_address", "other@example.test"),
        ("created_at", "2026-11-14T07:00Z"),
    ],
)
def test_pending_outbox_text_cannot_change(
    db: Conn, rows: Rows, expect_violation: ExpectViolation, column: str, value: str
) -> None:
    _, _, draft_id = rows.approved_draft()
    outbox_id = rows.outbox(draft_id)
    with expect_violation(RestrictViolation, "outbox_text_write_once"):
        db.execute(
            sql.SQL("UPDATE outbox SET {} = %s WHERE id = %s").format(sql.Identifier(column)),
            (value, outbox_id),
        )


def test_setting_the_same_text_again_is_not_a_change(db: Conn, rows: Rows) -> None:
    """IS DISTINCT FROM compares values: rewriting the identical body is allowed."""
    _, _, draft_id = rows.approved_draft()
    outbox_id = rows.outbox(draft_id)
    db.execute("UPDATE outbox SET body = body WHERE id = %s", (outbox_id,))


# --- write-once tables ------------------------------------------------------------


@pytest.fixture
def offered(rows: Rows) -> tuple[int, int, int]:
    """(draft id, quote id, request id): a draft with version 1 and one offer."""
    email_id = rows.email()
    request_id = rows.request(email_id)
    draft_id = rows.draft(email_id, request_id)
    quote_id = rows.quote(request_id)
    rows.version(draft_id, 1)
    rows.offer(draft_id, request_id, quote_id, hold=True)
    return draft_id, quote_id, request_id


@pytest.mark.parametrize(
    ("table", "update", "where"),
    [
        ("quotes", "deposit_cents = 1", "id = %(quote)s"),
        ("draft_versions", "body = 'edited in place'", "draft_id = %(draft)s"),
        ("draft_offers", "hold = false", "draft_id = %(draft)s"),
    ],
)
@pytest.mark.parametrize("op", ["update", "delete"])
def test_write_once_tables(
    db: Conn,
    expect_violation: ExpectViolation,
    offered: tuple[int, int, int],
    table: str,
    update: str,
    where: str,
    op: str,
) -> None:
    draft_id, quote_id, _ = offered
    statement = (
        f"UPDATE {table} SET {update} WHERE {where}"
        if op == "update"
        else f"DELETE FROM {table} WHERE {where}"
    )
    with expect_violation(RestrictViolation, f"{table}_write_once"):
        db.execute(statement, {"draft": draft_id, "quote": quote_id})


@pytest.mark.parametrize("op", ["update", "delete"])
def test_audit_log_write_once(
    db: Conn, rows: Rows, expect_violation: ExpectViolation, op: str
) -> None:
    entry = rows.audit()
    with expect_violation(RestrictViolation, "audit_log_write_once"):
        if op == "update":
            db.execute("UPDATE audit_log SET actor = 'agent' WHERE id = %s", (entry,))
        else:
            db.execute("DELETE FROM audit_log WHERE id = %s", (entry,))


def test_owner_edit_adds_a_version_instead(rows: Rows, offered: tuple[int, int, int]) -> None:
    """D41: an edit is a new row, which write-once allows."""
    draft_id, _, _ = offered
    rows.version(draft_id, 2, body="Dear guest, edited by the owner. {{QUOTE_BLOCK}}")
