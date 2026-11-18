"""Guardrails 2-5 on owner edits over HTTP (D77): an edit is always stored, with its
warnings; approval answers one 422 listing every warning of that version (price and
guardrail), each one audited, and confirm_warnings then approves."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from anemos.clock import FixedClock
from anemos.guardrails import number_values
from anemos.reservations.loader import load_hotel_data
from anemos.stub_drafter import Drafted, draft_reply

if TYPE_CHECKING:
    from conftest import CommittedDb, OwnerApi

pytestmark = pytest.mark.integration

RECEIVED = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)
NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
JUNE = "[2027-06-01,2027-06-04)"
TRANSFER = "It costs €25.00 per trip per car, for up to 4 passengers with their luggage."
HINT = "repeat with confirm_warnings=true to send it anyway"


@pytest.fixture
def api(owner_api: OwnerApi) -> Iterator[TestClient]:
    with owner_api(NOW) as client:
        yield client


def drafted(committed_db: CommittedDb, language: str = "en") -> tuple[int, int, str]:
    """(email id, draft id, version 1's body): a stub quote draft, sea view 1-4 June,
    with a summary line and one quote block; the run returned the transfer policy."""
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    email_id = rows.email(received_at=RECEIVED, language=language)
    rows.request(email_id, stay=JUNE, adults=2, room_type_id="sea_view")
    result = draft_reply(conn, load_hotel_data(conn), FixedClock(NOW), email_id)
    assert isinstance(result, Drafted), result
    rows.audit(email_id=email_id, actor="agent", event="agent.tool_called", output=Jsonb({
        "tool": "search_policies", "record": {"hits": [
            {"policy_id": "transfer", "title": "Transfer", "text": TRANSFER, "text_sha256": "x"},
        ]},
    }))  # fmt: skip
    body = conn.execute("SELECT body FROM draft_versions WHERE draft_id = %s AND version = 1",
                        (result.draft_id,)).fetchone()  # fmt: skip
    conn.commit()
    assert body is not None
    return email_id, result.draft_id, str(body[0])


def edit(api: TestClient, draft_id: int, body: str) -> dict[str, Any]:
    response = api.post(f"/api/drafts/{draft_id}/versions", json={"body": body, "base_version": 1})
    assert response.status_code == 201
    result: dict[str, Any] = response.json()
    return result


def shown(committed_db: CommittedDb, email_id: int) -> list[tuple[str, Any]]:
    conn = committed_db.connect()
    found = conn.execute(
        "SELECT event, output FROM audit_log WHERE email_id = %s AND event LIKE"
        " 'draft.%%warning_shown' ORDER BY id",
        (email_id,),
    ).fetchall()
    conn.commit()
    return [(e, o) for e, o in found]


def test_api_a_guardrail_warning_is_422_audited_then_confirmed(
    committed_db: CommittedDb, api: TestClient
) -> None:
    email_id, draft_id, v1 = drafted(committed_db)
    edited = edit(api, draft_id, v1.replace("Dear guest,", "Dear guest, your room is guaranteed."))
    assert edited["price_warning"] is None
    assert edited["warnings"] == ["forbidden phrases: ['guaranteed']"]
    refused = api.post(f"/api/drafts/{draft_id}/approve", json={"version": 2})
    assert (refused.status_code, refused.json()) == (422, {
        "error": "guardrail_warning",
        "warnings": [{"check": "g5_no_forbidden_phrases",
                      "message": "forbidden phrases: ['guaranteed']"}],
        "hint": HINT,
    })  # fmt: skip
    assert shown(committed_db, email_id) == [("draft.guardrail_warning_shown", {
        "action": "approve", "version": 2, "check": "g5_no_forbidden_phrases",
        "message": "forbidden phrases: ['guaranteed']",
    })]  # fmt: skip
    confirmed = api.post(f"/api/drafts/{draft_id}/approve",
                         json={"version": 2, "confirm_warnings": True})  # fmt: skip
    assert confirmed.status_code == 200


def test_api_a_changed_price_and_a_guardrail_warning_come_in_one_422(
    committed_db: CommittedDb, api: TestClient
) -> None:
    email_id, draft_id, v1 = drafted(committed_db)
    both = v1.replace("€390.00", "€350.00").replace("Dear guest,", "Dear guest, guaranteed:")
    edited = edit(api, draft_id, both)
    assert edited["price_warning"] is not None and edited["warnings"] != []
    refused = api.post(f"/api/drafts/{draft_id}/approve", json={"version": 2})
    assert refused.status_code == 422
    body = refused.json()
    assert body["error"] == "guardrail_warning"
    assert [w["check"] for w in body["warnings"]] == ["price_check", "g5_no_forbidden_phrases"]
    assert [event for event, _ in shown(committed_db, email_id)] == [
        "draft.price_warning_shown", "draft.guardrail_warning_shown",
    ]  # fmt: skip
    confirmed = api.post(f"/api/drafts/{draft_id}/approve",
                         json={"version": 2, "confirm_warnings": True})  # fmt: skip
    assert confirmed.status_code == 200
    conn = committed_db.connect()
    approved = conn.execute("SELECT output FROM audit_log WHERE email_id = %s AND event ="
                            " 'draft.approved'", (email_id,)).fetchone()  # fmt: skip
    conn.commit()
    assert approved is not None
    assert approved[0]["confirmed_checks"] == ["price_check", "g5_no_forbidden_phrases"]


@pytest.mark.parametrize("change, warns", [
    (("Dear guest,", "Dear guest, the transfer is €25 per car."), []),  # a policy number
    (("Dear guest,", "Dear guest, only 9 rooms are left."), ["g2_numbers_from_policies"]),
    (("Dear guest,", "  Dear   guest,  "), []),  # whitespace only
])  # fmt: skip
def test_api_numbers_and_whitespace_on_an_english_edit(
    committed_db: CommittedDb, api: TestClient, change: tuple[str, str], warns: list[str]
) -> None:
    _, draft_id, v1 = drafted(committed_db)
    assert "9" not in number_values(v1)  # a new number; one already in version 1 is allowed
    edit(api, draft_id, v1.replace(*change))
    conn = committed_db.connect()
    results = conn.execute("SELECT guardrail_results FROM draft_versions WHERE draft_id = %s"
                           " AND version = 2", (draft_id,)).fetchone()  # fmt: skip
    conn.commit()
    assert results is not None
    assert [k for k, r in results[0].items() if r is not True] == warns


def test_api_deleting_the_summary_line_warns(committed_db: CommittedDb, api: TestClient) -> None:
    _, draft_id, v1 = drafted(committed_db)
    line = next(x for x in v1.splitlines() if x.startswith("Your request:"))
    edited = edit(api, draft_id, v1.replace(line, ""))
    assert len(edited["warnings"]) == 1
    assert edited["warnings"][0].startswith("the request summary line was changed or removed")


def test_api_an_english_sentence_in_a_greek_reply_warns(
    committed_db: CommittedDb, api: TestClient
) -> None:
    _, draft_id, v1 = drafted(committed_db, language="el")
    assert "Το αίτημά σας:" in v1  # a Greek draft
    edited = edit(api, draft_id, v1 + "\n\nThank you very much for choosing our lovely hotel.")
    assert [w.split(" with ")[0] for w in edited["warnings"]] == ["Greek reply"]
