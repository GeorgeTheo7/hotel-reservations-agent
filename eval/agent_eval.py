"""The agent eval: every dev case through the production run with a real model
(extraction, the policy selector and the agent), scored against the pass bar fixed in
docs/NOTES.md before the first run (anemos.agent_evaluation).

    uv run --env-file .env python eval/agent_eval.py --model gpt-6-luna

Each case runs on its own: an emptied scratch database (`<dev db>_agent_test`, never
the dev database), its own `world` bookings, then store -> claim -> extract -> route ->
the agent (anemos.run.agent_finish). What the run left behind (the email, its draft,
the audit rows) is collected per case. A case that ended SYSTEM_ERROR because of a
provider failure after the adapter's retry (a 5xx or a timeout) is run once more, and
both attempts are recorded; any other SYSTEM_ERROR counts.

Before any call the worst-case cost is estimated from the adapter's reservations; over
--max-usd (default 1.00) nothing is sent. Refuses a dirty working tree and held-out
cases. Writes eval/results/<time>-agent-dev-<model>.json before printing the report.
Never run by pytest or CI: tests call main() with the gold client or a scripted one.
"""

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import TupleRow

import anemos.db.scratch as scratch
from anemos.agent.loop import MAX_CALLS, first_message
from anemos.agent.tools import TOOLS_BY_ROUTE
from anemos.agent_evaluation import AgentCase, ToolUse, bar, provider_failure, reported
from anemos.audit import run_cost
from anemos.clock import Clock, FixedClock, SystemClock
from anemos.db.connection import connect
from anemos.email_cases import EmailCase
from anemos.evaluation import (
    CaseOutcome,
    Row,
    SetError,
    dev_cases,
    estimate_max_cost,
    git_state,
    raw_score,
    score,
)
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import process_email
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, ModelClient, Settings, Transcript, UserText
from anemos.llm.adapter import max_turn_input_tokens as turn_input_bound
from anemos.llm.budget import Budget, cap_from_env
from anemos.llm.openai_client import OpenAIClient
from anemos.llm.pricing import PRICES, max_cost
from anemos.policies import load_policies
from anemos.policies.search import selector_prompt
from anemos.prompts import CURRENT_AGENT, CURRENT_EXTRACTION, CURRENT_POLICY_SELECT
from anemos.run import agent_finish

type Conn = psycopg.Connection[TupleRow]

REPO = Path(__file__).resolve().parents[1]
EVAL = REPO / "eval"
WORLD_PRICE_CENTS = 0  # a world booking only blocks a room; its price is never read


def main(
    argv: list[str] | None = None,
    *,
    client: ModelClient | None = None,
    clock: Clock | None = None,
    budget: Budget | None = None,
    repo: Path = REPO,
    eval_dir: Path = EVAL,
    conninfo: str | None = None,
    timer: Callable[[], float] = time.monotonic,
) -> int:
    parser = argparse.ArgumentParser(prog="eval/agent_eval.py",
                                     description=__doc__.split("\n")[0])  # fmt: skip
    parser.add_argument("--model", required=True, choices=sorted(PRICES))
    parser.add_argument("--case", action="append", default=[], dest="cases",
                        help="run only this dev case (repeatable)")  # fmt: skip
    parser.add_argument("--max-usd", type=float, default=1.00)
    parser.add_argument("--results-dir", type=Path, default=EVAL / "results")
    args = parser.parse_args(argv)

    commit, dirty = git_state(repo)
    if dirty:
        print("the working tree has uncommitted changes: commit them first", file=sys.stderr)
        return 2
    try:
        cases = dev_cases(eval_dir, args.cases)
    except SetError as exc:
        print(exc, file=sys.stderr)
        return 2
    estimate = worst_case(cases, args.model)
    print(f"{len(cases)} cases, worst-case cost {estimate} µ$ (${estimate / 1e6:.4f})")
    if estimate > args.max_usd * 1_000_000:
        print(f"over --max-usd {args.max_usd}: nothing sent", file=sys.stderr)
        return 3

    clock = clock or SystemClock()
    started = clock.now()
    conninfo = conninfo or scratch.prepare("agent")
    settings = Settings(args.model, CURRENT_EXTRACTION.max_output_tokens,
                        CURRENT_EXTRACTION.reasoning_effort)  # fmt: skip
    llm = LLM(client or OpenAIClient(), settings, budget or Budget(clock, cap_from_env()),
              lambda: connect(conninfo), clock)  # fmt: skip
    results = [run_case(conninfo, llm, case, timer) for case in cases]
    lines, extra = bar(results), reported(results)
    # Written before anything is printed: the calls are paid for (docs/NOTES.md).
    path = write(args.results_dir, started, args.model, commit, estimate, results, lines, extra)
    print(report(results, lines, extra))
    print(f"\nwritten: {path}")
    return 0


# --- the worst case --------------------------------------------------------------------------

# Per turn: a tool call and its answer. The largest answer is three policy sections
# (1,155 bytes as sent, 2026-11-17); a test keeps the allowance above it.
GROWTH_BYTES = 2_000


# A turn that reaches the selector wrote a valid search: a query of at most 300 ASCII
# characters (anything else is an invalid query, no selector call), so at most ~350 bytes
# of output with the call's JSON, and a token is at least one byte.
SEARCH_OUTPUT_TOKENS = 400


def worst_case(cases: Sequence[EmailCase], model: str) -> int:
    """The most the cases can cost: extraction, then 8 agent turns, each resending a
    transcript grown by GROWTH_BYTES per turn. Each turn either uses the whole output
    limit, or writes a valid search (a short output) and adds one selector call:
    whichever costs more is counted, never both."""
    extraction = estimate_max_cost(cases, CURRENT_EXTRACTION, model)
    longest = max((len(c.text.encode()) for c in load_policies()), default=0)
    assert 3 * longest + 500 <= GROWTH_BYTES, "a policy answer outgrows the estimate"
    sp = selector_prompt("q" * 300)
    selector = max_cost(model, max_input_tokens=len((sp.instructions + sp.input).encode()) + 64,
                        max_output_tokens=CURRENT_POLICY_SELECT.max_output_tokens)  # fmt: skip
    total = extraction
    for case in cases:
        message = first_message("quote", "", case.labels.language, case.email.subject,
                                case.email.body.strip())  # fmt: skip
        base = turn_input_bound(Transcript(CURRENT_AGENT.text(), (UserText(message),)),
                                TOOLS_BY_ROUTE["quote"])  # fmt: skip
        for turn in range(MAX_CALLS):
            sent = base + turn * GROWTH_BYTES
            full = max_cost(model, max_input_tokens=sent,
                            max_output_tokens=CURRENT_AGENT.max_output_tokens)  # fmt: skip
            search = max_cost(model, max_input_tokens=sent,
                              max_output_tokens=SEARCH_OUTPUT_TOKENS) + selector  # fmt: skip
            total += max(full, search)
    return total


# --- one case ---------------------------------------------------------------------------------


def run_case(conninfo: str, llm: LLM, case: EmailCase, timer: Callable[[], float]) -> AgentCase:
    """One case on its own; a provider failure after the adapter's retry is run once
    more, and the first attempt's error is kept."""
    first = _attempt(conninfo, llm, case, timer)
    if first.system_error is None or not provider_failure(first.system_error):
        return first
    again = _attempt(conninfo, llm, case, timer)
    again.reran, again.first_error = True, first.system_error
    return again


def _attempt(conninfo: str, llm: LLM, case: EmailCase, timer: Callable[[], float]) -> AgentCase:
    scratch.truncate(conninfo)
    clock = FixedClock(case.email.received_at)
    with connect(conninfo) as conn:
        assert scratch.is_empty(conn), case.id
        insert_world(conn, case, clock.now())
        email_id = store_email(conn, clock, case.incoming()).email_id
        before = _write_counts(conn)
        conn.commit()
    start = timer()
    process_email(lambda: connect(conninfo), clock, llm, email_id, finish=agent_finish(llm))
    latency = round((timer() - start) * 1000)
    with connect(conninfo) as conn:
        collected = collect(conn, case, email_id, llm.settings.model)
        written = _write_counts(conn)
        conn.commit()
    if written != before:
        collected.unsafe.append(f"bookings/outbox rows {before} -> {written}")
    collected.latency_ms = latency
    return collected


def insert_world(conn: Conn, case: EmailCase, created_at: datetime) -> None:
    """The case's bookings, inserted directly (scratch setup; as in extraction_eval.py)."""
    for b in case.world.bookings:
        conn.execute(
            "INSERT INTO bookings (room_id, stay, status, expires_at, price_cents, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (b.room, f"[{b.check_in},{b.check_out})", b.status, b.expires_at,
             WORLD_PRICE_CENTS, created_at),
        )  # fmt: skip


def _write_counts(conn: Conn) -> tuple[int, int]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM bookings), (SELECT count(*) FROM outbox)"
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def collect(conn: Conn, case: EmailCase, email_id: int, model: str) -> AgentCase:
    """Everything the bar and the report read, from what the run left behind."""
    labels = case.labels
    status, codes, route, holds, body, draft_id = conn.execute(
        "SELECT e.status, e.reason_codes, d.route,"
        " (SELECT count(*) FROM draft_offers o WHERE o.draft_id = d.id AND o.hold),"
        " (SELECT v.body FROM draft_versions v WHERE v.draft_id = d.id AND v.version = 1), d.id"
        " FROM emails e LEFT JOIN drafts d ON d.email_id = e.id WHERE e.id = %s", (email_id,),
    ).fetchone() or (None, None, None, 0, None, None)  # fmt: skip
    codes = tuple(codes or ())
    calls = conn.execute(
        "SELECT event, step, cost_micro_usd, output_tokens, error, output FROM audit_log"
        " WHERE email_id = %s AND event IN ('llm.called', 'llm.failed') ORDER BY id",
        (email_id,),
    ).fetchall()
    tools = conn.execute(
        "SELECT output FROM audit_log WHERE email_id = %s AND event = 'agent.tool_called'"
        " ORDER BY id", (email_id,),
    ).fetchall()  # fmt: skip
    escalated = conn.execute(
        "SELECT output->>'detail' FROM audit_log WHERE email_id = %s"
        " AND event = 'email.escalated' ORDER BY id DESC LIMIT 1", (email_id,),
    ).fetchone()  # fmt: skip
    failed_extraction = not any(e == "llm.called" and s == "extract" for e, s, *_ in calls)
    outcome = CaseOutcome(
        case.id, labels.route, tuple(c.value for c in labels.reason_codes), labels.holds,
        route if route is not None else ("escalate" if status == "escalated" else str(status)),
        codes, int(holds or 0), failed_extraction,
    )  # fmt: skip
    agent = [(e, out, err, tokens) for e, s, _, tokens, err, out in calls if s == "agent"]
    rejected = [err for e, _, err, _ in agent if e == "llm.failed" and _rejection(err)]
    write_calls = [name for name in ("hold_room", "send_reply")
                   for _, _, err, _ in agent if err and f"'{name}'" in err]  # fmt: skip
    drafts = [
        out for e, out, _, _ in agent if e == "llm.called" and out.get("tool") == "write_draft"
    ]
    found = AgentCase(
        case_id=case.id, language=labels.language, outcome=outcome,
        extraction=_extraction_row(case, calls, model),
        required_tools=list(labels.required_tools), policy_ids=list(labels.policy_ids),
        forbidden_phrases=list(labels.forbidden_phrases),
        tools=[_tool_use(r[0]) for r in tools],
        retrieved=sorted({h["policy_id"] for r in tools
                          for h in r[0].get("record", {}).get("hits", [])}),
        rejected=rejected, write_tool_calls=write_calls,
        agent_turns=sum(1 for e, _, err, _ in agent if e == "llm.called" or err in rejected),
        max_output_tokens=max((t or 0 for e, _, _, t in agent if e == "llm.called"), default=0),
        system_error=(escalated[0] if escalated else "") if "SYSTEM_ERROR" in codes else None,
        model_text=drafts[-1]["arguments"]["text"] if drafts else None,
        draft_body=body, guardrail_fail="GUARDRAIL_FAIL" in codes,
        cost_micro_usd=run_cost(conn, email_id).cost_micro_usd,  # one definition (audit)
    )  # fmt: skip
    if draft_id is not None:
        found.unsafe += _offer_problems(conn, draft_id, email_id)
    return found


def _rejection(error: str | None) -> bool:
    """A call the adapter refused (D74: answered to the model and counted)."""
    return error is not None and ("UnknownTool" in error or "InvalidArguments" in error)


def _offer_problems(conn: Conn, draft_id: int, email_id: int) -> list[str]:
    """An offer outside this email's request, or a hold D9 forbids."""
    rows = conn.execute(
        "SELECT o.quote_id, o.hold, q.hold_allowed, r.email_id FROM draft_offers o"
        " JOIN quotes q ON q.id = o.quote_id JOIN requests r ON r.id = q.request_id"
        " WHERE o.draft_id = %s", (draft_id,),
    ).fetchall()  # fmt: skip
    problems = [f"quote {q} is not this email's" for q, _, _, e in rows if e != email_id]
    held = [q for q, hold, _, _ in rows if hold]
    if held and (len(rows) != 1 or not rows[0][2]):
        problems.append(f"hold on {held} breaks D9")
    return problems


def _tool_use(output: dict[str, Any]) -> ToolUse:
    if "ends" in output:
        return ToolUse(output["tool"], f"ENDS {output['ends']['code']}")
    answer = output.get("answer", {})
    detail = output.get("record", {}).get("invalid_query", "")
    return ToolUse(output["tool"], str(answer.get("result") or answer.get("error")), detail)


def _extraction_row(case: EmailCase, calls: Sequence[Any], model: str) -> Row:
    called = [(c, out) for e, s, c, _, _, out in calls if e == "llm.called" and s == "extract"]
    if not called:
        return Row(case.id, model, error="extraction failed", gold_kind=case.labels.kind)
    cost, output = called[-1]
    got = ExtractionOutput.model_validate(output["result"])
    gold = case.gold_output()
    assert gold is not None, case.id
    return Row(case.id, model, cost_micro_usd=cost, output=output["result"],
               fields=score(gold, got, case.email.received_at),
               raw_fields=raw_score(gold, got, case.email.received_at),
               gold_kind=gold.kind)  # fmt: skip


# --- report and record ------------------------------------------------------------------------


def report(cases: Sequence[AgentCase], lines: Sequence[Any], extra: dict[str, Any]) -> str:
    out = ["pass bar (docs/NOTES.md):"]
    for line in lines:
        out.append(f"  {'PASS' if line.passed else 'FAIL'}  {line.name}: {line.value}"
                   f" (target {line.target})")  # fmt: skip
        if line.detail and not line.passed:
            out.append(f"        {line.detail}")
    out.append("reported (outside the bar):")
    for key, value in extra.items():
        out.append(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    total = sum(c.cost_micro_usd for c in cases)
    out.append(f"cost: {total} µ$ (${total / 1e6:.4f}) for {len(cases)} cases")
    return "\n".join(out)


def write(
    results_dir: Path, started: datetime, model: str, commit: str, estimate: int,
    cases: Sequence[AgentCase], lines: Sequence[Any], extra: dict[str, Any],
) -> Path:  # fmt: skip
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{started:%Y%m%dT%H%M%SZ}-agent-dev-{model}.json"
    data = {
        "run": {
            "model": model, "commit": commit, "dirty": False, "started_at": started.isoformat(),
            "prompt_versions": {"extraction": CURRENT_EXTRACTION.version,
                                "agent": CURRENT_AGENT.version,
                                "policy_select": CURRENT_POLICY_SELECT.version},
            "settings": {"agent_max_output_tokens": CURRENT_AGENT.max_output_tokens,
                         "agent_reasoning_effort": CURRENT_AGENT.reasoning_effort,
                         "extraction_max_output_tokens": CURRENT_EXTRACTION.max_output_tokens,
                         "max_calls": MAX_CALLS},
            "worst_case_micro_usd": estimate,
        },
        "bar": [asdict(line) for line in lines],
        "reported": extra,
        "cases": [asdict(c) for c in cases],
    }  # fmt: skip
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # e.g. a Windows console on code page 1253
        sys.stdout.reconfigure(errors="replace")
    raise SystemExit(main())
