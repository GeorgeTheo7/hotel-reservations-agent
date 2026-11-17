"""A run end to end (D17, D70, D71): store -> claim -> extract -> route -> the agent
(loop, tools, final write), set aside or escalate, through the real code, on a
committed database.

Every evaluation case runs on its own (docs/email-format.md: each file is one email on
its own): an emptied database (committed_db truncates every operational table before
each test, and the run checks it is empty), the case's own `world` bookings, then the
run. Every case ends exactly as labelled (status, draft route, reason codes, holds);
the two that once lacked a second code (el-group-wedding, el-complaint-legal) get it
from the escalation summary (D81). A case also calls every labelled
required tool and retrieves every labelled policy id. No network: every model call is
answered by the gold client (anemos.gold) from the case's labels. Superseding (D48) is
tested on purpose, below (with the stub drafter, as the demo used to run).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.clock import FixedClock
from anemos.email_cases import EmailCase, load_cases
from anemos.extraction import ExtractionFlag
from anemos.extraction_run import process_email
from anemos.gold import GoldClient
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, ModelClient, Prompt, Settings
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, ScriptedClient, Step
from anemos.mailbox import IncomingEmail
from anemos.reservations.formatting import Lang, format_money
from anemos.run import agent_finish, process_inbox, route_and_draft

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]
type Outcome = tuple[str, tuple[str, ...], int]  # route, codes, holds

CASES = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=100)
EXACT = sorted(CASES)


def in_world(committed_db: CommittedDb, case: EmailCase) -> None:
    """The case's world on an empty database: nothing from another case can leak in."""
    conn = committed_db.connect()
    left = conn.execute(
        "SELECT (SELECT count(*) FROM emails), (SELECT count(*) FROM bookings),"
        " (SELECT count(*) FROM drafts), (SELECT count(*) FROM quotes)"
    ).fetchone()
    assert left == (0, 0, 0, 0), f"{case.id}: the database is not empty: {left}"
    rows = committed_db.rows(conn)
    for b in case.world.bookings:
        rows.booking(room_id=b.room, stay=f"[{b.check_in},{b.check_out})", status=b.status,
                     expires_at=b.expires_at)  # fmt: skip
    conn.commit()


def make_llm(committed_db: CommittedDb, steps: list[Step], clock: FixedClock,
             tmp: Path) -> tuple[LLM, ScriptedClient]:  # fmt: skip
    client = ScriptedClient.of(steps)
    budget = Budget(clock, 1_000_000, tmp / "llm_spend.jsonl")
    return LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None), client


def stored(committed_db: CommittedDb, case: EmailCase) -> tuple[int, FixedClock]:
    clock = FixedClock(case.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, case.incoming()).email_id
    conn.commit()
    return email_id, clock


def one(committed_db: CommittedDb, query: str, *params: Any) -> Any:
    conn = committed_db.connect()
    row = conn.execute(query, params).fetchone()
    conn.commit()
    return row


def run_case(
    committed_db: CommittedDb, case: EmailCase, tmp: Path, answer: Any = None
) -> tuple[int, ModelClient]:
    """The production run (the agent), every model call answered by the gold client;
    `answer` replaces the extraction (then nothing past routing may call a model)."""
    in_world(committed_db, case)
    email_id, clock = stored(committed_db, case)
    client: ModelClient = GoldClient([case]) if answer is None else ScriptedClient.of(
        [Answer(answer)])  # fmt: skip
    budget = Budget(clock, 1_000_000, tmp / "llm_spend.jsonl")
    llm = LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)
    process_email(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    return email_id, client


def tools_used(committed_db: CommittedDb, email_id: int) -> tuple[list[str], set[str]]:
    """The tools the agent called, and every policy id search_policies returned."""
    conn = committed_db.connect()
    rows = conn.execute("SELECT output FROM audit_log WHERE event = 'agent.tool_called'"
                        " AND email_id = %s ORDER BY id", (email_id,)).fetchall()  # fmt: skip
    conn.commit()
    called = [r[0]["tool"] for r in rows]
    retrieved = {h["policy_id"] for r in rows for h in r[0].get("record", {}).get("hits", [])}
    return called, retrieved


def outcome(committed_db: CommittedDb, email_id: int) -> Outcome:
    status, codes, route, holds = one(
        committed_db,
        "SELECT e.status, e.reason_codes, d.route,"
        " (SELECT count(*) FROM draft_offers o WHERE o.draft_id = d.id AND o.hold)"
        " FROM emails e LEFT JOIN drafts d ON d.email_id = e.id WHERE e.id = %s",
        email_id,
    )
    if status == "set_aside":
        return ("set_aside", (), 0)
    if status == "escalated":
        return ("escalate", tuple(sorted(codes)), 0)
    assert status == "awaiting_approval", status
    return (route, (), holds)


def gold(case: EmailCase) -> Outcome:
    codes = tuple(sorted(c.value for c in case.labels.reason_codes))
    return (case.labels.route, codes, case.labels.holds)


def routed(committed_db: CommittedDb, email_id: int) -> dict[str, Any]:
    row = one(committed_db, "SELECT output FROM audit_log WHERE event = 'email.routed'"
              " AND email_id = %s", email_id)  # fmt: skip
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


# --- every evaluation case end to end ---------------------------------------------------


def test_every_case_is_counted_once() -> None:
    assert len(CASES) == 55 and len(EXACT) == 55


@pytest.mark.parametrize("case_id", EXACT)
def test_a_run_ends_as_labelled(committed_db: CommittedDb, tmp_path: Path, case_id: str) -> None:
    case = CASES[case_id]
    email_id, _ = run_case(committed_db, case, tmp_path)
    assert outcome(committed_db, email_id) == gold(case)
    called, retrieved = tools_used(committed_db, email_id)
    assert set(case.labels.required_tools) <= set(called), called
    assert set(case.labels.policy_ids) <= retrieved, retrieved


QUOTED = [c for c in EXACT if CASES[c].labels.route == "quote"]


@pytest.mark.parametrize("case_id", QUOTED)
def test_each_quote_block_shows_the_stored_quotes_amounts(
    committed_db: CommittedDb, tmp_path: Path, case_id: str
) -> None:
    """Approval re-prices from the stored quotes row (D47), so the block the guest reads
    must show that row's amounts: the accommodation total, city tax, deposit and every
    nightly rate. The block is rendered from the quote the tool returned (in memory)."""
    email_id, _ = run_case(committed_db, CASES[case_id], tmp_path)
    conn = committed_db.connect()
    rows = conn.execute(
        "SELECT o.rendered_block, q.accommodation_subtotal_cents, q.city_tax_cents,"
        " q.deposit_cents, q.lines, e.language FROM draft_offers o"
        " JOIN drafts d ON d.id = o.draft_id JOIN emails e ON e.id = d.email_id"
        " JOIN quotes q ON q.id = o.quote_id WHERE d.email_id = %s", (email_id,),
    ).fetchall()  # fmt: skip
    conn.commit()
    assert rows, "no offer"
    for block, subtotal, city_tax, deposit, lines, language in rows:
        lang: Lang = "el" if language == "el" else "en"
        rates = {line["room_rate_cents"] for line in lines}
        for cents in (subtotal, city_tax, deposit, *rates):
            assert format_money(cents, lang) in block, (cents, block)


# --- single behaviours ---------------------------------------------------------------------


def test_a_thread_reply_is_escalated_without_a_model_call(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    email_id, client = run_case(committed_db, CASES["en-thread-reply"], tmp_path)
    assert isinstance(client, GoldClient) and client.calls == []
    assert outcome(committed_db, email_id) == ("escalate", ("THREAD_REPLY",), 0)
    assert routed(committed_db, email_id)["codes"] == ["THREAD_REPLY"]
    assert one(committed_db, "SELECT count(*) FROM requests") == (0,)


def test_spam_is_set_aside_and_audited(committed_db: CommittedDb, tmp_path: Path) -> None:
    email_id, _ = run_case(committed_db, CASES["en-spam-seo"], tmp_path)
    assert outcome(committed_db, email_id) == ("set_aside", (), 0)
    assert routed(committed_db, email_id) == {"outcome": "set_aside"}
    assert one(committed_db, "SELECT count(*) FROM audit_log WHERE event = 'email.set_aside'"
               " AND email_id = %s", email_id) == (1,)  # fmt: skip


def test_every_reason_code_is_stored(committed_db: CommittedDb, tmp_path: Path) -> None:
    case = CASES["en-quote-sea-view-june"]
    base = case.gold_output()
    assert base is not None
    flags = [ExtractionFlag.MEDICAL_NEED, ExtractionFlag.LEGAL_THREAT]
    flagged = base.model_copy(update={"flags": flags})
    email_id, _ = run_case(committed_db, case, tmp_path, answer=flagged)
    assert outcome(committed_db, email_id) == ("escalate", ("LEGAL", "MEDICAL"), 0)
    assert one(committed_db, "SELECT reason_codes FROM emails WHERE id = %s", email_id) == (
        ["LEGAL", "MEDICAL"],
    )  # fmt: skip


def test_the_routers_decision_is_audited_even_when_the_tools_change_it(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """Routed to quote; the tools find nothing free and the draft declines (D60). The
    audit keeps both, so the eval can tell routing apart from availability."""
    email_id, _ = run_case(committed_db, CASES["en-fully-booked-decline"], tmp_path)
    assert routed(committed_db, email_id) == {"outcome": "to_agent", "route": "quote",
                                              "detail": ""}  # fmt: skip
    assert outcome(committed_db, email_id) == ("decline", (), 0)


def test_process_inbox_runs_every_received_email_once(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """Three different guests, so no email supersedes another (that is tested below)."""
    ids = ["en-quote-plus-breakfast", "en-spam-seo", "en-thread-reply"]
    cases = [CASES[i] for i in ids]
    assert len({c.incoming().from_address for c in cases}) == 3
    clock = FixedClock(max(c.email.received_at for c in cases))
    conn = committed_db.connect()
    stored_ids = [store_email(conn, clock, c.incoming()).email_id for c in cases]
    conn.commit()
    by_body = {c.email.body.strip(): c.gold_output() for c in cases if c.gold_output()}

    def answer(prompt: Prompt) -> Answer:
        body = next(b for b in by_body if b in prompt.input)
        found = by_body[body]
        assert found is not None
        return Answer(found)

    llm, client = make_llm(committed_db, [answer, answer], clock, tmp_path)
    results = process_inbox(committed_db.connect, clock, llm, finish=route_and_draft)
    assert sorted(r.email_id for r in results) == sorted(stored_ids)
    assert len(client.sent) == 2  # the thread reply made no call
    assert [outcome(committed_db, i) for i in stored_ids] == [
        ("quote", (), 1), ("set_aside", (), 0), ("escalate", ("THREAD_REPLY",), 0),
    ]  # fmt: skip
    assert process_inbox(committed_db.connect, clock, llm, finish=route_and_draft) == []


# --- superseding (D48), on purpose ------------------------------------------------------

GUEST = CASES["en-quote-plus-breakfast"]


def second_email(hours_later: int) -> IncomingEmail:
    """A newer email from the same guest (same address, later received_at)."""
    first = GUEST.incoming()
    return replace(first, message_id="<second@cases.example.test>",
                   received_at=first.received_at + timedelta(hours=hours_later))  # fmt: skip


def statuses(committed_db: CommittedDb, email_id: int) -> tuple[str, str | None]:
    row = one(
        committed_db,
        "SELECT e.status, d.status FROM emails e LEFT JOIN drafts d ON d.email_id = e.id"
        " WHERE e.id = %s",
        email_id,
    )
    return (str(row[0]), None if row[1] is None else str(row[1]))


def test_a_newer_email_supersedes_the_older_pending_draft(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """The older email is drafted first; storing the newer one supersedes that draft."""
    gold_out = GUEST.gold_output()
    assert gold_out is not None
    clock = FixedClock(GUEST.email.received_at + timedelta(hours=2))
    llm, _ = make_llm(committed_db, [Answer(gold_out), Answer(gold_out)], clock, tmp_path)
    conn = committed_db.connect()
    older = store_email(conn, clock, GUEST.incoming()).email_id
    conn.commit()
    process_inbox(committed_db.connect, clock, llm, finish=route_and_draft)
    assert statuses(committed_db, older) == ("awaiting_approval", "pending_approval")

    newer = store_email(conn, clock, second_email(1)).email_id
    conn.commit()
    assert statuses(committed_db, older) == ("superseded", "superseded")
    process_inbox(committed_db.connect, clock, llm, finish=route_and_draft)
    assert statuses(committed_db, newer) == ("awaiting_approval", "pending_approval")


def test_a_draft_is_born_superseded_when_a_newer_email_is_already_stored(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """Both emails are stored before either is run: the older one's draft is born
    superseded, whichever order the runs happen in."""
    gold_out = GUEST.gold_output()
    assert gold_out is not None
    clock = FixedClock(GUEST.email.received_at + timedelta(hours=2))
    conn = committed_db.connect()
    older = store_email(conn, clock, GUEST.incoming()).email_id
    newer = store_email(conn, clock, second_email(1)).email_id
    conn.commit()
    llm, _ = make_llm(committed_db, [Answer(gold_out), Answer(gold_out)], clock, tmp_path)
    process_inbox(committed_db.connect, clock, llm, finish=route_and_draft)
    assert statuses(committed_db, older) == ("superseded", "superseded")
    assert statuses(committed_db, newer) == ("awaiting_approval", "pending_approval")
