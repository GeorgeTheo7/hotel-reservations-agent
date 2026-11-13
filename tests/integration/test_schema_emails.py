"""Constraints of migration 0005 (emails, requests, bookings foreign keys).

Statuses and reason codes come from docs/states.md and the spec (D49). Each violation
runs in a savepoint (`expect_violation`), so one test can try several bad rows.
"""

import itertools
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, NotNullViolation, UniqueViolation
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.reservations.errors import ReasonCode

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]
type ExpectViolation = Callable[[type[psycopg.Error], str], AbstractContextManager[None]]

T0 = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)
STAY = "[2027-07-12,2027-07-15)"

# docs/states.md, Email.
EMAIL_STATUSES = [
    "received", "processing", "set_aside", "escalated", "awaiting_approval",
    "answered", "expired", "superseded", "closed",
]  # fmt: skip

_message_ids = itertools.count(1)


def insert_email(db: Conn, **overrides: Any) -> int:
    row: dict[str, Any] = {
        "message_id": f"<test-{next(_message_ids)}@example.test>",
        "from_address": "guest@example.test",
        "subject": "Room in July",
        "raw_body": "Hello, do you have a room?",
        "clean_body": "Hello, do you have a room?",
        "received_at": T0,
        "created_at": T0,
        "status": "received",
    } | overrides
    columns = ", ".join(row)
    placeholders = ", ".join(f"%({name})s" for name in row)
    result = db.execute(
        f"INSERT INTO emails ({columns}) VALUES ({placeholders}) RETURNING id",
        row,
    ).fetchone()
    assert result is not None
    return int(result[0])


def insert_request(db: Conn, email_id: int, **overrides: Any) -> int:
    row: dict[str, Any] = {
        "email_id": email_id,
        "extracted": Jsonb({"date_spec": {"kind": "exact"}}),
        "created_at": T0,
    } | overrides
    columns = ", ".join(row)
    placeholders = ", ".join(f"%({name})s" for name in row)
    result = db.execute(
        f"INSERT INTO requests ({columns}) VALUES ({placeholders}) RETURNING id",
        row,
    ).fetchone()
    assert result is not None
    return int(result[0])


def insert_booking(db: Conn, *, email_id: int | None, request_id: int | None) -> None:
    db.execute(
        "INSERT INTO bookings (room_id, stay, status, price_cents, created_at,"
        " email_id, request_id)"
        " VALUES ((SELECT min(id) FROM rooms), %s::daterange, 'confirmed', 42000, %s, %s, %s)",
        (STAY, T0, email_id, request_id),
    )


# --- emails: basics ---------------------------------------------------------------------


def test_minimal_email_is_stored_without_reasons(db: Conn) -> None:
    email_id = insert_email(db)
    row = db.execute(
        "SELECT status, reason_codes, language, mail_thread_id FROM emails WHERE id = %s",
        (email_id,),
    ).fetchone()
    assert row == ("received", [], None, None)


def test_same_message_id_twice_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    """D4: the database itself makes storing an email idempotent on message_id."""
    insert_email(db, message_id="<once@example.test>")
    with expect_violation(UniqueViolation, "emails_message_id_key"):
        insert_email(db, message_id="<once@example.test>")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_message_id_rejected(db: Conn, expect_violation: ExpectViolation, blank: str) -> None:
    with expect_violation(CheckViolation, "emails_message_id_not_blank"):
        insert_email(db, message_id=blank)


def test_blank_from_address_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "emails_from_address_not_blank"):
        insert_email(db, from_address=" ")


def test_from_address_must_be_lowercase(db: Conn, expect_violation: ExpectViolation) -> None:
    """D48 compares addresses with =; one spelling per address."""
    with expect_violation(CheckViolation, "emails_from_address_lowercase"):
        insert_email(db, from_address="Guest@Example.test")


@pytest.mark.parametrize("language", ["EN", "eng", "e", "el-GR"])
def test_language_must_be_two_lowercase_letters(
    db: Conn, expect_violation: ExpectViolation, language: str
) -> None:
    with expect_violation(CheckViolation, "emails_language_iso"):
        insert_email(db, language=language)


@pytest.mark.parametrize("language", ["en", "el", "de"])
def test_language_codes_accepted(db: Conn, language: str) -> None:
    insert_email(db, language=language)


def test_created_at_is_required(db: Conn) -> None:
    """No DEFAULT now(): the code passes the injected clock's time (D20)."""
    with pytest.raises(NotNullViolation) as caught, db.transaction():
        insert_email(db, created_at=None)
    assert caught.value.diag.column_name == "created_at"


# --- emails: status and reason codes ------------------------------------------------


@pytest.mark.parametrize("status", [s for s in EMAIL_STATUSES if s != "escalated"])
def test_every_states_md_status_accepted(db: Conn, status: str) -> None:
    insert_email(db, status=status)


def test_escalated_accepted_with_a_reason(db: Conn) -> None:
    insert_email(db, status="escalated", reason_codes=["GROUP"])


@pytest.mark.parametrize("status", ["new", "sent", "approved", "ESCALATED", ""])
def test_unknown_status_rejected(db: Conn, expect_violation: ExpectViolation, status: str) -> None:
    with expect_violation(CheckViolation, "emails_status_valid"):
        insert_email(db, status=status)


def test_every_reason_code_in_the_enum_is_accepted(db: Conn) -> None:
    """Keeps the SQL list and anemos.reservations.errors.ReasonCode from drifting apart:
    a code added to the enum but not to the CHECK fails here."""
    insert_email(db, status="escalated", reason_codes=[code.value for code in ReasonCode])


def test_the_three_d49_codes_exist() -> None:
    assert {"NO_LONGER_AVAILABLE", "OWNER_REOPENED", "SYSTEM_ERROR"} <= set(ReasonCode)


@pytest.mark.parametrize(
    "codes",
    [["UNKNOWN"], ["group"], ["GROUP", "SPAM"], ["GROUP", None]],
    ids=["unknown", "lowercase", "one-bad-of-two", "null-element"],
)
def test_reason_code_outside_the_spec_rejected(
    db: Conn, expect_violation: ExpectViolation, codes: list[str | None]
) -> None:
    with expect_violation(CheckViolation, "emails_reason_codes_valid"):
        insert_email(db, status="escalated", reason_codes=codes)


def test_escalated_without_reason_rejected(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "emails_escalated_has_reason"):
        insert_email(db, status="escalated")


@pytest.mark.parametrize("status", ["received", "awaiting_approval", "answered", "set_aside"])
def test_reasons_on_a_non_escalated_email_rejected(
    db: Conn, expect_violation: ExpectViolation, status: str
) -> None:
    with expect_violation(CheckViolation, "emails_reasons_only_if_escalated"):
        insert_email(db, status=status, reason_codes=["GROUP"])


def test_reasons_are_kept_when_an_escalated_email_is_closed(db: Conn) -> None:
    email_id = insert_email(db, status="escalated", reason_codes=["COMPLAINT"])
    db.execute("UPDATE emails SET status = 'closed' WHERE id = %s", (email_id,))


def test_a_closed_email_may_have_no_reasons(db: Conn) -> None:
    """Rejecting a draft closes its email (D40) without escalating it."""
    insert_email(db, status="closed")


# --- requests -------------------------------------------------------------------------


def test_full_request_accepted(db: Conn) -> None:
    insert_request(
        db,
        insert_email(db),
        stay=STAY,
        adults=2,
        children=2,
        child_ages=[0, 17],
        rooms=1,
        room_type_id="sea_view",
    )


def test_one_request_per_email(db: Conn, expect_violation: ExpectViolation) -> None:
    email_id = insert_email(db)
    insert_request(db, email_id)
    with expect_violation(UniqueViolation, "requests_email_id_key"):
        insert_request(db, email_id)


def test_request_needs_an_existing_email(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(ForeignKeyViolation, "requests_email_id_fkey"):
        insert_request(db, 999_999_999)


def test_request_needs_an_existing_room_type(db: Conn, expect_violation: ExpectViolation) -> None:
    with expect_violation(ForeignKeyViolation, "requests_room_type_id_fkey"):
        insert_request(db, insert_email(db), room_type_id="penthouse")


def test_an_email_with_a_request_cannot_be_deleted(
    db: Conn, expect_violation: ExpectViolation
) -> None:
    email_id = insert_email(db)
    insert_request(db, email_id)
    with expect_violation(ForeignKeyViolation, "requests_email_id_fkey"):
        db.execute("DELETE FROM emails WHERE id = %s", (email_id,))


@pytest.mark.parametrize(
    "stay", ["[2027-07-12,2027-07-12)", "[2027-07-12,)", "(,2027-07-15)"],
    ids=["empty", "no-check-out", "no-check-in"],
)  # fmt: skip
def test_stay_must_be_bounded_and_not_empty(
    db: Conn, expect_violation: ExpectViolation, stay: str
) -> None:
    with expect_violation(CheckViolation, "requests_stay_bounded"):
        insert_request(db, insert_email(db), stay=stay)


def test_unresolved_request_has_null_fields(db: Conn) -> None:
    """An email escalated or answered with NEEDS_INFO still stores what was extracted."""
    insert_request(db, insert_email(db))


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("adults", -1, "requests_adults_non_negative"),
        ("rooms", 0, "requests_rooms_positive"),
    ],
)
def test_counts_out_of_range_rejected(
    db: Conn, expect_violation: ExpectViolation, column: str, value: int, constraint: str
) -> None:
    with expect_violation(CheckViolation, constraint):
        insert_request(db, insert_email(db), **{column: value})


def test_negative_children_rejected(db: Conn) -> None:
    """Two CHECKs fail here (0 ages <= -1 children is false too), so
    requests_children_non_negative can never be the only one that fires."""
    with pytest.raises(CheckViolation) as caught, db.transaction():
        insert_request(db, insert_email(db), children=-1)
    assert caught.value.diag.constraint_name in {
        "requests_children_non_negative",
        "requests_child_ages_not_more_than_children",
    }


@pytest.mark.parametrize(
    "ages", [[18], [-1], [5, None]], ids=["18", "negative", "null-element"]
)  # fmt: skip
def test_child_age_outside_0_to_17_rejected(
    db: Conn, expect_violation: ExpectViolation, ages: list[int | None]
) -> None:
    """Same bounds as model.Child."""
    with expect_violation(CheckViolation, "requests_child_ages_valid"):
        insert_request(db, insert_email(db), children=3, child_ages=ages)


def test_ages_may_be_missing(db: Conn) -> None:
    """'Two children' without ages: stored, then the guest is asked (Scope)."""
    insert_request(db, insert_email(db), children=2, child_ages=[])


@pytest.mark.parametrize(("children", "ages"), [(1, [4, 6]), (None, [4])])
def test_more_ages_than_children_rejected(
    db: Conn, expect_violation: ExpectViolation, children: int | None, ages: list[int]
) -> None:
    with expect_violation(CheckViolation, "requests_child_ages_not_more_than_children"):
        insert_request(db, insert_email(db), children=children, child_ages=ages)


# --- bookings: foreign keys promised in week 1 ----------------------------------------


def test_booking_with_its_email_and_request_accepted(db: Conn) -> None:
    email_id = insert_email(db)
    insert_booking(db, email_id=email_id, request_id=insert_request(db, email_id))


def test_booking_with_neither_accepted(db: Conn) -> None:
    """Test fixtures (and week-1 tests) create bookings with no email."""
    insert_booking(db, email_id=None, request_id=None)


def test_booking_with_request_but_no_email_rejected(
    db: Conn, expect_violation: ExpectViolation
) -> None:
    request_id = insert_request(db, insert_email(db))
    with expect_violation(ForeignKeyViolation, "bookings_request_of_email_fkey"):
        insert_booking(db, email_id=None, request_id=request_id)


def test_booking_with_email_but_no_request_rejected(
    db: Conn, expect_violation: ExpectViolation
) -> None:
    with expect_violation(ForeignKeyViolation, "bookings_request_of_email_fkey"):
        insert_booking(db, email_id=insert_email(db), request_id=None)


def test_booking_request_must_belong_to_its_email(
    db: Conn, expect_violation: ExpectViolation
) -> None:
    first, second = insert_email(db), insert_email(db)
    request_of_first = insert_request(db, first)
    insert_request(db, second)
    with expect_violation(ForeignKeyViolation, "bookings_request_of_email_fkey"):
        insert_booking(db, email_id=second, request_id=request_of_first)


def test_booking_with_unknown_email_and_request_rejected(
    db: Conn, expect_violation: ExpectViolation
) -> None:
    """The one two-column key covers unknown ids too; no one-column keys are needed."""
    with expect_violation(ForeignKeyViolation, "bookings_request_of_email_fkey"):
        insert_booking(db, email_id=999_999_999, request_id=999_999_999)


def test_bookings_have_only_the_two_column_foreign_key_to_requests(db: Conn) -> None:
    rows = db.execute(
        "SELECT conname FROM pg_constraint"
        " WHERE conrelid = 'bookings'::regclass AND contype = 'f' ORDER BY 1"
    ).fetchall()
    assert rows == [("bookings_request_of_email_fkey",), ("bookings_room_id_fkey",)]
