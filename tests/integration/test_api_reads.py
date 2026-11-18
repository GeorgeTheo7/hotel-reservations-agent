"""Read endpoints over HTTP, on committed_db: each request opens its own connection, so
the rows a test sets up must be committed first."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from anemos.api.reads import inbox_query
from anemos.states import EmailStatus

if TYPE_CHECKING:  # tests/ is not a package: the fixture's classes are for mypy only
    from conftest import CommittedDb, OwnerApi, Rows

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)  # = Rows.T0
NOW = T0 + timedelta(hours=10)


@pytest.fixture
def api(owner_api: OwnerApi) -> Iterator[TestClient]:
    with owner_api(NOW) as client:
        yield client


@pytest.fixture
def rows(committed_db: CommittedDb) -> Iterator[Rows]:
    """Row helpers on one committed connection; everything is committed at teardown
    and by `commit()` in the test before the API reads it."""
    conn = committed_db.connect()
    yield committed_db.rows(conn)
    conn.commit()


def commit(rows: Rows) -> None:
    rows.db.commit()


def ids(page: dict[str, Any]) -> list[int]:
    return [item["id"] for item in page["items"]]


# --- inbox ----------------------------------------------------------------------------


def email_in(rows: Rows, status: EmailStatus, minutes: int = 0) -> int:
    reasons = ["MODEL"] if status is EmailStatus.ESCALATED else []
    return rows.email(
        status=status, reason_codes=reasons, received_at=T0 + timedelta(minutes=minutes)
    )


def test_inbox_is_newest_first_and_hides_set_aside(api: TestClient, rows: Rows) -> None:
    old = email_in(rows, EmailStatus.RECEIVED, minutes=1)
    new = email_in(rows, EmailStatus.ESCALATED, minutes=2)
    email_in(rows, EmailStatus.SET_ASIDE, minutes=3)
    commit(rows)
    page = api.get("/api/emails").json()
    assert ids(page) == [new, old]
    assert page["next_cursor"] is None
    assert page["items"][0] | {"received_at": None} == {
        "id": new, "from_address": "guest@example.test", "subject": "Room in July",
        "received_at": None, "status": "escalated", "reason_codes": ["MODEL"],
        "draft_id": None, "draft_status": None,
    }  # fmt: skip


@pytest.mark.parametrize("status", list(EmailStatus), ids=str)
def test_status_filter_returns_only_that_status(
    api: TestClient, rows: Rows, status: EmailStatus
) -> None:
    """One email in every status; the filter finds exactly its own, set_aside too (D50)."""
    by_status = {s: email_in(rows, s, minutes=i) for i, s in enumerate(EmailStatus)}
    commit(rows)
    response = api.get("/api/emails", params={"status": status.value})
    assert response.status_code == 200
    assert ids(response.json()) == [by_status[status]]


def test_inbox_shows_the_draft(api: TestClient, rows: Rows) -> None:
    email_id = email_in(rows, EmailStatus.AWAITING_APPROVAL)
    draft_id = rows.draft(email_id, None, route="policy")
    commit(rows)
    [item] = api.get("/api/emails").json()["items"]
    assert (item["draft_id"], item["draft_status"]) == (draft_id, "pending_approval")


@pytest.mark.parametrize(
    "params",
    [
        {"status": "spam"},
        {"limit": 0},
        {"limit": 201},
        {"cursor": "not-a-cursor"},
        {"cursor": "WyIyMDI2LTExLTEzVDA3OjAwOjAwIiwgMV0="},  # naive time
        {"cursor": "WyIyMDI2LTExLTEzVDA3OjAwOjAwKzAwOjAwIiwgIjEiXQ=="},  # id as text
    ],
    ids=["unknown-status", "limit-0", "limit-201", "garbage", "naive-time", "text-id"],
)
def test_bad_inbox_parameters_are_422(api: TestClient, params: dict[str, Any]) -> None:
    assert api.get("/api/emails", params=params).status_code == 422


def test_pages_have_no_duplicates_or_gaps_when_an_email_arrives(
    api: TestClient, rows: Rows
) -> None:
    """Five emails, pages of two. A newer email arriving after page 1 does not shift
    anything into or out of pages 2 and 3; it is on top of a fresh first page."""
    five = [email_in(rows, EmailStatus.RECEIVED, minutes=i) for i in range(5)]
    commit(rows)
    first = api.get("/api/emails", params={"limit": 2}).json()
    newest = email_in(rows, EmailStatus.RECEIVED, minutes=10)
    commit(rows)
    second = api.get("/api/emails", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    third = api.get("/api/emails", params={"limit": 2, "cursor": second["next_cursor"]}).json()
    assert ids(first) + ids(second) + ids(third) == five[::-1]
    assert third["next_cursor"] is None
    assert ids(api.get("/api/emails", params={"limit": 2}).json())[0] == newest


def test_same_received_at_is_ordered_by_id_across_pages(api: TestClient, rows: Rows) -> None:
    """Ties on received_at: the id decides, so a page boundary between two emails of
    the same second neither repeats nor skips one."""
    same = [email_in(rows, EmailStatus.RECEIVED) for _ in range(3)]
    commit(rows)
    first = api.get("/api/emails", params={"limit": 2}).json()
    second = api.get("/api/emails", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert ids(first) + ids(second) == same[::-1]


def test_a_full_last_page_has_no_cursor(api: TestClient, rows: Rows) -> None:
    for i in range(2):
        email_in(rows, EmailStatus.RECEIVED, minutes=i)
    commit(rows)
    assert api.get("/api/emails", params={"limit": 2}).json()["next_cursor"] is None


@pytest.mark.parametrize(
    ("status", "index", "condition"),
    [
        (
            EmailStatus.RECEIVED,
            "emails_status_received_at_id_idx",
            "Index Cond: ((status = 'received'::text) AND (ROW(received_at, id) < ROW(",
        ),
        (None, "emails_visible_received_at_id_idx", "Index Cond: (ROW(received_at, id) < ROW("),
    ],
    ids=["status-filter", "default-view"],
)
def test_a_second_inbox_page_seeks_in_its_index(
    committed_db: CommittedDb, rows: Rows, status: EmailStatus | None, index: str, condition: str
) -> None:
    """Migration 0012. The second page (with a cursor) must use its index *with an index
    condition* on the cursor comparison, not just the index name (the step-15 lesson);
    the first page has no comparison to show. Fresh statistics and sequential scans off
    for this transaction only, so the question is "can it?", not "is it cheapest?"."""
    for i, s in enumerate([EmailStatus.RECEIVED, EmailStatus.SET_ASIDE] * 3):
        email_in(rows, s, minutes=i)
    commit(rows)
    conn = committed_db.connect()
    conn.execute("ANALYZE emails")
    conn.execute("SET LOCAL enable_seqscan = off")
    query = inbox_query(status, (T0 + timedelta(minutes=4), 5), 2)
    plan = [line for (line,) in conn.execute(sql.SQL("EXPLAIN {}").format(query))]
    conn.rollback()
    scan = next((i for i, line in enumerate(plan) if index in line), None)
    assert scan is not None, plan
    assert condition in plan[scan + 1], plan


# --- one email --------------------------------------------------------------------------


def test_email_detail_returns_what_the_database_holds(api: TestClient, rows: Rows) -> None:
    email_id = email_in(rows, EmailStatus.AWAITING_APPROVAL)
    request_id = rows.request(email_id)
    quote_id = rows.quote(request_id)
    draft_id = rows.draft(email_id, request_id)
    rows.version(draft_id)
    rows.version(draft_id, 2, body="Dear guest, edited. {{QUOTE_BLOCK}}")
    rows.offer(draft_id, request_id, quote_id, hold=True)
    commit(rows)

    body = api.get(f"/api/emails/{email_id}").json()
    assert body["status"] == "awaiting_approval"
    assert body["outbox"] is None
    draft = body["draft"]
    assert (draft["id"], draft["status"], draft["route"]) == (draft_id, "pending_approval", "quote")
    assert [(v["version"], v["author"]) for v in draft["versions"]] == [(1, "agent"), (2, "owner")]
    assert draft["versions"][1]["body"] == "Dear guest, edited. {{QUOTE_BLOCK}}"
    assert draft["versions"][0]["guardrail_results"] == {"quote_block_once": True}
    assert draft["offers"] == [
        {
            "quote_id": quote_id, "room_type_id": "sea_view",
            "check_in": "2027-07-12", "check_out": "2027-07-15", "rooms": 1,
            "hold": True, "hold_allowed": True, "changes": [],
            "accommodation_subtotal_cents": 39000, "city_tax_cents": 1200,
            "deposit_cents": 11700, "free_cancellation_until": "2027-06-28",
            "rendered_block": f"Our offer: quote {quote_id}\nAccommodation total: €390.00",
        }
    ]  # fmt: skip


def test_email_detail_with_its_outbox_row(api: TestClient, rows: Rows) -> None:
    _, _, draft_id = rows.approved_draft()
    outbox_id = rows.outbox(draft_id)
    email_id = rows.db.execute("SELECT email_id FROM drafts WHERE id = %s", (draft_id,))
    found = email_id.fetchone()
    assert found is not None
    commit(rows)
    body = api.get(f"/api/emails/{found[0]}").json()
    assert body["draft"]["status"] == "approved"
    assert body["outbox"] == {
        "id": outbox_id, "status": "pending", "attempts": 0, "last_error": None,
        "next_attempt_at": None, "sent_at": None,
    }  # fmt: skip


def test_email_without_a_draft(api: TestClient, rows: Rows) -> None:
    email_id = email_in(rows, EmailStatus.RECEIVED)
    commit(rows)
    body = api.get(f"/api/emails/{email_id}").json()
    assert (body["draft"], body["outbox"], body["clean_body"]) == (
        None, None, "Hello, do you have a room?",
    )  # fmt: skip


def test_unknown_email_is_404(api: TestClient) -> None:
    response = api.get("/api/emails/999999")
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


# --- Holds page -------------------------------------------------------------------------


def room_ids(rows: Rows) -> list[int]:
    return [r for (r,) in rows.db.execute("SELECT id FROM rooms ORDER BY id")]


def test_holds_page_lists_holds_and_confirmed(api: TestClient, rows: Rows) -> None:
    """Default: holds and confirmed bookings, by check-in then room; cancelled and
    expired are not on the page. A hold past expires_at is marked, and stays a hold:
    the read flips nothing (D22)."""
    r1, r2, r3, r4 = room_ids(rows)[:4]
    email_id, request_id, draft_id = rows.approved_draft()
    link = {"email_id": email_id, "request_id": request_id, "draft_id": draft_id}
    late = rows.booking(room_id=r2, status="hold", expires_at=NOW + timedelta(hours=1), **link)
    stale = rows.booking(room_id=r1, status="hold", expires_at=NOW - timedelta(seconds=1), **link)
    early = rows.booking(room_id=r3, stay="[2027-07-01,2027-07-03)", status="confirmed")
    rows.booking(room_id=r4, status="cancelled")
    rows.booking(room_id=r4, stay="[2027-08-01,2027-08-02)", status="expired",
                 expires_at=T0, **link)  # fmt: skip
    commit(rows)

    holds = api.get("/api/holds").json()
    assert [(b["id"], b["past_expiry"]) for b in holds] == [
        (early, False), (stale, True), (late, False),
    ]  # fmt: skip
    assert holds[1] | {"room_type_id": None} == {
        "id": stale, "room_id": r1, "room_type_id": None,
        "check_in": "2027-07-12", "check_out": "2027-07-15", "status": "hold",
        "expires_at": (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "past_expiry": True, "price_cents": 39000, "email_id": email_id,
        "guest_address": "guest@example.test", "draft_id": draft_id,
    }  # fmt: skip
    assert holds[0]["guest_address"] is None  # entered without an email
    status = rows.db.execute("SELECT status FROM bookings WHERE id = %s", (stale,)).fetchone()
    assert status == ("hold",)


def test_exactly_at_expiry_is_not_past_expiry(api: TestClient, rows: Rows) -> None:
    """Same comparison as D22: a hold counts as expired only when expires_at < now."""
    email_id, request_id, draft_id = rows.approved_draft()
    rows.booking(status="hold", expires_at=NOW, email_id=email_id, request_id=request_id,
                 draft_id=draft_id)  # fmt: skip
    commit(rows)
    [hold] = api.get("/api/holds").json()
    assert hold["past_expiry"] is False


@pytest.mark.parametrize("status", ["hold", "confirmed"])
def test_holds_page_filter(api: TestClient, rows: Rows, status: str) -> None:
    r1, r2 = room_ids(rows)[:2]
    email_id, request_id, draft_id = rows.approved_draft()
    held = rows.booking(room_id=r1, status="hold", expires_at=NOW, email_id=email_id,
                        request_id=request_id, draft_id=draft_id)  # fmt: skip
    confirmed = rows.booking(room_id=r2, status="confirmed")
    commit(rows)
    expected = {"hold": held, "confirmed": confirmed}[status]
    assert [b["id"] for b in api.get("/api/holds", params={"status": status}).json()] == [expected]


@pytest.mark.parametrize("status", ["cancelled", "expired", "nonsense"])
def test_holds_page_filter_only_takes_page_statuses(api: TestClient, status: str) -> None:
    assert api.get("/api/holds", params={"status": status}).status_code == 422
