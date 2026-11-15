"""The six rules that hold across emails, drafts, bookings and outbox (states.md).

One scenario (`world`) runs emails through every path on committed_db, with real
commits. Each rule is a checker that lists violating rows; its test asserts the list
is empty on the scenario. A second test per rule plants a violation and asserts the
checker finds it, so a checker that can never fail cannot pass unnoticed.

Isolation: `world` is function-scoped and committed_db truncates before and after
every test, so each test builds its own scenario; rules 5 and 6 (which move time and
attempt changes) can never leave rows for another test to see. Rules 5 and 6 also
re-check rules 1-4 at their end: the rules must hold in every state, not only the first.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import ForeignKeyViolation, RestrictViolation
from psycopg.rows import TupleRow

from anemos.approval import Approved, Invalidated, _flip_stale_holds, approve_draft
from anemos.audit import AuditEvent
from anemos.clock import FixedClock
from anemos.inbox import store_email
from anemos.mailbox import incoming
from anemos.owner_actions import (
    DraftExpired,
    cancel_booking,
    close_email,
    confirm_hold,
    edit_draft,
    expire_draft_if_due,
    reject_draft,
    release_hold,
    reopen_email,
)
from anemos.reservations.loader import load_hotel_data
from anemos.states import (
    FINAL_BOOKING_STATUSES,
    FINAL_DRAFT_STATUSES,
    FINAL_EMAIL_STATUSES,
    FINAL_OUTBOX_STATUSES,
    TransitionRefused,
)
from anemos.stub_drafter import Drafted, Escalated, draft_reply

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import CommittedDb, Rows

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

RECEIVED = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)
NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)


# --- the scenario -----------------------------------------------------------------------


@dataclass
class World:
    conn: Conn
    clock: FixedClock
    drafts: dict[str, int]  # scenario name -> draft id
    emails: dict[str, int]  # scenario name -> email id


def _email(
    rows: Rows,
    name: str,
    *,
    sender: str | None = None,
    received: datetime = RECEIVED,
    **request: Any,
) -> int:
    """An email (and its request, if request columns are given) named for the scenario.
    Each scenario has its own sender, so D48 superseding only happens where intended."""
    email_id = rows.email(
        from_address=sender or f"{name.replace('_', '-')}@example.test", received_at=received,
        message_id=f"<{name}@example.test>", subject=f"About {name}",
    )  # fmt: skip
    if request:
        columns: dict[str, Any] = {"stay": "[2027-06-01,2027-06-04)", "adults": 2} | request
        rows.request(email_id, **columns)
    return email_id


@pytest.fixture
def world(committed_db: CommittedDb) -> World:
    """Every path through states.md, committed, on a clock that only moves forward."""
    conn = committed_db.connect()
    rows = committed_db.rows(conn)
    hotel = load_hotel_data(conn)
    clock = FixedClock(NOW)
    later = FixedClock(NOW + timedelta(hours=73))  # past both the 48 h hold and 72 h draft
    drafts: dict[str, int] = {}
    emails: dict[str, int] = {}

    def drafted(
        name: str, *, at: FixedClock = clock, sender: str | None = None, **request: Any
    ) -> int:
        emails[name] = _email(rows, name, sender=sender, **request)
        result = draft_reply(conn, hotel, at, emails[name])
        assert isinstance(result, Drafted), (name, result)
        drafts[name] = result.draft_id
        return result.draft_id

    # Approved: held quote, quote without hold (Ex 4), two rooms, decline; holds that
    # will be confirmed, released, or swept as stale.
    approved: dict[str, dict[str, Any]] = {
        "held": {"room_type_id": "sea_view"},
        "no_hold": {"stay": "[2027-08-10,2027-08-13)", "room_type_id": "sea_view"},
        "two_rooms": {"adults": 4, "rooms": 2, "room_type_id": "standard"},
        "decline": {"stay": "[2027-11-05,2027-11-08)"},
        "confirmed": {"stay": "[2027-07-01,2027-07-04)", "room_type_id": "sea_view"},
        "released": {"stay": "[2027-07-10,2027-07-13)", "room_type_id": "sea_view"},
        "stale": {"stay": "[2027-09-01,2027-09-04)", "room_type_id": "sea_view"},
        "confirmed_cancelled": {"stay": "[2027-07-20,2027-07-23)", "room_type_id": "sea_view"},
    }
    for name, request in approved.items():
        assert isinstance(approve_draft(conn, clock, drafted(name, **request)), Approved)

    # Approved alternatives (Sea View full: Standard and Family Suite offered, no hold).
    for room in (201, 202, 203, 204):
        rows.booking(room_id=room, stay="[2027-06-10,2027-06-13)")
    alternatives = drafted("alternatives", stay="[2027-06-10,2027-06-13)", room_type_id="sea_view")
    assert isinstance(approve_draft(conn, clock, alternatives), Approved)

    # Rejected (ask_info), and a pending draft with an owner edit.
    reject_draft(conn, clock, drafted("rejected", children=2, child_ages=[8]))
    edit_draft(conn, clock, drafted("pending", children=1), "Dear guest, how old?")

    # Invalidated: every Family Suite is taken after the draft was written.
    invalid = drafted("invalidated", stay="[2027-10-01,2027-10-04)", room_type_id="family_suite")
    for room in (301, 302, 303):
        rows.booking(room_id=room, stay="[2027-10-01,2027-10-04)")
    assert isinstance(approve_draft(conn, clock, invalid), Invalidated)

    # Superseded by a newer email; and a draft born superseded (D48).
    drafted("superseded", room_type_id="standard")
    store_email(conn, clock, incoming(
        message_id="<superseded-2@example.test>", from_header="superseded@example.test",
        subject="Change", body="Two rooms instead", received_at=RECEIVED + timedelta(minutes=5),
    ))  # fmt: skip
    _email(rows, "newer", sender="born@example.test", received=RECEIVED + timedelta(minutes=5))
    drafted("born_superseded", sender="born@example.test", room_type_id="standard")

    # Escalated by the stub (GROUP), then closed by the owner.
    emails["escalated"] = _email(rows, "escalated", adults=6, rooms=3)
    assert isinstance(draft_reply(conn, hotel, clock, emails["escalated"]), Escalated)
    close_email(conn, clock, emails["escalated"])

    # Set aside, then "not spam".
    emails["reopened"] = _email(rows, "reopened")
    conn.execute("UPDATE emails SET status = 'set_aside' WHERE id = %s", (emails["reopened"],))
    reopen_email(conn, clock, emails["reopened"])

    # Left as they are, made by raw SQL because no code path leaves them yet:
    # - set_aside: stands in for the week 4 router setting an email aside as spam.
    # - processing: stands in for a run in flight until the week 5 agent exists; today
    #   `processing` only lives inside the stub drafter's single transaction.
    emails["set_aside"] = _email(rows, "set_aside")
    conn.execute("UPDATE emails SET status = 'set_aside' WHERE id = %s", (emails["set_aside"],))
    emails["in_flight"] = _email(rows, "in_flight")
    conn.execute("UPDATE emails SET status = 'processing' WHERE id = %s", (emails["in_flight"],))

    # Holds page: confirm one hold, release another; mark one outbox row sent.
    confirm_hold(conn, clock, _booking_of(conn, drafts["confirmed"]))
    release_hold(conn, clock, _booking_of(conn, drafts["released"]))
    cancelled = _booking_of(conn, drafts["confirmed_cancelled"])
    confirm_hold(conn, clock, cancelled)
    cancel_booking(conn, clock, cancelled)  # confirmed -> cancelled
    conn.execute(
        "UPDATE outbox SET status = 'sent', attempts = 1, sent_at = %s WHERE draft_id = %s",
        (NOW, drafts["decline"]),
    )
    conn.commit()

    # 73 hours later: an owner action on a 73-hour-old draft expires it (D54); the
    # stale-hold sweep expires every hold from the first round; then one more hold is
    # approved, so a live hold remains for rule 5 to watch expire.
    reject_draft(conn, later, drafted("expired", room_type_id="standard"))
    reject_draft(conn, later, drafted("expired_closed", room_type_id="standard"))
    close_email(conn, later, emails["expired_closed"])  # expired -> closed
    _flip_stale_holds(conn, later.now())
    live = drafted("live", at=later, stay="[2027-08-24,2027-08-27)", room_type_id="sea_view")
    assert isinstance(approve_draft(conn, later, live), Approved)
    conn.commit()
    return World(conn, later, drafts, emails)


def _booking_of(conn: Conn, draft_id: int) -> int:
    row = conn.execute("SELECT min(id) FROM bookings WHERE draft_id = %s", (draft_id,)).fetchone()
    assert row is not None and row[0] is not None
    return int(row[0])


def allowed_statuses(conn: Conn) -> dict[str, set[str]]:
    """Each table's statuses, read from its status CHECK constraint in pg_catalog, so a
    status added by a later migration is required here without editing this test."""
    allowed: dict[str, set[str]] = {}
    for table in ("emails", "drafts", "bookings", "outbox"):
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = %s::regclass AND conname = %s",
            (table, f"{table}_status_valid"),
        ).fetchone()
        assert row is not None, f"{table}_status_valid not found"
        allowed[table] = set(re.findall(r"'([a-z_]+)'::text", row[0]))
        assert allowed[table], row[0]
    conn.commit()
    return allowed


def test_the_scenario_reaches_every_status_the_database_allows(world: World) -> None:
    """Guards the scenario itself: if a path disappears, the rules prove less."""
    for table, statuses in allowed_statuses(world.conn).items():
        query = sql.SQL("SELECT DISTINCT status FROM {}").format(sql.Identifier(table))
        found = {status for (status,) in world.conn.execute(query)}
        assert statuses - found == set(), f"{table}: no row in {sorted(statuses - found)}"
    world.conn.commit()


def test_the_scenario_performs_every_audited_transition(world: World) -> None:
    """Every AuditEvent appears, so every transition with an audit entry was run
    (including booking confirmed -> cancelled and email expired -> closed)."""
    events = {e for (e,) in world.conn.execute("SELECT DISTINCT event FROM audit_log")}
    world.conn.commit()
    assert {event.value for event in AuditEvent} - events == set()
    cancelled_after_confirm = world.conn.execute(
        "SELECT count(*) FROM bookings WHERE status = 'cancelled' AND id IN ("
        " SELECT (output->>'booking_id')::bigint FROM audit_log WHERE event = 'hold.confirmed')"
    ).fetchone()
    closed_after_expiry = world.conn.execute(
        "SELECT count(*) FROM emails WHERE status = 'closed' AND id IN ("
        " SELECT email_id FROM audit_log WHERE event = 'draft.expired')"
    ).fetchone()
    world.conn.commit()
    assert cancelled_after_confirm == (1,)
    assert closed_after_expiry == (1,)


# --- the checkers -----------------------------------------------------------------------

RULE_1 = """
SELECT e.id FROM emails e LEFT JOIN drafts d ON d.email_id = e.id
WHERE (e.status = 'answered') <> (d.status IS NOT DISTINCT FROM 'approved')
"""  # an email is answered if and only if its draft is approved

RULE_2 = """
SELECT d.id FROM drafts d LEFT JOIN outbox o ON o.draft_id = d.id
GROUP BY d.id, d.status
HAVING (d.status = 'approved') <> (count(o.id) = 1) OR count(o.id) > 1
"""  # an outbox row exists if and only if the draft is approved, and exactly one

RULE_3 = """
SELECT b.id FROM bookings b JOIN drafts d ON d.id = b.draft_id
WHERE d.status <> 'approved' OR b.request_id IS DISTINCT FROM d.request_id
   OR b.email_id IS DISTINCT FROM d.email_id
"""  # a booking created by the system (it has a draft) points to an approved draft's request

RULE_4 = """
SELECT b.id FROM bookings b JOIN rooms r ON r.id = b.room_id
WHERE b.draft_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM draft_offers o JOIN quotes q ON q.id = o.quote_id
    WHERE o.draft_id = b.draft_id AND o.hold AND q.hold_allowed
      AND q.room_type_id = r.room_type_id AND q.stay = b.stay)
"""  # every booking comes from the draft's held offer, and that quote allowed a hold


def violations(conn: Conn, rule: str) -> list[int]:
    rows = conn.execute(rule).fetchall()
    conn.commit()
    return sorted(int(r[0]) for r in rows)


@pytest.mark.parametrize(
    "rule", [RULE_1, RULE_2, RULE_3, RULE_4], ids=["rule1", "rule2", "rule3", "rule4"]
)
def test_rules_1_to_4_hold_on_the_scenario(world: World, rule: str) -> None:
    assert violations(world.conn, rule) == []


def assert_rules_1_to_4(conn: Conn) -> None:
    for name, rule in (("1", RULE_1), ("2", RULE_2), ("3", RULE_3), ("4", RULE_4)):
        assert violations(conn, rule) == [], f"rule {name}"


def test_rules_1_to_4_have_something_to_check(world: World) -> None:
    """Not vacuous: the scenario has answered emails, outbox rows and system bookings."""
    counts = world.conn.execute(
        "SELECT (SELECT count(*) FROM emails WHERE status = 'answered'),"
        " (SELECT count(*) FROM outbox),"
        " (SELECT count(*) FROM bookings WHERE draft_id IS NOT NULL),"
        " (SELECT count(*) FROM draft_offers o JOIN quotes q ON q.id = o.quote_id"
        "  WHERE NOT q.hold_allowed)"
    ).fetchone()
    assert counts is not None
    assert all(c >= 3 for c in counts), counts


# --- planted violations: each checker can fail ------------------------------------------


def test_rule_1_checker_finds_an_answered_email_without_an_approved_draft(
    rows: Rows, db: Conn
) -> None:
    email_id = rows.email(status="answered")
    rows.draft(email_id, None, route="policy")  # still pending
    assert email_id in [int(r[0]) for r in db.execute(RULE_1).fetchall()]


def test_rule_2_checker_finds_an_approved_draft_without_outbox(rows: Rows, db: Conn) -> None:
    _, _, draft_id = rows.approved_draft()
    assert draft_id in [int(r[0]) for r in db.execute(RULE_2).fetchall()]


def test_rule_3_is_also_enforced_by_the_database(rows: Rows, db: Conn) -> None:
    """A system booking for a draft that is not approved cannot even be committed (the
    deferred foreign key, migration 0008), so the checker is a second line."""
    email_id = rows.email()
    request_id = rows.request(email_id)
    draft_id = rows.draft(email_id, request_id)  # pending
    with pytest.raises(ForeignKeyViolation), db.transaction():
        rows.booking(email_id=email_id, request_id=request_id, draft_id=draft_id)
        rows.check_deferred_now("bookings_draft_approved_fkey")


def test_rule_3_checker_finds_a_booking_of_an_unapproved_draft(rows: Rows, db: Conn) -> None:
    email_id = rows.email()
    request_id = rows.request(email_id)
    draft_id = rows.draft(email_id, request_id)
    booking_id = rows.booking(email_id=email_id, request_id=request_id, draft_id=draft_id)
    # Checked before COMMIT (the deferred key would refuse it there).
    assert booking_id in [int(r[0]) for r in db.execute(RULE_3).fetchall()]


def test_rule_4_checker_finds_a_booking_of_a_quote_without_hold(rows: Rows, db: Conn) -> None:
    """The database allows this one (the key only checks "approved"): only the checker
    and the approval code stand between a no-hold quote and a booking."""
    email_id, request_id, draft_id = rows.approved_draft()
    quote_id = rows.quote(request_id, changes=["min_stay"])
    rows.offer(draft_id, request_id, quote_id, hold=False)
    booking_id = rows.booking(
        room_id=201, email_id=email_id, request_id=request_id, draft_id=draft_id
    )
    assert booking_id in [int(r[0]) for r in db.execute(RULE_4).fetchall()]


# --- rule 5: time only expires ----------------------------------------------------------

STATE_SQL = {
    "emails": "SELECT id, status FROM emails",
    "drafts": "SELECT id, status FROM drafts",
    "bookings": "SELECT id, status FROM bookings",
    "outbox": "SELECT id, status FROM outbox",
}


def state(conn: Conn) -> dict[str, dict[int, str]]:
    result = {t: {int(i): s for i, s in conn.execute(q).fetchall()} for t, q in STATE_SQL.items()}
    conn.commit()
    return result


def time_violations(
    before: dict[str, dict[int, str]], after: dict[str, dict[int, str]]
) -> list[str]:
    """Rule 5 (D17): rows may only move to `expired`; nothing may appear."""
    found: list[str] = []
    for table in STATE_SQL:
        for row_id in after[table].keys() - before[table].keys():
            found.append(f"{table} {row_id} created ({after[table][row_id]})")
        for row_id, old in before[table].items():
            new = after[table].get(row_id)
            if new != old and new != "expired":
                found.append(f"{table} {row_id}: {old} -> {new}")
    return found


def time_passes(conn: Conn, clock: FixedClock) -> None:
    """Every time-driven step in the system today: draft expiry (72 h) and the stale-hold
    sweep (the week-10 tidy-up job will run exactly these)."""
    for (draft_id,) in conn.execute("SELECT id FROM drafts ORDER BY id").fetchall():
        expire_draft_if_due(conn, clock, int(draft_id))
    _flip_stale_holds(conn, clock.now())
    conn.commit()


def test_rule_5_time_only_expires(world: World) -> None:
    before = state(world.conn)
    time_passes(world.conn, FixedClock(NOW + timedelta(days=10)))
    after = state(world.conn)
    assert time_violations(before, after) == []
    changed = {(t, i) for t in STATE_SQL for i in before[t] if before[t][i] != after[t][i]}
    assert changed, "time changed nothing: the scenario has no pending draft or live hold"
    assert_rules_1_to_4(world.conn)


def test_rule_5_checker_finds_time_that_escalates_or_creates(world: World) -> None:
    before = state(world.conn)
    after = {t: dict(rows) for t, rows in before.items()}
    pending = world.emails["pending"]
    after["emails"][pending] = "escalated"
    after["outbox"][999_999] = "pending"
    assert sorted(time_violations(before, after)) == sorted(
        [
            "outbox 999999 created (pending)",
            f"emails {pending}: awaiting_approval -> escalated",
        ]
    )


# Arrival 5 Jun 2027 (check-in 12:00 UTC); email 1 Jun, so not SHORT_NOTICE.
D64_RECEIVED = datetime(2027, 6, 1, 6, 0, tzinfo=UTC)
TEN_DAYS_LATER = FixedClock(D64_RECEIVED + timedelta(days=10))


@pytest.fixture
def stale_draft(db: Conn, rows: Rows) -> int:
    """A pending draft that, ten days on, is past 72 h *and* past its arrival."""
    email_id = rows.email(received_at=D64_RECEIVED, message_id="<d64@example.test>")
    rows.request(email_id, stay="[2027-06-05,2027-06-08)", adults=2, room_type_id="sea_view")
    result = draft_reply(db, load_hotel_data(db), FixedClock(D64_RECEIVED), email_id)
    assert isinstance(result, Drafted)
    return result.draft_id


@pytest.mark.parametrize(
    "action",
    [
        lambda db, d: approve_draft(db, TEN_DAYS_LATER, d),
        lambda db, d: edit_draft(db, TEN_DAYS_LATER, d, "Still interested?"),
        lambda db, d: reject_draft(db, TEN_DAYS_LATER, d),
    ],
    ids=["approve", "edit", "reject"],
)
def test_time_plus_a_click_gives_expired_never_invalidated(
    db: Conn, stale_draft: int, action: Callable[[Conn, int], object]
) -> None:
    """Rule 5 vs D64: expiry (72 h) is checked before the arrival check, so an owner
    action on a stale draft whose arrival also passed expires it; it is never
    invalidated and escalated (an escalation caused by time would break D17)."""
    assert action(db, stale_draft) == DraftExpired(stale_draft)
    row = db.execute(
        "SELECT d.status, e.status, e.reason_codes FROM drafts d"
        " JOIN emails e ON e.id = d.email_id WHERE d.id = %s",
        (stale_draft,),
    ).fetchone()
    assert row == ("expired", "expired", [])


# --- rule 6: nothing changes a final status --------------------------------------------


def snapshot(conn: Conn) -> dict[str, list[Any]]:
    tables = ("emails", "drafts", "draft_versions", "bookings", "outbox", "audit_log")
    query = sql.SQL("SELECT to_jsonb(t)::text AS row FROM {} t ORDER BY row")
    result = {t: conn.execute(query.format(sql.Identifier(t))).fetchall() for t in tables}
    conn.commit()
    return result


def final_rows(conn: Conn) -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for table, finals in (
        ("emails", FINAL_EMAIL_STATUSES), ("drafts", FINAL_DRAFT_STATUSES),
        ("bookings", FINAL_BOOKING_STATUSES), ("outbox", FINAL_OUTBOX_STATUSES),
    ):  # fmt: skip
        query = sql.SQL("SELECT id, status FROM {} WHERE status = ANY(%s)").format(
            sql.Identifier(table)
        )
        rows += [(table, int(i), s) for i, s in conn.execute(query, ([str(f) for f in finals],))]
    conn.commit()
    return rows


def actions_for(table: str, clock: FixedClock) -> dict[str, Callable[[Conn, int], object]]:
    """Every code path that could change a row of this table."""
    direct: dict[str, Callable[[Conn, int], object]] = {
        "sql-update": lambda c, i: c.execute(
            sql.SQL("UPDATE {} SET status = status WHERE id = %s").format(sql.Identifier(table)),
            (i,),
        ),
        "sql-delete": lambda c, i: c.execute(
            sql.SQL("DELETE FROM {} WHERE id = %s").format(sql.Identifier(table)), (i,)
        ),
    }
    by_table: dict[str, dict[str, Callable[[Conn, int], object]]] = {
        "drafts": {
            "approve": lambda c, i: approve_draft(c, clock, i, confirm_warnings=True),
            "edit": lambda c, i: edit_draft(c, clock, i, "Changed."),
            "reject": lambda c, i: reject_draft(c, clock, i),
            "supersede": lambda c, i: _store_newer(c, clock, "drafts", i),
        },
        "emails": {
            "close": lambda c, i: close_email(c, clock, i),
            "reopen": lambda c, i: reopen_email(c, clock, i),
            "supersede": lambda c, i: _store_newer(c, clock, "emails", i),
        },
        "bookings": {
            "confirm": lambda c, i: confirm_hold(c, clock, i),
            "release": lambda c, i: release_hold(c, clock, i),
            "cancel": lambda c, i: cancel_booking(c, clock, i),
        },
        "outbox": {},
    }
    return by_table[table] | direct


def _store_newer(conn: Conn, clock: FixedClock, table: str, row_id: int) -> object:
    """Store a newer email from the row's sender (D48 superseding). It inserts a new
    email, so the attempt "goes through"; rule 6 is about the final row, which must not
    change."""
    query = (
        "SELECT e.from_address FROM drafts d JOIN emails e ON e.id = d.email_id WHERE d.id = %s"
        if table == "drafts"
        else "SELECT from_address FROM emails WHERE id = %s"
    )
    row = conn.execute(query, (row_id,)).fetchone()
    assert row is not None
    return store_email(conn, clock, incoming(
        message_id=f"<newer-{table}-{row_id}@example.test>", from_header=row[0],
        subject="One more thing", body="b", received_at=clock.now(),
    ))  # fmt: skip


def _row(conn: Conn, table: str, row_id: int) -> str | None:
    query = sql.SQL("SELECT to_jsonb(t)::text FROM {} t WHERE id = %s").format(
        sql.Identifier(table)
    )
    found = conn.execute(query, (row_id,)).fetchone()
    return None if found is None else str(found[0])


def changed_finals(world: World) -> list[str]:
    """Run every action on every final row; report any action after which that row
    differs (or is gone). Every attempt is rolled back, whatever happened."""
    changed: list[str] = []
    for table, row_id, status in final_rows(world.conn):
        for name, action in actions_for(table, world.clock).items():
            before = _row(world.conn, table, row_id)
            try:
                action(world.conn, row_id)
                after = _row(world.conn, table, row_id)
            except (TransitionRefused, RestrictViolation):
                after = before  # refused: nothing written (and rolled back below)
            finally:
                world.conn.rollback()
            if after != before:
                changed.append(f"{name} on {table} {row_id} ({status})")
    return changed


def test_rule_6_nothing_changes_a_final_status(world: World) -> None:
    """Also covers audit_log: refusals at this layer are TransitionRefused or the D51
    trigger, both before or instead of any write, and every attempt is rolled back, so
    no refusal row can appear (the API writes those in its own transaction, step 16).
    The snapshot includes audit_log and must be identical."""
    before = snapshot(world.conn)
    assert len(final_rows(world.conn)) >= 15
    assert changed_finals(world) == []
    assert snapshot(world.conn) == before
    assert_rules_1_to_4(world.conn)


def test_rule_6_checker_finds_a_final_row_that_changes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planted: an action that switches off the D51 trigger for its transaction and
    edits a final draft, as a bug or a careless migration could. The checker reports it,
    and the rollback leaves the scenario unchanged."""
    original = actions_for

    def with_planted(table: str, clock: FixedClock) -> dict[str, Callable[[Conn, int], object]]:
        actions = original(table, clock)
        if table == "drafts":
            actions["planted"] = lambda c, i: (
                c.execute("ALTER TABLE drafts DISABLE TRIGGER drafts_final_status"),
                c.execute("UPDATE drafts SET route = 'policy' WHERE id = %s", (i,)),
            )
        return actions

    before = snapshot(world.conn)
    monkeypatch.setitem(globals(), "actions_for", with_planted)
    reported = changed_finals(world)
    assert reported, "the planted change was not reported"
    assert all(item.startswith("planted on drafts") for item in reported)
    assert snapshot(world.conn) == before
