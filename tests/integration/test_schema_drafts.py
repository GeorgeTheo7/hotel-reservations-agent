"""Constraints of migration 0006 (quotes, drafts, draft_versions, draft_offers).

Statuses come from docs/states.md (Draft). Rows are built by the `rows` fixture with
valid defaults; each test overrides only the column under test.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    GeneratedAlways,
    UniqueViolation,
)
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]
type ExpectViolation = Callable[[type[psycopg.Error], str], AbstractContextManager[None]]

# docs/states.md, Draft.
DRAFT_STATUSES = [
    "pending_approval", "approved", "rejected", "expired", "superseded", "invalidated",
]  # fmt: skip
QUOTE_CHANGES = [
    "open_part", "min_stay", "other_type", "more_rooms", "other_dates", "window_option",
]  # fmt: skip


def hold_allowed(db: Conn, quote_id: int) -> bool:
    row = db.execute("SELECT hold_allowed FROM quotes WHERE id = %s", (quote_id,)).fetchone()
    assert row is not None
    return bool(row[0])


# --- quotes -----------------------------------------------------------------------------


def test_exact_quote_may_be_held(db: Conn, rows: Rows) -> None:
    assert hold_allowed(db, rows.quote(rows.request())) is True


@pytest.mark.parametrize("change", QUOTE_CHANGES)
def test_any_change_means_no_hold(db: Conn, rows: Rows, change: str) -> None:
    """D9: hold_allowed is derived from `changes`, so the two cannot disagree."""
    assert hold_allowed(db, rows.quote(rows.request(), changes=[change])) is False


def test_changes_combine(db: Conn, rows: Rows) -> None:
    """D34: an open part can need its min-stay version too."""
    quote_id = rows.quote(rows.request(), changes=["open_part", "min_stay"])
    assert hold_allowed(db, quote_id) is False


def test_hold_allowed_cannot_be_written(db: Conn, rows: Rows) -> None:
    with pytest.raises(GeneratedAlways), db.transaction():
        rows.quote(rows.request(), hold_allowed=True)


@pytest.mark.parametrize(
    "changes", [["upgrade"], ["MIN_STAY"], ["min_stay", None]],
    ids=["unknown", "uppercase", "null-element"],
)  # fmt: skip
def test_unknown_change_rejected(
    rows: Rows, expect_violation: ExpectViolation, changes: list[str | None]
) -> None:
    with expect_violation(CheckViolation, "quotes_changes_valid"):
        rows.quote(rows.request(), changes=changes)


def test_quote_needs_an_existing_request(rows: Rows, expect_violation: ExpectViolation) -> None:
    with expect_violation(ForeignKeyViolation, "quotes_request_id_fkey"):
        rows.quote(999_999_999)


def test_quote_needs_an_existing_room_type(rows: Rows, expect_violation: ExpectViolation) -> None:
    with expect_violation(ForeignKeyViolation, "quotes_room_type_id_fkey"):
        rows.quote(rows.request(), room_type_id="penthouse")


@pytest.mark.parametrize("stay", ["[2027-07-12,2027-07-12)", "[2027-07-12,)"])
def test_quote_stay_must_be_bounded(
    rows: Rows, expect_violation: ExpectViolation, stay: str
) -> None:
    with expect_violation(CheckViolation, "quotes_stay_bounded"):
        rows.quote(rows.request(), stay=stay)


@pytest.mark.parametrize("rooms", [0, 3])
def test_quote_has_one_or_two_rooms(
    rows: Rows, expect_violation: ExpectViolation, rooms: int
) -> None:
    """D5: at most 2 rooms; 3+ is GROUP and never priced."""
    with expect_violation(CheckViolation, "quotes_rooms_valid"):
        rows.quote(rows.request(), rooms=rooms)


@pytest.mark.parametrize(
    "lines",
    [[{}, {}], [{}, {}, {}, {}], {"night": "2027-07-12"}],
    ids=["missing-night", "extra-night", "not-an-array"],
)
def test_one_line_per_night(rows: Rows, expect_violation: ExpectViolation, lines: Any) -> None:
    with expect_violation(CheckViolation, "quotes_one_line_per_night"):
        rows.quote(rows.request(), lines=Jsonb(lines))


def test_unknown_extra_bed_band_rejected(rows: Rows, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "quotes_extra_bed_bands_valid"):
        rows.quote(rows.request(), extra_bed_bands=["infant"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"room_subtotal_cents": 0, "accommodation_subtotal_cents": 0},
        {"extra_bed_subtotal_cents": -1, "accommodation_subtotal_cents": 38999},
        {"city_tax_cents": -1},
        {"deposit_cents": 0},
    ],
    ids=["no-room-price", "negative-extra-beds", "negative-city-tax", "no-deposit"],
)
def test_amounts_out_of_range_rejected(
    rows: Rows, expect_violation: ExpectViolation, overrides: dict[str, int]
) -> None:
    with expect_violation(CheckViolation, "quotes_amounts_valid"):
        rows.quote(rows.request(), **overrides)


def test_accommodation_is_rooms_plus_extra_beds(
    rows: Rows, expect_violation: ExpectViolation
) -> None:
    """D24. The deposit formula (D14) is checked in code only."""
    with expect_violation(CheckViolation, "quotes_accommodation_is_rooms_plus_extra_beds"):
        rows.quote(rows.request(), extra_bed_subtotal_cents=2000)


# --- drafts -----------------------------------------------------------------------------


def test_draft_without_request_accepted(rows: Rows) -> None:
    """A policy answer has no request; MATCH SIMPLE skips the request check."""
    rows.draft(rows.email(), None, route="policy")


def test_one_draft_per_email(rows: Rows, expect_violation: ExpectViolation) -> None:
    email_id = rows.email()
    request_id = rows.request(email_id)
    rows.draft(email_id, request_id)
    with expect_violation(UniqueViolation, "drafts_email_id_key"):
        rows.draft(email_id, request_id)


def test_draft_needs_an_existing_email(rows: Rows, expect_violation: ExpectViolation) -> None:
    with expect_violation(ForeignKeyViolation, "drafts_email_id_fkey"):
        rows.draft(999_999_999, None)


def test_draft_request_must_belong_to_its_email(
    rows: Rows, expect_violation: ExpectViolation
) -> None:
    other_request = rows.request()
    with expect_violation(ForeignKeyViolation, "drafts_request_of_email_fkey"):
        rows.draft(rows.email(), other_request)


def test_unknown_route_rejected(rows: Rows, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "drafts_route_valid"):
        rows.draft(rows.email(), None, route="escalate")


@pytest.mark.parametrize("status", DRAFT_STATUSES)
def test_every_states_md_status_accepted(rows: Rows, status: str) -> None:
    decided_at = None if status == "pending_approval" else rows.T0 + timedelta(hours=1)
    rows.draft(rows.email(), None, status=status, decided_at=decided_at)


@pytest.mark.parametrize("status", ["sent", "pending", "draft", ""])
def test_unknown_draft_status_rejected(
    rows: Rows, expect_violation: ExpectViolation, status: str
) -> None:
    with expect_violation(CheckViolation, "drafts_status_valid"):
        rows.draft(rows.email(), None, status=status, decided_at=rows.T0)


def test_pending_draft_has_no_decision_time(rows: Rows, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "drafts_decided_iff_not_pending"):
        rows.draft(rows.email(), None, decided_at=rows.T0)


def test_decided_draft_has_a_decision_time(rows: Rows, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "drafts_decided_iff_not_pending"):
        rows.draft(rows.email(), None, status="approved")


def test_born_superseded_draft_is_decided_at_creation(rows: Rows) -> None:
    """D48: decided_at = created_at is allowed."""
    rows.draft(rows.email(), None, status="superseded", decided_at=rows.T0)


def test_decision_not_before_creation(rows: Rows, expect_violation: ExpectViolation) -> None:
    with expect_violation(CheckViolation, "drafts_decided_not_before_created"):
        rows.draft(rows.email(), None, status="rejected", decided_at=rows.T0 - timedelta(seconds=1))


# --- draft_versions ---------------------------------------------------------------------


@pytest.fixture
def draft_id(rows: Rows) -> int:
    return rows.draft(rows.email(), None, route="policy")


def test_agent_version_then_owner_edits(rows: Rows, draft_id: int) -> None:
    rows.version(draft_id, 1)
    rows.version(draft_id, 2)
    rows.version(draft_id, 3)


def test_version_numbers_are_unique_per_draft(
    rows: Rows, expect_violation: ExpectViolation, draft_id: int
) -> None:
    rows.version(draft_id, 1)
    with expect_violation(UniqueViolation, "draft_versions_pkey"):
        rows.version(draft_id, 1)


@pytest.mark.parametrize(("version", "author"), [(1, "owner"), (2, "agent")])
def test_version_1_is_the_agents_and_only_version_1(
    rows: Rows, expect_violation: ExpectViolation, draft_id: int, version: int, author: str
) -> None:
    with expect_violation(CheckViolation, "draft_versions_agent_writes_version_1"):
        rows.version(draft_id, version, author=author)


def test_version_must_be_positive(
    rows: Rows, expect_violation: ExpectViolation, draft_id: int
) -> None:
    # author 'owner' so only the positive check fails, not the version-1 rule.
    with expect_violation(CheckViolation, "draft_versions_version_positive"):
        rows.version(draft_id, 0, author="owner")


def test_unknown_author_rejected(
    rows: Rows, expect_violation: ExpectViolation, draft_id: int
) -> None:
    with expect_violation(CheckViolation, "draft_versions_author_valid"):
        rows.version(draft_id, 2, author="system")


def test_blank_body_rejected(rows: Rows, expect_violation: ExpectViolation, draft_id: int) -> None:
    with expect_violation(CheckViolation, "draft_versions_body_not_blank"):
        rows.version(draft_id, 1, body="  \n ")


def test_guardrail_results_are_an_object(
    rows: Rows, expect_violation: ExpectViolation, draft_id: int
) -> None:
    with expect_violation(CheckViolation, "draft_versions_guardrail_results_object"):
        rows.version(draft_id, 1, guardrail_results=Jsonb(["quote_block_once"]))


def test_version_needs_an_existing_draft(rows: Rows, expect_violation: ExpectViolation) -> None:
    with expect_violation(ForeignKeyViolation, "draft_versions_draft_id_fkey"):
        rows.version(999_999_999, 1)


# --- draft_offers -----------------------------------------------------------------------


@pytest.fixture
def quote_draft(rows: Rows) -> tuple[int, int]:
    """(draft id, request id) of a quote-route draft."""
    email_id = rows.email()
    request_id = rows.request(email_id)
    return rows.draft(email_id, request_id), request_id


def test_exact_quote_held(rows: Rows, quote_draft: tuple[int, int]) -> None:
    draft_id, request_id = quote_draft
    rows.offer(draft_id, request_id, rows.quote(request_id), hold=True)


def test_several_options_offered_without_hold(rows: Rows, quote_draft: tuple[int, int]) -> None:
    draft_id, request_id = quote_draft
    for check_in in ("2027-07-12", "2027-07-14", "2027-07-16"):
        stay = f"[{check_in},{check_in[:-2]}{int(check_in[-2:]) + 3})"
        quote_id = rows.quote(request_id, stay=stay, changes=["window_option"])
        rows.offer(draft_id, request_id, quote_id, hold=False)


def test_changed_quote_cannot_be_held(
    rows: Rows, expect_violation: ExpectViolation, quote_draft: tuple[int, int]
) -> None:
    """D9 / guardrail 7 / cross-rule 4, in the database."""
    draft_id, request_id = quote_draft
    quote_id = rows.quote(request_id, changes=["min_stay"])
    with expect_violation(CheckViolation, "draft_offers_hold_only_if_allowed"):
        rows.offer(draft_id, request_id, quote_id, hold=True)


def test_copied_hold_allowed_must_match_the_quote(
    rows: Rows, expect_violation: ExpectViolation, quote_draft: tuple[int, int]
) -> None:
    """A lying copy of hold_allowed cannot sneak a hold past the CHECK."""
    draft_id, request_id = quote_draft
    quote_id = rows.quote(request_id, changes=["other_type"])
    with expect_violation(ForeignKeyViolation, "draft_offers_quote_fkey"):
        rows.offer(draft_id, request_id, quote_id, hold=True, quote_hold_allowed=True)


def test_draft_cannot_offer_another_requests_quote(
    rows: Rows, expect_violation: ExpectViolation, quote_draft: tuple[int, int]
) -> None:
    """Guardrail 7: proposed holds come from this run's quotes only."""
    draft_id, request_id = quote_draft
    foreign_quote = rows.quote(rows.request())
    with expect_violation(ForeignKeyViolation, "draft_offers_quote_fkey"):
        rows.offer(draft_id, request_id, foreign_quote, hold=True)


def test_offer_request_must_be_the_drafts(
    rows: Rows, expect_violation: ExpectViolation, quote_draft: tuple[int, int]
) -> None:
    draft_id, _ = quote_draft
    other_request = rows.request()
    with expect_violation(ForeignKeyViolation, "draft_offers_draft_fkey"):
        rows.offer(draft_id, other_request, rows.quote(other_request), hold=False)


def test_draft_without_request_offers_nothing(
    rows: Rows, expect_violation: ExpectViolation
) -> None:
    draft_id = rows.draft(rows.email(), None, route="policy")
    request_id = rows.request()
    with expect_violation(ForeignKeyViolation, "draft_offers_draft_fkey"):
        rows.offer(draft_id, request_id, rows.quote(request_id), hold=False)


def test_at_most_one_held_offer_per_draft(
    rows: Rows, expect_violation: ExpectViolation, quote_draft: tuple[int, int]
) -> None:
    draft_id, request_id = quote_draft
    rows.offer(draft_id, request_id, rows.quote(request_id), hold=True)
    with expect_violation(UniqueViolation, "draft_offers_one_hold_per_draft"):
        rows.offer(draft_id, request_id, rows.quote(request_id), hold=True)


def test_same_quote_offered_once(
    rows: Rows, expect_violation: ExpectViolation, quote_draft: tuple[int, int]
) -> None:
    draft_id, request_id = quote_draft
    quote_id = rows.quote(request_id)
    rows.offer(draft_id, request_id, quote_id, hold=False)
    with expect_violation(UniqueViolation, "draft_offers_pkey"):
        rows.offer(draft_id, request_id, quote_id, hold=False)
