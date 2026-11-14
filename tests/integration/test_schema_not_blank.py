"""Migration 0007: not-blank checks refuse every kind of whitespace, not only spaces.

Before 0007 these used `btrim(x) <> ''`, which trims spaces only (docs/NOTES.md).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg.errors import CheckViolation
from psycopg.rows import TupleRow

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]
type ExpectViolation = Callable[[type[psycopg.Error], str], AbstractContextManager[None]]

BLANKS = pytest.mark.parametrize(
    "blank", ["   ", "\t", "\n", " \t\r\n "], ids=["spaces", "tab", "newline", "mixed"]
)


@BLANKS
def test_room_type_greek_name_not_blank(
    db: Conn, expect_violation: ExpectViolation, blank: str
) -> None:
    with expect_violation(CheckViolation, "room_types_name_el_not_blank"):
        db.execute("UPDATE room_types SET name_el = %s WHERE id = 'sea_view'", (blank,))


@BLANKS
def test_message_id_not_blank(rows: Rows, expect_violation: ExpectViolation, blank: str) -> None:
    with expect_violation(CheckViolation, "emails_message_id_not_blank"):
        rows.email(message_id=blank)


@BLANKS
def test_from_address_not_blank(rows: Rows, expect_violation: ExpectViolation, blank: str) -> None:
    with expect_violation(CheckViolation, "emails_from_address_not_blank"):
        rows.email(from_address=blank)


def test_values_with_surrounding_whitespace_still_accepted(db: Conn, rows: Rows) -> None:
    """Only all-whitespace is blank; trimming is the adapter's job, not a CHECK's."""
    rows.email(message_id=" <a@example.test>\n")
    db.execute("UPDATE room_types SET name_el = ' Θέα ' WHERE id = 'sea_view'")
