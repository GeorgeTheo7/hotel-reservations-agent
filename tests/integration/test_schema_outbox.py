"""Migration 0008, tables: outbox, bookings.draft_id, audit_log.

The two foreign keys to `drafts (.., status)` are DEFERRABLE INITIALLY DEFERRED:
checked at COMMIT. The `db` fixture never commits, so tests call
`rows.check_deferred_now(name)` to run the pending check on the spot.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, GeneratedAlways, UniqueViolation
from psycopg.rows import TupleRow

from anemos.audit import AuditEvent
from anemos.states import OutboxStatus

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]
type ExpectViolation = Callable[[type[psycopg.Error], str], AbstractContextManager[None]]

OUTBOX_FK = "outbox_draft_approved_fkey"
BOOKING_FK = "bookings_draft_approved_fkey"


# --- outbox: cross-rule 2 -----------------------------------------------------------


def test_outbox_row_for_an_approved_draft(rows: Rows) -> None:
    _, _, draft_id = rows.approved_draft()
    rows.outbox(draft_id)
    rows.check_deferred_now(OUTBOX_FK)


@pytest.mark.parametrize(
    "status", ["pending_approval", "rejected", "invalidated", "expired", "superseded"]
)
def test_no_outbox_row_unless_the_draft_is_approved(
    rows: Rows, expect_violation: ExpectViolation, status: str
) -> None:
    email_id = rows.email()
    decided_at = None if status == "pending_approval" else rows.T0
    draft_id = rows.draft(email_id, None, route="policy", status=status, decided_at=decided_at)
    with expect_violation(ForeignKeyViolation, OUTBOX_FK):
        rows.outbox(draft_id)
        rows.check_deferred_now(OUTBOX_FK)


def test_outbox_row_may_come_before_the_approval_in_one_transaction(db: Conn, rows: Rows) -> None:
    """D10 order: write the outbox row, then set the draft to approved; the FK is only
    checked at COMMIT (here: at check_deferred_now)."""
    email_id = rows.email()
    draft_id = rows.draft(email_id, None, route="policy")
    rows.outbox(draft_id)
    db.execute(
        "UPDATE drafts SET status = 'approved', decided_at = created_at WHERE id = %s",
        (draft_id,),
    )
    rows.check_deferred_now(OUTBOX_FK)


def test_exactly_one_outbox_row_per_draft(rows: Rows, expect_violation: ExpectViolation) -> None:
    """The double-send guard (states.md, Outbox)."""
    _, _, draft_id = rows.approved_draft()
    rows.outbox(draft_id)
    with expect_violation(UniqueViolation, "outbox_draft_id_key"):
        rows.outbox(draft_id)


def test_outbox_draft_status_cannot_be_written(db: Conn, rows: Rows) -> None:
    _, _, draft_id = rows.approved_draft()
    with pytest.raises(GeneratedAlways), db.transaction():
        rows.outbox(draft_id, draft_status="pending_approval")


# --- outbox: columns ----------------------------------------------------------------


@pytest.fixture
def approved(rows: Rows) -> int:
    return rows.approved_draft()[2]


def test_every_outbox_status_in_code_accepted(rows: Rows, approved: int) -> None:
    """The CHECK list and anemos.states.OutboxStatus agree (one draft per row)."""
    rows.outbox(approved, status=OutboxStatus.PENDING)
    for status in OutboxStatus:
        if status is not OutboxStatus.PENDING:
            _, _, draft_id = rows.approved_draft()
            rows.outbox(draft_id, status=status, sent_at=rows.T0, attempts=1)


def test_unknown_outbox_status_rejected(
    rows: Rows, expect_violation: ExpectViolation, approved: int
) -> None:
    with expect_violation(CheckViolation, "outbox_status_valid"):
        rows.outbox(approved, status="failed")


@pytest.mark.parametrize(
    "overrides",
    [{"status": "sent", "attempts": 1}, {"status": "pending", "sent_at": "2026-11-13T08:00Z"}],
    ids=["sent-without-time", "time-without-sent"],
)
def test_sent_at_exactly_when_sent(
    rows: Rows, expect_violation: ExpectViolation, approved: int, overrides: dict[str, Any]
) -> None:
    with expect_violation(CheckViolation, "outbox_sent_at_iff_sent"):
        rows.outbox(approved, **overrides)


def test_not_sent_before_created(
    rows: Rows, expect_violation: ExpectViolation, approved: int
) -> None:
    with expect_violation(CheckViolation, "outbox_sent_not_before_created"):
        rows.outbox(approved, status="sent", attempts=1, sent_at=rows.T0 - timedelta(seconds=1))


@pytest.mark.parametrize(
    "overrides",
    [{"attempts": -1}, {"status": "sent", "attempts": 0, "sent_at": "2026-11-13T08:00Z"}],
    ids=["negative", "sent-without-attempt"],
)
def test_attempts_valid(
    rows: Rows, expect_violation: ExpectViolation, approved: int, overrides: dict[str, Any]
) -> None:
    with expect_violation(CheckViolation, "outbox_attempts_valid"):
        rows.outbox(approved, **overrides)


def test_to_address_lowercase(rows: Rows, expect_violation: ExpectViolation, approved: int) -> None:
    with expect_violation(CheckViolation, "outbox_to_address_lowercase"):
        rows.outbox(approved, to_address="Guest@Example.test")


@pytest.mark.parametrize(
    ("column", "constraint"),
    [("to_address", "outbox_to_address_not_blank"), ("body", "outbox_body_not_blank")],
)
@pytest.mark.parametrize("blank", [" ", "\t", "\n"], ids=["space", "tab", "newline"])
def test_outbox_text_not_blank(
    rows: Rows,
    expect_violation: ExpectViolation,
    approved: int,
    column: str,
    constraint: str,
    blank: str,
) -> None:
    with expect_violation(CheckViolation, constraint):
        rows.outbox(approved, **{column: blank})


# --- bookings.draft_id: cross-rule 3 ------------------------------------------------


def test_booking_of_an_approved_draft(rows: Rows) -> None:
    email_id, request_id, draft_id = rows.approved_draft()
    rows.booking(email_id=email_id, request_id=request_id, draft_id=draft_id)
    rows.check_deferred_now(BOOKING_FK)


def test_fixture_booking_with_no_draft_still_allowed(rows: Rows) -> None:
    rows.booking()
    rows.check_deferred_now(BOOKING_FK)


def test_no_booking_for_a_pending_draft(rows: Rows, expect_violation: ExpectViolation) -> None:
    email_id = rows.email()
    request_id = rows.request(email_id)
    draft_id = rows.draft(email_id, request_id)
    with expect_violation(ForeignKeyViolation, BOOKING_FK):
        rows.booking(email_id=email_id, request_id=request_id, draft_id=draft_id)
        rows.check_deferred_now(BOOKING_FK)


def test_booking_with_request_needs_a_draft(rows: Rows, expect_violation: ExpectViolation) -> None:
    """MATCH FULL: draft and request together, or neither. Only approval creates
    bookings for a request, and approval always has a draft."""
    email_id, request_id, _ = rows.approved_draft()
    with expect_violation(ForeignKeyViolation, BOOKING_FK):
        rows.booking(email_id=email_id, request_id=request_id)
        rows.check_deferred_now(BOOKING_FK)


def test_booking_request_must_be_the_drafts(rows: Rows, expect_violation: ExpectViolation) -> None:
    _, _, draft_id = rows.approved_draft()
    other_email = rows.email()
    other_request = rows.request(other_email)
    with expect_violation(ForeignKeyViolation, BOOKING_FK):
        rows.booking(email_id=other_email, request_id=other_request, draft_id=draft_id)
        rows.check_deferred_now(BOOKING_FK)


def test_booking_may_come_before_the_approval_in_one_transaction(db: Conn, rows: Rows) -> None:
    email_id = rows.email()
    request_id = rows.request(email_id)
    draft_id = rows.draft(email_id, request_id)
    rows.booking(email_id=email_id, request_id=request_id, draft_id=draft_id)
    db.execute(
        "UPDATE drafts SET status = 'approved', decided_at = created_at WHERE id = %s",
        (draft_id,),
    )
    rows.check_deferred_now(BOOKING_FK)


def test_booking_draft_status_cannot_be_written(db: Conn, rows: Rows) -> None:
    with pytest.raises(GeneratedAlways), db.transaction():
        rows.booking(draft_status="approved")


# --- audit_log ------------------------------------------------------------------------


def test_minimal_audit_entry(rows: Rows) -> None:
    rows.audit()


def test_full_audit_entry(rows: Rows) -> None:
    _, _, draft_id = rows.approved_draft()
    email_id = rows.email()
    rows.audit(
        actor="agent",
        event="agent.tool_called",
        email_id=email_id,
        draft_id=draft_id,
        step="agent_loop",
        model="model-under-test",
        prompt_version="v1",
        input_tokens=812,
        output_tokens=96,
        cost_micro_usd=210,
        latency_ms=1450,
    )


def test_unknown_actor_rejected(rows: Rows, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "audit_log_actor_valid"):
        rows.audit(actor="model")


@pytest.mark.parametrize(
    "event", ["approved", "Draft.approved", "draft.", "draft approved", ".approved", "draft.v2"]
)
def test_event_must_be_area_dot_action(
    rows: Rows, expect_violation: ExpectViolation, event: str
) -> None:
    with expect_violation(CheckViolation, "audit_log_event_format"):
        rows.audit(event=event)


@pytest.mark.parametrize("event", ["draft.approved", "email.status_changed", "hold.flip.expired"])
def test_event_formats_accepted(rows: Rows, event: str) -> None:
    rows.audit(event=event)


@pytest.mark.parametrize(
    "column", ["input_tokens", "output_tokens", "cost_micro_usd", "latency_ms"]
)
def test_usage_not_negative(rows: Rows, expect_violation: ExpectViolation, column: str) -> None:
    with expect_violation(CheckViolation, "audit_log_usage_non_negative"):
        rows.audit(**{column: -1})


@pytest.mark.parametrize(
    ("column", "constraint"),
    [("email_id", "audit_log_email_id_fkey"), ("draft_id", "audit_log_draft_id_fkey")],
)
def test_audit_links_must_exist(
    rows: Rows, expect_violation: ExpectViolation, column: str, constraint: str
) -> None:
    with expect_violation(ForeignKeyViolation, constraint):
        rows.audit(**{column: 999_999_999})


def test_audit_log_is_indexed_by_email(db: Conn) -> None:
    row = db.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'audit_log_email_id_idx'"
    ).fetchone()
    assert row is not None
    assert row[0].endswith("USING btree (email_id)")


@pytest.mark.parametrize("event", list(AuditEvent), ids=str)
def test_every_audit_event_in_code_is_accepted(rows: Rows, event: AuditEvent) -> None:
    """Event names live in anemos.audit.AuditEvent; each must pass the format CHECK."""
    rows.audit(event=event.value)
