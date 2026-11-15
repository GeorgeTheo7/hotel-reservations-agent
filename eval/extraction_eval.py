"""The extraction eval: every dev case through the real run with a real model, scored
for extraction fields and for routing (week 4).

    uv run --env-file .env python eval/extraction_eval.py --model gpt-6-luna

One model call per case serves both scores. Each case runs on its own: an emptied
scratch database (`<dev db>_eval_test`, never the dev database), its own `world`
bookings, then store -> claim -> extract -> route -> draft (anemos.run). Fields are
scored from the model's answer as audited; routing from how the email ended (status,
draft route, reason codes, holds). Targets: SPEC Evaluation plan. Not-built-yet cases
(week 5) are scored for fields and reported apart for routing.

Before any call the worst-case cost is estimated (the adapter's reservation per case);
over --max-usd (default 0.10) nothing is sent. Refuses a dirty working tree and
held-out cases. Writes eval/results/<time>-extraction-dev-<model>.json. Never run by
pytest or CI: tests call main() with a scripted client.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import TupleRow

import anemos.db.scratch as scratch
from anemos.clock import Clock, FixedClock, SystemClock
from anemos.db.connection import connect
from anemos.email_cases import EmailCase
from anemos.evaluation import (
    ESCALATION_PRECISION_TARGET,
    ESCALATION_RECALL_TARGET,
    FIELD_TARGETS,
    FIELDS,
    NOT_BUILT_YET,
    ROUTE_ACCURACY_TARGET,
    CaseOutcome,
    RoutingScore,
    Row,
    SetError,
    dev_cases,
    estimate_max_cost,
    field_accuracy,
    git_state,
    meets,
    score,
    score_routing,
)
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import process_email
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, ModelClient, Settings
from anemos.llm.budget import Budget, cap_from_env
from anemos.llm.openai_client import OpenAIClient
from anemos.llm.pricing import PRICES
from anemos.prompts import CURRENT_EXTRACTION
from anemos.run import route_and_draft

type Conn = psycopg.Connection[TupleRow]

REPO = Path(__file__).resolve().parents[1]
EVAL = REPO / "eval"


@dataclass(frozen=True)
class CaseResult:
    row: Row  # fields, model output, tokens, cost, latency, error
    outcome: CaseOutcome
    gold: dict[str, Any]  # the labelled extraction, for showing misses


def main(
    argv: list[str] | None = None,
    *,
    client: ModelClient | None = None,
    clock: Clock | None = None,
    budget: Budget | None = None,
    repo: Path = REPO,
    eval_dir: Path = EVAL,
    conninfo: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="eval/extraction_eval.py",
                                     description=__doc__.split("\n")[0])  # fmt: skip
    parser.add_argument("--model", required=True, choices=sorted(PRICES))
    parser.add_argument("--case", action="append", default=[], dest="cases",
                        help="run only this dev case (repeatable)")  # fmt: skip
    parser.add_argument("--max-usd", type=float, default=0.10)
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
    spec = CURRENT_EXTRACTION
    estimate = estimate_max_cost(cases, spec, args.model)
    print(f"{len(cases)} cases, worst-case cost {estimate} µ$ (${estimate / 1e6:.4f})")
    if estimate > args.max_usd * 1_000_000:
        print(f"over --max-usd {args.max_usd}: nothing sent", file=sys.stderr)
        return 3

    clock = clock or SystemClock()
    started = clock.now()
    conninfo = conninfo or scratch.prepare("eval")
    settings = Settings(args.model, spec.max_output_tokens, spec.reasoning_effort)
    llm = LLM(client or OpenAIClient(), settings, budget or Budget(clock, cap_from_env()),
              lambda: connect(conninfo), clock)  # fmt: skip
    results = [run_case(conninfo, llm, case) for case in cases]

    accuracy = field_accuracy([r.row for r in results])
    routing = score_routing([r.outcome for r in results])
    print(report(results, accuracy, routing))
    path = write(args.results_dir, started, args.model, settings, spec.version, commit,
                 estimate, results, accuracy, routing)  # fmt: skip
    print(f"\nwritten: {path}")
    return 0


def run_case(conninfo: str, llm: LLM, case: EmailCase) -> CaseResult:
    """One case on its own: empty database, its world, the real run."""
    scratch.truncate(conninfo)
    clock = FixedClock(case.email.received_at)
    with connect(conninfo) as conn:
        assert scratch.is_empty(conn), case.id
        insert_world(conn, case, clock.now())
        email_id = store_email(conn, clock, case.incoming()).email_id
        conn.commit()
    run = process_email(lambda: connect(conninfo), clock, llm, email_id, finish=route_and_draft)
    gold = case.gold_output()
    assert gold is not None, case.id
    with connect(conninfo) as conn:
        return CaseResult(_row(conn, case, email_id, llm.settings.model),
                          _outcome(conn, case, email_id, failed=run.extraction is None),
                          gold.model_dump(mode="json"))  # fmt: skip


WORLD_PRICE_CENTS = 0  # a world booking only blocks a room; its price is never read


def insert_world(conn: Conn, case: EmailCase, created_at: datetime) -> None:
    """The case's bookings, inserted directly: setup of the scratch database, not
    something the app does (in src/ only the approval transaction creates bookings,
    tests/unit/test_write_boundaries.py)."""
    for b in case.world.bookings:
        conn.execute(
            "INSERT INTO bookings (room_id, stay, status, expires_at, price_cents, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (b.room, f"[{b.check_in},{b.check_out})", b.status, b.expires_at,
             WORLD_PRICE_CENTS, created_at),
        )  # fmt: skip


def _row(conn: Conn, case: EmailCase, email_id: int, model: str) -> Row:
    called = conn.execute(
        "SELECT input_tokens, output_tokens, cost_micro_usd, latency_ms, output FROM audit_log"
        " WHERE email_id = %s AND event = 'llm.called' ORDER BY id DESC LIMIT 1",
        (email_id,),
    ).fetchone()
    if called is None:
        errors = conn.execute(
            "SELECT string_agg(coalesce(error, output->>'detail', event), '; ' ORDER BY id)"
            " FROM audit_log WHERE email_id = %s AND event IN"
            " ('llm.failed', 'llm.refused_budget', 'email.escalated')",
            (email_id,),
        ).fetchone()
        return Row(case.id, model, error=(errors or [None])[0] or "no model answer")
    tokens_in, tokens_out, cost, latency, output = called
    got = ExtractionOutput.model_validate(output["result"])
    gold = case.gold_output()
    assert gold is not None, case.id
    return Row(
        case.id, model, response_model=output.get("response_model"), input_tokens=tokens_in,
        cached_input_tokens=output.get("cached_input_tokens"), output_tokens=tokens_out,
        reasoning_tokens=output.get("reasoning_tokens"), cost_micro_usd=cost,
        latency_ms=latency, attempts=output.get("attempt"),
        fields=score(gold, got, case.email.received_at), output=output["result"],
    )  # fmt: skip


def _outcome(conn: Conn, case: EmailCase, email_id: int, *, failed: bool) -> CaseOutcome:
    found = conn.execute(
        "SELECT e.status, e.reason_codes, d.route,"
        " (SELECT count(*) FROM draft_offers o WHERE o.draft_id = d.id AND o.hold)"
        " FROM emails e LEFT JOIN drafts d ON d.email_id = e.id WHERE e.id = %s",
        (email_id,),
    ).fetchone()
    assert found is not None
    status, codes, route, holds = found
    if route is None:
        route = "escalate" if status == "escalated" else str(status)
    return CaseOutcome(
        case.id, case.labels.route, tuple(c.value for c in case.labels.reason_codes),
        case.labels.holds, route, tuple(codes or ()), int(holds or 0), failed,
    )  # fmt: skip


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _verdict(value: float | None, target: float) -> str:
    return f"{_pct(value)} (target ≥ {target:.0%}: {'PASS' if meets(value, target) else 'FAIL'})"


def report(results: Sequence[CaseResult], accuracy: dict[str, float], routing: RoutingScore) -> str:
    rows = [r.row for r in results]
    cost = sum(r.cost_micro_usd or 0 for r in rows)
    lines = [f"cases {len(rows)}, failed calls {len(routing.failed)}, cost {cost} µ$ "
             f"(${cost / 1e6:.4f})", "", "extraction fields:"]  # fmt: skip
    for f in FIELDS:
        target = FIELD_TARGETS.get(f)
        lines.append(
            f"  {f:13} " + (_verdict(accuracy[f], target) if target else _pct(accuracy[f]))
        )
    lines += [
        "",
        f"routing ({routing.cases} cases; {len(routing.not_built_yet)} not built yet left out):",
        f"  route accuracy        {_verdict(routing.route_accuracy, ROUTE_ACCURACY_TARGET)}",
        f"  escalation recall     {_verdict(routing.recall, ESCALATION_RECALL_TARGET)}"
        f"  ({routing.escalations_caught}/{routing.escalations_expected})",
        f"  escalation precision  {_verdict(routing.precision, ESCALATION_PRECISION_TARGET)}"
        f"  ({routing.escalated_rightly}/{routing.escalated})",
        f"  reason codes exact    {routing.codes_exact}/{routing.escalations_caught}"
        " of the escalations caught",
        "",
        "not built yet (week 5):",
    ]
    for r in results:
        if r.outcome.case_id in NOT_BUILT_YET:
            o = r.outcome
            lines.append(f"  {o.case_id:30} expected {o.expected_route} {list(o.expected_codes)},"
                         f" got {o.route} {list(o.codes)}")  # fmt: skip
    lines += ["", "failed calls:"] + [f"  {r.row.case_id}: {r.row.error}" for r in results
                                       if r.outcome.failed] or ["  none"]  # fmt: skip
    lines += ["", "misses (expected -> got):"]
    missed = False
    for r in results:
        o, row = r.outcome, r.row
        if row.fields is not None:
            for f in FIELDS:
                if not row.fields[f]:
                    missed = True
                    lines.append(f"  {o.case_id:30} field {f}: {r.gold.get(_output_key(f))!r} -> "
                                 f"{(row.output or {}).get(_output_key(f))!r}")  # fmt: skip
        if o.case_id not in NOT_BUILT_YET and not o.matches:
            missed = True
            lines.append(f"  {o.case_id:30} outcome: {o.expected_route} "
                         f"{list(o.expected_codes)} holds {o.expected_holds} -> "
                         f"{'FAILED CALL, ' if o.failed else ''}{o.route} {list(o.codes)} "
                         f"holds {o.holds}")  # fmt: skip
    if not missed:
        lines.append("  none")
    return "\n".join(lines)


def _output_key(f: str) -> str:
    return "date_spec" if f == "stay" else f


def write(results_dir: Path, started: datetime, model: str, settings: Settings,
          prompt_version: str, commit: str, estimate: int, results: Sequence[CaseResult],
          accuracy: dict[str, float], routing: RoutingScore) -> Path:  # fmt: skip
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{started.strftime('%Y%m%dT%H%M%SZ')}-extraction-dev-{model}.json"
    data = {
        "run": {"set_name": "dev", "model": model, "settings": asdict(settings),
                "prompt_version": prompt_version, "commit": commit, "dirty": False,
                "started_at": started.isoformat(), "estimate_micro_usd": estimate,
                "cost_micro_usd": sum(r.row.cost_micro_usd or 0 for r in results)},
        "summary": {
            "field_accuracy": accuracy,
            "field_targets": {f: {"target": t, "pass": meets(accuracy.get(f), t)}
                              for f, t in FIELD_TARGETS.items()},
            "routing": asdict(routing) | {
                "route_accuracy": routing.route_accuracy, "recall": routing.recall,
                "precision": routing.precision,
                "route_accuracy_pass": meets(routing.route_accuracy, ROUTE_ACCURACY_TARGET),
                "recall_pass": meets(routing.recall, ESCALATION_RECALL_TARGET),
                "precision_pass": meets(routing.precision, ESCALATION_PRECISION_TARGET),
            },
        },
        "cases": [asdict(r.row) | {"outcome": asdict(r.outcome)
                                   | {"matches": r.outcome.matches}} for r in results],
    }  # fmt: skip
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
