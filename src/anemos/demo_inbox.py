"""Fill a local inbox for trying the API by hand: `uv run --env-file .env python -m
anemos.demo_inbox [--at TIME]`. The emails are the demo cases in anemos/demo_emails/
(the test-email format, docs/email-format.md); all fictional.

Each email is stored by the real `store_email` and drafted by the real stub drafter.
Two steps are stand-ins until week 4, done here with plain SQL and nowhere else in
src/, driven by the case's gold labels: the extraction (the request row, with the stay
from the real `datespec.resolve`, and the language) and the router (`set_aside`).
Running it again stores nothing twice (D4): an email already stored is skipped.

The stays are in the 2027 season (seed data, D21), so run it before they pass.
"""

import argparse
from collections.abc import Callable
from datetime import datetime

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from anemos.clock import Clock, FixedClock, SystemClock
from anemos.db.connection import connect as db_connect
from anemos.email_cases import EmailCase, demo_cases
from anemos.inbox import store_email
from anemos.reservations.datespec import resolve
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.model import Stay
from anemos.states import EmailStatus
from anemos.stub_drafter import Drafted, draft_reply

type Conn = psycopg.Connection[TupleRow]


def fill(connect: Callable[[], Conn], clock: Clock) -> list[str]:
    """Store and draft every demo case, one committed transaction each. Returns one
    report line per case."""
    report = []
    with connect() as conn:
        hotel = load_hotel_data(conn)
        conn.commit()
        for case in demo_cases():
            with conn.transaction():
                report.append(_one(conn, clock, hotel, case))
    return report


def request_columns(case: EmailCase) -> dict[str, object]:
    """Stand-in for extraction (week 4): the requests row from the gold labels. The
    stay comes from the real resolver at the email's received_at."""
    extraction = case.labels.extraction
    assert extraction is not None, f"{case.id}: a drafted demo case needs an extraction"
    spec = extraction.date_spec_value()
    assert spec is not None, f"{case.id}: a drafted demo case needs dates"
    stay = resolve(spec, case.email.received_at)
    assert isinstance(stay, Stay), f"{case.id}: demo dates must resolve to one stay: {stay}"
    return {
        "extracted": Jsonb(extraction.model_dump(mode="json")),
        "stay": Range(stay.check_in, stay.check_out, "[)"),
        "adults": extraction.adults,
        "children": extraction.children,
        "child_ages": extraction.child_ages,
        "rooms": extraction.rooms,
        "room_type_id": extraction.room_type,
    }


def _one(conn: Conn, clock: Clock, hotel: HotelData, case: EmailCase) -> str:
    stored = store_email(conn, clock, case.incoming())
    if not stored.stored:
        return f"{case.id}: already stored (email {stored.email_id}), skipped"
    if case.labels.route == "set_aside":  # stand-in for the router (week 4)
        conn.execute(
            "UPDATE emails SET status = %s WHERE id = %s", (EmailStatus.SET_ASIDE, stored.email_id)
        )
        return f"{case.id}: email {stored.email_id} set aside"
    conn.execute(
        "UPDATE emails SET language = %s WHERE id = %s", (case.labels.language, stored.email_id)
    )
    columns = {"email_id": stored.email_id, "created_at": clock.now()} | request_columns(case)
    insert = sql.SQL("INSERT INTO requests ({}) VALUES ({})").format(
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(map(sql.Placeholder, columns)),
    )
    conn.execute(insert, columns)
    result = draft_reply(conn, hotel, clock, stored.email_id)
    if isinstance(result, Drafted):
        return f"{case.id}: email {stored.email_id}, draft {result.draft_id} ({result.route})"
    return f"{case.id}: email {stored.email_id} escalated ({result.reason})"


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
