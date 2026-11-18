"""Holds page actions over HTTP (confirm, release, cancel) on committed_db, and the
mirror of the audit coverage guard: every API-only audit event is written by at least
one API test."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from anemos.audit import API_ONLY_EVENTS

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import ApiAuditCoverage, CommittedDb, OwnerApi

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)  # = Rows.T0
NOW = T0 + timedelta(hours=10)
TABLES = ("emails", "drafts", "bookings", "outbox", "audit_log")

# The one transition each action allows (states.md, Booking; D43, D46).
ALLOWED = {"confirm": ("hold", "confirmed"), "release": ("hold", "cancelled"),
           "cancel": ("confirmed", "cancelled")}  # fmt: skip
AUDITED_AS = {"confirm": "hold.confirmed", "release": "hold.released",
              "cancel": "booking.cancelled"}  # fmt: skip


@pytest.fixture
def api(owner_api: OwnerApi) -> Iterator[TestClient]:
    with owner_api(NOW) as client:
        yield client


def booking(committed_db: CommittedDb, status: str, *, expires_in: timedelta | None = None) -> int:
    """A booking of an approved draft (so it has an email), in the given status."""
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    email_id, request_id, draft_id = rows.approved_draft()
    expires_at = None
    if status in ("hold", "expired"):
        expires_at = NOW + (expires_in if expires_in is not None else timedelta(hours=48))
    booking_id = rows.booking(
        status=status, expires_at=expires_at, email_id=email_id, request_id=request_id,
        draft_id=draft_id,
    )  # fmt: skip
    conn.commit()
    return booking_id


def read(committed_db: CommittedDb, query: str, *params: Any) -> list[tuple[Any, ...]]:
    conn = committed_db.connect()
    found = conn.execute(query, params).fetchall()
    conn.commit()
    return found


def snapshot(committed_db: CommittedDb) -> dict[str, list[Any]]:
    conn = committed_db.connect()
    query = sql.SQL("SELECT to_jsonb(t)::text AS row FROM {} t ORDER BY row")
    found = {t: conn.execute(query.format(sql.Identifier(t))).fetchall() for t in TABLES}
    conn.commit()
    return found


def email_of(committed_db: CommittedDb, booking_id: int) -> int:
    [(email_id,)] = read(committed_db, "SELECT email_id FROM bookings WHERE id = %s", booking_id)
    return int(email_id)


# --- allowed ----------------------------------------------------------------------------


@pytest.mark.parametrize("action", list(ALLOWED))
def test_each_action_moves_its_booking(
    committed_db: CommittedDb, api: TestClient, action: str
) -> None:
    current, wanted = ALLOWED[action]
    booking_id = booking(committed_db, current)
    response = api.post(f"/api/bookings/{booking_id}/{action}")
    assert (response.status_code, response.json()) == (
        200, {"booking_id": booking_id, "status": wanted},
    )  # fmt: skip
    assert read(committed_db, "SELECT status FROM bookings WHERE id = %s", booking_id) == [
        (wanted,)
    ]
    assert read(committed_db, "SELECT actor, event, email_id, output FROM audit_log") == [
        ("owner", AUDITED_AS[action], email_of(committed_db, booking_id),
         {"booking_id": booking_id}),
    ]  # fmt: skip


def test_a_hold_past_its_expiry_can_still_be_confirmed(
    committed_db: CommittedDb, api: TestClient
) -> None:
    """D43: until a hold is flipped, the room has been kept for this guest. The Holds
    page shows it as past expiry; confirming it is allowed."""
    booking_id = booking(committed_db, "hold", expires_in=-timedelta(minutes=1))
    [hold] = api.get("/api/holds").json()
    assert (hold["id"], hold["past_expiry"], hold["status"]) == (booking_id, True, "hold")
    assert api.post(f"/api/bookings/{booking_id}/confirm").status_code == 200
    [after] = api.get("/api/holds").json()
    assert (after["status"], after["past_expiry"]) == ("confirmed", False)


# --- refused ----------------------------------------------------------------------------

REFUSED = [
    (action, status)
    for action, (allowed_from, _) in ALLOWED.items()
    for status in ("hold", "confirmed", "cancelled", "expired")
    if status != allowed_from
]


@pytest.mark.parametrize(("action", "status"), REFUSED, ids=[f"{a}-{s}" for a, s in REFUSED])
def test_refused_booking_actions_are_409_and_only_audited(
    committed_db: CommittedDb, api: TestClient, action: str, status: str
) -> None:
    """Includes release on a confirmed booking: a confirmed stay is cancelled, never
    "released" (D46)."""
    booking_id = booking(committed_db, status, expires_in=-timedelta(hours=1))
    wanted = ALLOWED[action][1]
    before = snapshot(committed_db)
    response = api.post(f"/api/bookings/{booking_id}/{action}")
    assert (response.status_code, response.json()) == (
        409, {"error": "transition_refused", "current": status, "wanted": wanted},
    )  # fmt: skip
    after = snapshot(committed_db)
    for table in TABLES[:-1]:
        assert after[table] == before[table], table
    assert read(committed_db, "SELECT actor, event, email_id, draft_id, output FROM audit_log") == [
        ("owner", "booking.action_refused", email_of(committed_db, booking_id), None,
         {"action": action, "reason": "transition_refused", "booking_id": booking_id,
          "current": status, "wanted": wanted}),
    ]  # fmt: skip


@pytest.mark.parametrize("action", list(ALLOWED))
def test_unknown_booking_is_404(committed_db: CommittedDb, api: TestClient, action: str) -> None:
    before = snapshot(committed_db)
    response = api.post(f"/api/bookings/999999/{action}")
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert snapshot(committed_db) == before


# --- the mirror of the audit coverage guard ---------------------------------------------


def test_every_api_only_event_is_written_by_an_api_test(api_audit: ApiAuditCoverage) -> None:
    """test_cross_rules skips audit.API_ONLY_EVENTS because only the API writes them.
    So that the exclusion cannot hide an untested event, each one must have been
    committed by at least one test in a test_api_*.py module during this run (the
    committed_db fixture records them). Moved to the end of the run by conftest; skips
    when not every API test ran (-k, or a single file), except in CI (GitHub sets
    CI=true), where a skip would silently switch the guard off."""
    if not api_audit.complete:
        if os.environ.get("CI") == "true":
            pytest.fail("in CI every API test must run, or the coverage guard is off")
        pytest.skip("not every API test ran, so coverage cannot be judged")
    missing = {event.value for event in API_ONLY_EVENTS} - api_audit.seen
    assert missing == set(), f"no API test writes {sorted(missing)}"
