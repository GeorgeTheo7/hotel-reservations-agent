"""The agent's read tools through ToolRunner, on a committed database (D11, D12, D71).

Each call is a short transaction on the run's one connection, committed before the
model sees the answer; the answer carries no money amount; rule escalations and
NO_MATCH end the loop; and no tool writes bookings or the outbox (D12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from anemos.agent.runner import Answer, Ends, QuoteSeen, ToolRunner
from anemos.agent.tools import NoArgs, SearchPoliciesArgs
from anemos.clock import FixedClock
from anemos.llm.adapter import LLM, Settings
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer as ModelAnswer
from anemos.llm.fake import ScriptedClient
from anemos.reservations.errors import ReasonCode

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

CLOCK = FixedClock(datetime(2027, 3, 1, 9, 0, tzinfo=UTC))
JULY = Range(date(2027, 7, 5), date(2027, 7, 9), "[)")
NO_ARGS = NoArgs()


@pytest.fixture
def conn(committed_db: CommittedDb) -> Iterator[Conn]:
    yield committed_db.connect()


def llm(tmp_path: Path, *steps: Any) -> LLM:
    budget = Budget(CLOCK, 1_000_000, tmp_path / "ledger.jsonl")
    return LLM(ScriptedClient.of(steps), Settings("gpt-6-luna", 100), budget, None, CLOCK)


def runner(conn: Conn, committed_db: CommittedDb, tmp_path: Path, *steps: Any,
           **request: Any) -> ToolRunner:  # fmt: skip
    rows = committed_db.rows(conn)
    email_id = rows.email(status="processing")
    extracted = Jsonb({"date_spec": {"kind": "exact"}, "flags": []})
    fields = {"stay": JULY, "adults": 2, "children": 0, "child_ages": [], "rooms": 1,
              "room_type_id": "sea_view", "extracted": extracted} | request  # fmt: skip
    request_id = rows.request(email_id, **fields)
    conn.commit()
    return ToolRunner(conn, CLOCK, llm(tmp_path, *steps), email_id, request_id)


def counts(committed_db: CommittedDb) -> tuple[int, int, int, int]:
    """bookings, outbox, quotes, agent.tool_called rows, from another connection: only
    committed rows are seen."""
    row = (
        committed_db.connect()
        .execute(
            "SELECT (SELECT count(*) FROM bookings), (SELECT count(*) FROM outbox),"
            " (SELECT count(*) FROM quotes),"
            " (SELECT count(*) FROM audit_log WHERE event = 'agent.tool_called')"
        )
        .fetchone()
    )
    assert row is not None
    return row


def test_quote_price_commits_its_quote_and_audit_row(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    tools = runner(conn, committed_db, tmp_path)
    result = tools.run("quote_price", NO_ARGS)
    assert isinstance(result, Answer)
    [quote] = result.output["quotes"]
    assert result.output["result"] == "QUOTED"
    assert quote == {"quote_id": quote["quote_id"], "room_type": "sea_view", "rooms": 1,
                     "nights": 4, "changes": []}  # fmt: skip
    assert result.quotes == (QuoteSeen(quote["quote_id"], hold_allowed=True),)
    assert conn.info.transaction_status == TransactionStatus.IDLE  # nothing left open
    assert counts(committed_db)[2:] == (1, 1)  # committed, seen from another connection


def test_no_answer_carries_a_money_amount(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    """The model never sees a price (D8): prices reach the guest only in {{QUOTE_BLOCK}}."""
    tools = runner(conn, committed_db, tmp_path)
    for name in ("check_availability", "quote_price"):
        result = tools.run(name, NO_ARGS)
        assert isinstance(result, Answer)
        text = json.dumps(result.output)
        assert "cents" not in text and "€" not in text and "price" not in text, text


def test_the_tools_never_write_bookings_or_the_outbox(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    """D12, end to end: every read tool, including alternatives on a full room type."""
    rows = committed_db.rows(conn)
    for room in conn.execute("SELECT id FROM rooms WHERE room_type_id = 'sea_view'").fetchall():
        rows.booking(room_id=room[0], stay=JULY)
    conn.commit()
    tools = runner(
        conn, committed_db, tmp_path, ModelAnswer({"policy_ids": ["pets-assistance-dogs"]})
    )
    before = counts(committed_db)[:2]
    priced = tools.run("quote_price", NO_ARGS)
    assert isinstance(priced, Answer) and priced.output["result"] == "FULLY_BOOKED"
    found = tools.run("suggest_alternatives", NO_ARGS)
    assert isinstance(found, Answer) and found.output["result"] == "ALTERNATIVES"
    assert all(not q.hold_allowed for q in found.quotes)  # other types: never held (D9)
    tools.run("check_availability", NO_ARGS)
    tools.run("search_policies", SearchPoliciesArgs(query="Can I bring my dog?"))
    assert counts(committed_db)[:2] == before


def test_a_closed_stay_is_kept_with_its_year(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    tools = runner(conn, committed_db, tmp_path,
                   stay=Range(date(2027, 1, 10), date(2027, 1, 13), "[)"))  # fmt: skip
    result = tools.run("quote_price", NO_ARGS)
    assert isinstance(result, Answer)
    assert (result.output, result.outcome, result.closed_year) == ({"result": "CLOSED"},
                                                                   "CLOSED", 2027)  # fmt: skip


def test_a_rule_escalation_ends_the_loop(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    """D11: the model may escalate, never un-escalate: GROUP comes back as Ends."""
    tools = runner(conn, committed_db, tmp_path, rooms=3, adults=6)
    result = tools.run("quote_price", NO_ARGS)
    assert isinstance(result, Ends) and result.code == ReasonCode.GROUP
    assert counts(committed_db)[3] == 1  # still on record


def test_a_window_is_refused_by_the_one_stay_tools(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    """check_availability and suggest_alternatives take one stay; a window's options
    come from quote_price (D7). A refusal, not a crash."""
    window = {"date_spec": {"kind": "approximate", "part": "late", "month": 6, "year": 2027,
                            "nights": 7}, "flags": []}  # fmt: skip
    tools = runner(conn, committed_db, tmp_path, stay=None, extracted=Jsonb(window))
    for name in ("check_availability", "suggest_alternatives"):
        result = tools.run(name, NO_ARGS)
        assert isinstance(result, Answer) and result.output["error"] == "NOT_FOR_THIS_REQUEST"
    options = tools.run("quote_price", NO_ARGS)
    assert isinstance(options, Answer) and options.output["result"] == "WINDOW_OPTIONS"


def test_search_policies_returns_texts_and_records_them(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    tools = runner(
        conn, committed_db, tmp_path, ModelAnswer({"policy_ids": ["pets-assistance-dogs"]})
    )
    result = tools.run("search_policies", SearchPoliciesArgs(query="Can I bring my dog?"))
    assert isinstance(result, Answer)
    [section] = result.output["sections"]
    assert section["policy_id"] == "pets-assistance-dogs"
    assert [h.policy_id for h in result.hits] == ["pets-assistance-dogs"]
    row = (
        committed_db.connect()
        .execute("SELECT output FROM audit_log WHERE event = 'agent.tool_called'")
        .fetchone()
    )
    assert row is not None
    record = row[0]["record"]
    assert record["hits"][0]["text"] == section["text"] and record["policies_sha256"]


def test_no_match_ends_the_loop_with_no_policy(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    """D73: never answered from nothing."""
    tools = runner(conn, committed_db, tmp_path, ModelAnswer({"policy_ids": []}))
    result = tools.run("search_policies", SearchPoliciesArgs(query="Is there a gym?"))
    assert isinstance(result, Ends) and result.code == ReasonCode.NO_POLICY


def test_an_invalid_query_goes_back_to_the_model(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    tools = runner(conn, committed_db, tmp_path)  # no selector step: no model call
    result = tools.run("search_policies", SearchPoliciesArgs(query="Σκύλος;"))
    assert isinstance(result, Answer) and result.output["error"] == "INVALID_QUERY"


def test_a_failing_tool_rolls_back_and_raises(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    tools = runner(conn, committed_db, tmp_path)
    tools.request_id = 999_999  # no such request: a bug, not a business error
    with pytest.raises(LookupError):
        tools.run("quote_price", NO_ARGS)
    assert conn.info.transaction_status == TransactionStatus.IDLE
    assert counts(committed_db)[3] == 0


def test_the_reservation_tools_need_a_request(
    conn: Conn, committed_db: CommittedDb, tmp_path: Path
) -> None:
    tools = runner(conn, committed_db, tmp_path)
    tools.request_id = None  # a policy question: no reservation tool is ever sent
    with pytest.raises(ValueError, match="needs a stored request"):
        tools.run("quote_price", NO_ARGS)
