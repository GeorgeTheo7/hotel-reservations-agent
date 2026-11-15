"""Fill a local inbox for trying the API by hand: `uv run --env-file .env python -m
anemos.demo_inbox`. All emails and addresses are fictional (example.test).

Each email is stored by the real `store_email` and drafted by the real stub drafter.
Two steps are stand-ins until week 4, done here with plain SQL and nowhere else in
src/: the extracted request and language (extraction), and `set_aside` for the
spam-like email (the router). Running it again stores nothing twice (D4): an email
already stored is skipped.

The stays are in the 2027 season (seed data, D21), so run it before they pass.
"""

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from anemos.clock import Clock, FixedClock, SystemClock
from anemos.db.connection import connect as db_connect
from anemos.inbox import store_email
from anemos.mailbox import incoming
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.loader import load_hotel_data
from anemos.states import EmailStatus
from anemos.stub_drafter import Drafted, draft_reply

type Conn = psycopg.Connection[TupleRow]


@dataclass(frozen=True)
class Sample:
    key: str  # also the Message-ID's local part
    sender: str
    subject: str
    body: str
    language: str
    request: dict[str, Any] | None  # None: no request, the email is set aside


def samples() -> list[Sample]:
    """The fictional emails, from demo_inbox.json (guest wording stays out of Python)."""
    raw = files("anemos").joinpath("demo_inbox.json").read_text(encoding="utf-8")
    return [Sample(**item) for item in json.loads(raw)]


def fill(connect: Callable[[], Conn], clock: Clock) -> list[str]:
    """Store and draft every sample, one committed transaction each. Returns one
    report line per sample."""
    report = []
    with connect() as conn:
        hotel = load_hotel_data(conn)
        conn.commit()
        emails = samples()
        for i, sample in enumerate(emails):
            with conn.transaction():
                report.append(_one(conn, clock, hotel, sample, minutes_ago=len(emails) - i))
    return report


def _one(conn: Conn, clock: Clock, hotel: HotelData, sample: Sample, *, minutes_ago: int) -> str:
    now = clock.now()
    email = incoming(
        message_id=f"<{sample.key}@demo.example.test>", from_header=sample.sender,
        subject=sample.subject, body=sample.body,
        received_at=(now - timedelta(minutes=minutes_ago)).astimezone(UTC),
    )  # fmt: skip
    stored = store_email(conn, clock, email)
    if not stored.stored:
        return f"{sample.key}: already stored (email {stored.email_id}), skipped"
    if sample.request is None:  # stand-in for the router (week 4)
        conn.execute(
            "UPDATE emails SET status = %s WHERE id = %s", (EmailStatus.SET_ASIDE, stored.email_id)
        )
        return f"{sample.key}: email {stored.email_id} set aside"
    # Stand-in for extraction (week 4): the request and language a model would extract.
    conn.execute(
        "UPDATE emails SET language = %s WHERE id = %s", (sample.language, stored.email_id)
    )
    columns = {"email_id": stored.email_id, "extracted": Jsonb({"demo": sample.key}),
               "created_at": now} | sample.request  # fmt: skip
    insert = sql.SQL("INSERT INTO requests ({}) VALUES ({})").format(
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(map(sql.Placeholder, columns)),
    )
    conn.execute(insert, columns)
    result = draft_reply(conn, hotel, clock, stored.email_id)
    if isinstance(result, Drafted):
        return f"{sample.key}: email {stored.email_id}, draft {result.draft_id} ({result.route})"
    return f"{sample.key}: email {stored.email_id} escalated ({result.reason})"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m anemos.demo_inbox", description=__doc__)
    parser.add_argument(
        "--at", type=datetime.fromisoformat, help="clock time, e.g. 2027-03-01T09:00+00:00"
    )
    args = parser.parse_args(argv)
    clock: Clock = SystemClock() if args.at is None else FixedClock(args.at)
    for line in fill(lambda: db_connect(), clock):
        print(line)


if __name__ == "__main__":
    main()
