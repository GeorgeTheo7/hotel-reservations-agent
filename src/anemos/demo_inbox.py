"""Fill a local inbox for trying the API by hand: `uv run --env-file .env python -m
anemos.demo_inbox [--at TIME] [--model gpt-6-luna]`. The emails are the demo cases in
anemos/demo_emails/ (the test-email format, docs/email-format.md); all fictional.

Each email goes through the real run (anemos.run): stored by `store_email`, claimed,
extracted, routed in code and drafted, set aside or escalated. Only the model's answer
is a stand-in by default: a scripted client answers each email with its case's gold
labels (no key, no network, no cost; the audit names it `gold-labels`, with a throwaway
spend ledger). `--model gpt-6-luna` sends the emails to the real model instead, under
the spend cap. Running it again stores and runs nothing twice (D4).

The stays are in the 2027 season (seed data, D21), so run it before they pass.
"""

import argparse
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import TupleRow

from anemos.clock import Clock, FixedClock, SystemClock
from anemos.db.connection import connect as db_connect
from anemos.email_cases import EmailCase, demo_cases
from anemos.extraction_run import process_email
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, ModelClient, Prompt, Settings
from anemos.llm.budget import Budget, cap_from_env
from anemos.llm.fake import Answer, ScriptedClient
from anemos.llm.openai_client import OpenAIClient
from anemos.llm.pricing import PRICES, Usage
from anemos.prompts import CURRENT_EXTRACTION
from anemos.run import route_and_draft

type Conn = psycopg.Connection[TupleRow]

GOLD = "gold"
GOLD_MODEL = "gpt-6-luna"  # priced, for the adapter's reservation; the usage is zero
GOLD_RESPONSE_MODEL = "gold-labels"  # what the audit rows name as the answering model


def gold_client(cases: Sequence[EmailCase], calls: int) -> ScriptedClient:
    """Answers each demo email with its case's gold extraction, found by its body."""
    answers = {c.email.body.strip(): c.gold_output() for c in cases}

    def answer(prompt: Prompt) -> Answer:
        for body, out in answers.items():
            if body in prompt.input and out is not None:
                return Answer(out, usage=Usage(0, 0, 0), response_model=GOLD_RESPONSE_MODEL)
        raise LookupError("this email is not a demo case")

    return ScriptedClient.of([answer] * calls)


def fill(connect: Callable[[], Conn], clock: Clock, model: str = GOLD) -> list[str]:
    """Store every demo case (one committed transaction each), then run each email
    stored now, oldest first. Returns one report line per case."""
    cases = demo_cases()
    stored: dict[str, int | None] = {}
    with connect() as conn:
        for case in cases:
            with conn.transaction():
                result = store_email(conn, clock, case.incoming())
            stored[case.id] = result.email_id if result.stored else None
        conn.commit()
    to_run = sorted(
        (c for c in cases if stored[c.id] is not None),
        key=lambda c: (c.email.received_at, stored[c.id]),
    )
    with tempfile.TemporaryDirectory() as scratch:
        llm = _llm(connect, clock, model, len(to_run), Path(scratch))
        for case in to_run:
            email_id = stored[case.id]
            assert email_id is not None
            process_email(connect, clock, llm, email_id, finish=route_and_draft)
    with connect() as conn:
        lines = [_line(conn, case, stored[case.id]) for case in cases]
        conn.commit()
    return lines


def _llm(connect: Callable[[], Conn], clock: Clock, model: str, calls: int, scratch: Path) -> LLM:
    spec = CURRENT_EXTRACTION
    client: ModelClient
    if model == GOLD:
        client, budget = gold_client(demo_cases(), calls), Budget(clock, 10**6, scratch / "spend")
        model = GOLD_MODEL
    else:
        client, budget = OpenAIClient(), Budget(clock, cap_from_env())
    settings = Settings(model, spec.max_output_tokens, spec.reasoning_effort)
    return LLM(client, settings, budget, connect, clock)


def _line(conn: Conn, case: EmailCase, email_id: int | None) -> str:
    if email_id is None:
        return f"{case.id}: already stored, skipped"
    status, codes, draft_id, route = conn.execute(
        "SELECT e.status, e.reason_codes, d.id, d.route FROM emails e"
        " LEFT JOIN drafts d ON d.email_id = e.id WHERE e.id = %s",
        (email_id,),
    ).fetchone() or (None, None, None, None)
    if draft_id is not None:
        return f"{case.id}: email {email_id}, draft {draft_id} ({route})"
    if status == "escalated":
        return f"{case.id}: email {email_id} escalated ({', '.join(codes)})"
    return f"{case.id}: email {email_id} {status}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m anemos.demo_inbox", description=__doc__)
    parser.add_argument(
        "--at", type=datetime.fromisoformat, help="clock time, e.g. 2027-03-01T09:00+00:00"
    )
    parser.add_argument("--model", default=GOLD, choices=[GOLD, *sorted(PRICES)])
    args = parser.parse_args(argv)
    clock: Clock = SystemClock() if args.at is None else FixedClock(args.at)
    for line in fill(lambda: db_connect(), clock, args.model):
        print(line)


if __name__ == "__main__":
    main()
