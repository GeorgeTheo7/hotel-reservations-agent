"""Draft and email actions over HTTP, on committed_db (each request commits for real).

Checked for every action: the status code and body, what was committed (read back on a
new connection), and for refusals that nothing changed except one audit row written
after the rollback.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import TupleRow

from anemos.api import create_app
from anemos.clock import FixedClock
from anemos.reservations.loader import load_hotel_data
from anemos.stub_drafter import Drafted, draft_reply

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

RECEIVED = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)
NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
PAST_72H = NOW + timedelta(hours=72, seconds=1)
JUNE = "[2027-06-01,2027-06-04)"
SEA_VIEW = (201, 202, 203, 204)
TABLES = ("emails", "drafts", "draft_versions", "bookings", "outbox", "audit_log")

type Client = Callable[..., TestClient]


@pytest.fixture
def client(committed_db: CommittedDb) -> Iterator[Client]:
    """client() at NOW, client(at) at another time: a separate app per clock."""
    opened: list[TestClient] = []

    def make(at: datetime = NOW) -> TestClient:
        test_client = TestClient(create_app(committed_db.connect, FixedClock(at)))
        opened.append(test_client)
        return test_client

    yield make
    for test_client in opened:
        test_client.close()


def drafted(committed_db: CommittedDb, sender: str = "guest@example.test") -> tuple[int, int]:
    """(email id, draft id): a committed stub draft, sea view 1-4 June, held offer."""
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    email_id = rows.email(from_address=sender, received_at=RECEIVED, message_id=f"<{sender}>")
    rows.request(email_id, stay=JUNE, adults=2, room_type_id="sea_view")
    result = draft_reply(conn, load_hotel_data(conn), FixedClock(NOW), email_id)
    assert isinstance(result, Drafted), result
    conn.commit()
    return email_id, result.draft_id


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


def audit_rows(committed_db: CommittedDb) -> list[tuple[Any, ...]]:
    return read(
        committed_db, "SELECT actor, event, email_id, draft_id, output FROM audit_log ORDER BY id"
    )


def only_audit_added(
    committed_db: CommittedDb, before: dict[str, list[Any]], audit_before: int
) -> list[tuple[Any, ...]]:
    """Assert nothing but audit_log changed; return the audit rows added."""
    after = snapshot(committed_db)
    for table in TABLES[:-1]:
        assert after[table] == before[table], table
    return audit_rows(committed_db)[audit_before:]


def latest_body(committed_db: CommittedDb, draft_id: int) -> str:
    [(body,)] = read(
        committed_db,
        "SELECT body FROM draft_versions WHERE draft_id = %s ORDER BY version DESC LIMIT 1",
        draft_id,
    )
    return str(body)


# --- edit -------------------------------------------------------------------------------


def test_edit_adds_an_owner_version(committed_db: CommittedDb, client: Client) -> None:
    _, draft_id = drafted(committed_db)
    friendlier = latest_body(committed_db, draft_id).replace("Dear", "Hello")
    response = client().post(
        f"/drafts/{draft_id}/versions", json={"body": friendlier, "base_version": 1}
    )
    assert (response.status_code, response.json()) == (201, {"version": 2, "price_warning": None})
    assert read(
        committed_db,
        "SELECT version, author, body FROM draft_versions WHERE draft_id = %s AND version = 2",
        draft_id,
    ) == [(2, "owner", friendlier)]


def test_edit_that_changes_a_price_is_stored_with_a_warning(
    committed_db: CommittedDb, client: Client
) -> None:
    _, draft_id = drafted(committed_db)
    cheaper = latest_body(committed_db, draft_id).replace("€390.00", "€350.00")
    response = client().post(
        f"/drafts/{draft_id}/versions", json={"body": cheaper, "base_version": 1}
    )
    assert response.status_code == 201
    assert response.json()["price_warning"].startswith("edited-price warning")


@pytest.mark.parametrize(
    "payload",
    [{"body": " ", "base_version": 1}, {"body": "\t\n", "base_version": 1},
     {"body": "Hello."}, {"body": "Hello.", "base_version": 0}],
    ids=["space", "tab-newline", "no-base-version", "base-version-0"],
)  # fmt: skip
def test_bad_edit_requests_are_422_and_write_nothing(
    committed_db: CommittedDb, client: Client, payload: dict[str, Any]
) -> None:
    _, draft_id = drafted(committed_db)
    before = snapshot(committed_db)
    assert client().post(f"/drafts/{draft_id}/versions", json=payload).status_code == 422
    assert snapshot(committed_db) == before


def test_stale_edit_is_409_and_only_audited(committed_db: CommittedDb, client: Client) -> None:
    """D67: the laptop saved v2; the phone's edit from v1 is refused, v2 is kept."""
    email_id, draft_id = drafted(committed_db)
    api = client()
    assert api.post(
        f"/drafts/{draft_id}/versions", json={"body": "Laptop.", "base_version": 1}
    ).status_code == 201  # fmt: skip
    before, audits = snapshot(committed_db), len(audit_rows(committed_db))

    response = api.post(f"/drafts/{draft_id}/versions", json={"body": "Phone.", "base_version": 1})
    assert (response.status_code, response.json()) == (
        409, {"error": "stale_version", "latest": 2},
    )  # fmt: skip
    assert only_audit_added(committed_db, before, audits) == [
        ("owner", "draft.action_refused", email_id, draft_id,
         {"action": "edit", "reason": "stale_version", "seen": 1, "latest": 2}),
    ]  # fmt: skip


def test_edit_after_72_hours_is_409_expired_and_the_flip_is_kept(
    committed_db: CommittedDb, client: Client
) -> None:
    _, draft_id = drafted(committed_db)
    response = client(PAST_72H).post(
        f"/drafts/{draft_id}/versions", json={"body": "Too late.", "base_version": 1}
    )
    assert (response.status_code, response.json()) == (
        409, {"error": "expired", "draft_id": draft_id},
    )  # fmt: skip
    assert read(
        committed_db,
        "SELECT d.status, e.status FROM drafts d JOIN emails e ON e.id = d.email_id"
        " WHERE d.id = %s",
        draft_id,
    ) == [("expired", "expired")]
    assert read(
        committed_db, "SELECT count(*) FROM draft_versions WHERE draft_id = %s", draft_id
    ) == [(1,)]


# --- approve ----------------------------------------------------------------------------


def test_approve_holds_and_queues_the_reply(committed_db: CommittedDb, client: Client) -> None:
    _, draft_id = drafted(committed_db)
    response = client().post(f"/drafts/{draft_id}/approve", json={"version": 1})
    assert response.status_code == 200
    body = response.json()
    [(outbox_id,)] = read(committed_db, "SELECT id FROM outbox WHERE draft_id = %s", draft_id)
    booking_ids = [b for (b,) in read(
        committed_db, "SELECT id FROM bookings WHERE draft_id = %s ORDER BY id", draft_id
    )]  # fmt: skip
    assert body == {
        "outbox_id": outbox_id, "booking_ids": booking_ids,
        "hold_until": (NOW + timedelta(hours=48)).isoformat().replace("+00:00", "Z"),
    }  # fmt: skip
    assert len(booking_ids) == 1


def test_double_approve_is_409_and_only_audited(committed_db: CommittedDb, client: Client) -> None:
    """The step-16 rule: a TransitionRefused is rolled back, then recorded as one
    draft.action_refused row in its own transaction."""
    email_id, draft_id = drafted(committed_db)
    api = client()
    assert api.post(f"/drafts/{draft_id}/approve", json={"version": 1}).status_code == 200
    before, audits = snapshot(committed_db), len(audit_rows(committed_db))

    response = api.post(f"/drafts/{draft_id}/approve", json={"version": 1})
    assert (response.status_code, response.json()) == (
        409, {"error": "transition_refused", "current": "approved", "wanted": "approved"},
    )  # fmt: skip
    assert only_audit_added(committed_db, before, audits) == [
        ("owner", "draft.action_refused", email_id, draft_id,
         {"action": "approve", "reason": "transition_refused", "current": "approved",
          "wanted": "approved"}),
    ]  # fmt: skip


def test_stale_approve_is_409_and_only_audited(committed_db: CommittedDb, client: Client) -> None:
    """D67: the phone shows v1, the laptop saved v2; approving v1 sends nothing."""
    email_id, draft_id = drafted(committed_db)
    api = client()
    friendlier = latest_body(committed_db, draft_id).replace("Dear", "Hello")
    api.post(f"/drafts/{draft_id}/versions", json={"body": friendlier, "base_version": 1})
    before, audits = snapshot(committed_db), len(audit_rows(committed_db))

    response = api.post(f"/drafts/{draft_id}/approve", json={"version": 1})
    assert (response.status_code, response.json()) == (
        409, {"error": "stale_version", "latest": 2},
    )  # fmt: skip
    assert only_audit_added(committed_db, before, audits) == [
        ("owner", "draft.action_refused", email_id, draft_id,
         {"action": "approve", "reason": "stale_version", "seen": 1, "latest": 2}),
    ]  # fmt: skip


def test_price_warning_is_422_audited_then_confirmed(
    committed_db: CommittedDb, client: Client
) -> None:
    email_id, draft_id = drafted(committed_db)
    api = client()
    cheaper = latest_body(committed_db, draft_id).replace("€390.00", "€350.00")
    warning = api.post(
        f"/drafts/{draft_id}/versions", json={"body": cheaper, "base_version": 1}
    ).json()["price_warning"]
    [(quote_id,)] = read(
        committed_db, "SELECT quote_id FROM draft_offers WHERE draft_id = %s", draft_id
    )
    before, audits = snapshot(committed_db), len(audit_rows(committed_db))

    response = api.post(f"/drafts/{draft_id}/approve", json={"version": 2})
    assert (response.status_code, response.json()) == (
        422,
        {"error": "price_warning", "message": warning,
         "hint": "repeat with confirm_warnings=true to send it anyway"},
    )  # fmt: skip
    assert only_audit_added(committed_db, before, audits) == [
        ("owner", "draft.price_warning_shown", email_id, draft_id,
         {"action": "approve", "version": 2, "quote_ids": [quote_id]}),
    ]  # fmt: skip

    confirmed = api.post(
        f"/drafts/{draft_id}/approve", json={"version": 2, "confirm_warnings": True}
    )
    assert confirmed.status_code == 200
    [(body,)] = read(committed_db, "SELECT body FROM outbox WHERE draft_id = %s", draft_id)
    assert "€350.00" in body


def test_invalidated_is_409_with_reasons_and_kept(
    committed_db: CommittedDb, client: Client
) -> None:
    _, draft_id = drafted(committed_db)
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    for room in SEA_VIEW:
        rows.booking(room_id=room, stay=JUNE)
    conn.commit()

    response = client().post(f"/drafts/{draft_id}/approve", json={"version": 1})
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "invalidated"
    assert any("0 of 1 room(s) free" in reason for reason in body["reasons"])
    assert read(
        committed_db,
        "SELECT d.status, e.status, e.reason_codes FROM drafts d"
        " JOIN emails e ON e.id = d.email_id WHERE d.id = %s",
        draft_id,
    ) == [("invalidated", "escalated", ["NO_LONGER_AVAILABLE"])]
    assert read(committed_db, "SELECT count(*) FROM outbox") == [(0,)]


def test_approve_after_72_hours_is_409_expired_and_kept(
    committed_db: CommittedDb, client: Client
) -> None:
    _, draft_id = drafted(committed_db)
    response = client(PAST_72H).post(f"/drafts/{draft_id}/approve", json={"version": 1})
    assert (response.status_code, response.json()) == (
        409, {"error": "expired", "draft_id": draft_id},
    )  # fmt: skip
    assert read(committed_db, "SELECT status FROM drafts WHERE id = %s", draft_id) == [("expired",)]
    assert read(committed_db, "SELECT count(*) FROM outbox") == [(0,)]


@pytest.mark.parametrize(
    "payload", [{}, {"version": 0}, {"version": "one"}], ids=["missing", "zero", "text"]
)
def test_bad_approve_requests_are_422(
    committed_db: CommittedDb, client: Client, payload: dict[str, Any]
) -> None:
    _, draft_id = drafted(committed_db)
    before = snapshot(committed_db)
    assert client().post(f"/drafts/{draft_id}/approve", json=payload).status_code == 422
    assert snapshot(committed_db) == before


def test_a_bug_after_a_write_rolls_everything_back(
    committed_db: CommittedDb, client: Client
) -> None:
    """approve_draft flips stale holds (step 3) before it builds the text (step 6). A
    text with {{HOLD_UNTIL}} and nothing held is a bug there: 500, and the flip done
    before it is rolled back too. No refusal row: a bug is not a refused action."""
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    email_id = rows.email(status="awaiting_approval", received_at=RECEIVED)
    draft_id = rows.draft(email_id, None, route="policy", created_at=NOW)
    rows.version(draft_id, body="We hold a room for you until {{HOLD_UNTIL}}.", created_at=NOW)
    other_email, other_request, other_draft = rows.approved_draft()
    rows.booking(status="hold", expires_at=NOW - timedelta(hours=1), email_id=other_email,
                 request_id=other_request, draft_id=other_draft)  # fmt: skip
    conn.commit()
    before = snapshot(committed_db)

    api = TestClient(create_app(committed_db.connect, FixedClock(NOW)),
                     raise_server_exceptions=False)  # fmt: skip
    response = api.post(f"/drafts/{draft_id}/approve", json={"version": 1})
    assert (response.status_code, response.json()) == (500, {"error": "internal"})
    assert snapshot(committed_db) == before  # the stale hold is still a hold


# --- reject -----------------------------------------------------------------------------


def test_reject_then_reject_again(committed_db: CommittedDb, client: Client) -> None:
    email_id, draft_id = drafted(committed_db)
    api = client()
    response = api.post(f"/drafts/{draft_id}/reject")
    assert (response.status_code, response.json()) == (
        200, {"draft_id": draft_id, "status": "rejected"},
    )  # fmt: skip
    assert read(committed_db, "SELECT status FROM emails WHERE id = %s", email_id) == [("closed",)]
    again = api.post(f"/drafts/{draft_id}/reject")
    assert (again.status_code, again.json()["current"]) == (409, "rejected")


def test_reject_after_72_hours_is_409_expired(committed_db: CommittedDb, client: Client) -> None:
    _, draft_id = drafted(committed_db)
    response = client(PAST_72H).post(f"/drafts/{draft_id}/reject")
    assert (response.status_code, response.json()["error"]) == (409, "expired")


# --- emails -----------------------------------------------------------------------------


def email(committed_db: CommittedDb, status: str, reasons: list[str] | None = None) -> int:
    conn = committed_db.connect()
    email_id = committed_db.rows(conn).email(status=status, reason_codes=reasons or [])
    conn.commit()
    return email_id


def test_close_an_escalated_email(committed_db: CommittedDb, client: Client) -> None:
    email_id = email(committed_db, "escalated", ["COMPLAINT"])
    response = client().post(f"/emails/{email_id}/close")
    assert (response.status_code, response.json()) == (
        200, {"email_id": email_id, "status": "closed"},
    )  # fmt: skip
    assert read(committed_db, "SELECT status FROM emails WHERE id = %s", email_id) == [("closed",)]


def test_reopen_a_set_aside_email(committed_db: CommittedDb, client: Client) -> None:
    email_id = email(committed_db, "set_aside")
    response = client().post(f"/emails/{email_id}/reopen")
    assert (response.status_code, response.json()) == (
        200, {"email_id": email_id, "status": "escalated"},
    )  # fmt: skip
    assert read(
        committed_db, "SELECT status, reason_codes FROM emails WHERE id = %s", email_id
    ) == [("escalated", ["OWNER_REOPENED"])]


@pytest.mark.parametrize(
    ("action", "status", "wanted"),
    [("close", "received", "closed"), ("reopen", "escalated", "escalated")],
)
def test_refused_email_actions_are_409_and_only_audited(
    committed_db: CommittedDb, client: Client, action: str, status: str, wanted: str
) -> None:
    email_id = email(committed_db, status, ["COMPLAINT"] if status == "escalated" else None)
    before, audits = snapshot(committed_db), len(audit_rows(committed_db))
    response = client().post(f"/emails/{email_id}/{action}")
    assert (response.status_code, response.json()) == (
        409, {"error": "transition_refused", "current": status, "wanted": wanted},
    )  # fmt: skip
    assert only_audit_added(committed_db, before, audits) == [
        ("owner", "email.action_refused", email_id, None,
         {"action": action, "reason": "transition_refused", "current": status, "wanted": wanted}),
    ]  # fmt: skip


@pytest.mark.parametrize(
    "path",
    ["/drafts/999999/reject", "/emails/999999/close", "/emails/999999/reopen"],
)
def test_unknown_ids_are_404_and_write_nothing(
    committed_db: CommittedDb, client: Client, path: str
) -> None:
    before = snapshot(committed_db)
    response = client().post(path)
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert snapshot(committed_db) == before


@pytest.mark.parametrize(
    ("path", "payload"),
    [("/drafts/999999/versions", {"body": "Hi.", "base_version": 1}),
     ("/drafts/999999/approve", {"version": 1})],
)  # fmt: skip
def test_unknown_drafts_are_404_for_edit_and_approve(
    committed_db: CommittedDb, client: Client, path: str, payload: dict[str, Any]
) -> None:
    response = client().post(path, json=payload)
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
