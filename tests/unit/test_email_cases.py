"""Test-email files (schema version 1, docs/email-format.md): every file in
eval/emails/ loads, and each rule of the schema refuses a file that breaks it.

Bad examples start from one valid case and change one thing, so a refusal can only
come from the rule under test.
"""

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from anemos.email_cases import (
    CaseError,
    EmailCase,
    check_unique,
    demo_cases,
    load_cases,
    parse_case,
)
from anemos.extraction import ExtractionFlag, Script
from anemos.reservations.datespec import ExactDates, PartialDate

pytestmark = pytest.mark.unit

EVAL_EMAILS = Path(__file__).resolve().parents[2] / "eval" / "emails"

VALID: dict[str, Any] = {
    "schema_version": 2,
    "id": "en-valid-case",
    "split": "dev",
    "tags": ["tetris"],
    "email": {
        "from": "Test Guest <Guest@Example.test>",
        "subject": "Rooms in July",
        "received_at": "2026-12-01T10:00:00+00:00",
        "body": "Two adults, 10 to 14 July 2027. Also: is breakfast included?",
    },
    "world": {
        "bookings": [
            {
                "room": 101,
                "check_in": "2027-07-10",
                "check_out": "2027-07-14",
                "status": "confirmed",
            },
            {
                "room": 201,
                "check_in": "2027-07-10",
                "check_out": "2027-07-12",
                "status": "hold",
                "expires_at": "2026-12-02T10:00:00+00:00",
            },
        ]
    },
    "labels": {
        "language": "en",
        "script": "latin",
        "kind": "request",
        "extraction": {
            "date_spec": {
                "kind": "exact",
                "check_in": {"day": 10, "month": 7, "year": 2027, "ambiguous_order": False},
                "check_out": {"day": 14, "month": 7, "year": 2027},
            },
            "adults": 2,
            "children": 0,
            "child_ages": [],
            "rooms": 1,
            "room_type": None,
            "flags": ["accessibility_need"],
            "arrival_time": "21:30",
        },
        "route": "quote",
        "reason_codes": [],
        "required_tools": ["check_availability", "quote_price", "search_policies"],
        "policy_ids": ["breakfast"],  # a quote that also answers a policy question
        "holds": 0,
        "forbidden_phrases": [],
    },
}


def parse(data: dict[str, Any]) -> EmailCase:
    return parse_case(yaml.safe_dump(data, allow_unicode=True), name=data["id"])


def changed(edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    data = copy.deepcopy(VALID)
    edit(data)
    return data


def labels(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = data["labels"]
    return result


# --- the files ------------------------------------------------------------------------


def test_every_eval_file_loads() -> None:
    cases = load_cases(EVAL_EMAILS)
    assert len(cases) >= 3
    assert {case.split for case in cases} <= {"dev", "held_out"}  # demo cases live in src/


def test_every_demo_case_loads_and_can_be_drafted() -> None:
    """The demo set ships in the package: split demo, no world (the CLI applies none),
    and every drafted case's gold dates resolve to one stay for the requests row. Two
    escalate (owner's UI, week 7: reason codes and a summary to show)."""
    cases = demo_cases()
    assert len(cases) == 8
    assert sorted(c.id for c in cases if c.labels.route == "escalate") == [
        "demo-complaint-noise", "demo-group-christening-el",
    ]  # fmt: skip
    for case in cases:
        assert (case.split, case.world.bookings) == ("demo", []), case.id
        if case.labels.route not in ("set_aside", "escalate"):  # answered from these labels
            gold = case.gold_output()
            assert gold is not None and gold.adults is not None, case.id


def test_eval_and_demo_ids_do_not_overlap() -> None:
    check_unique([*load_cases(EVAL_EMAILS), *demo_cases()])


def test_the_example_in_email_format_md_is_a_valid_case() -> None:
    """The documented schema is the real one: its first YAML block loads."""
    doc = (EVAL_EMAILS.parents[1] / "docs" / "email-format.md").read_text(encoding="utf-8")
    example = doc.split("```yaml\n", 1)[1].split("```", 1)[0]
    case = parse_case(example, name="en-quote-sea-view-june")
    assert [b.status for b in case.world.bookings] == ["confirmed", "hold"]


def test_a_valid_case_loads_with_world_flags_and_a_policy_question() -> None:
    case = parse(VALID)
    assert case.labels.extraction is not None
    assert case.labels.extraction.flags == [ExtractionFlag.ACCESSIBILITY_NEED]
    assert case.labels.extraction.date_spec_value() == ExactDates(
        PartialDate(10, 7, 2027), PartialDate(14, 7, 2027)
    )
    assert [b.room for b in case.world.bookings] == [101, 201]
    assert (case.labels.route, case.labels.policy_ids) == ("quote", ["breakfast"])


def test_world_and_optional_labels_default_to_empty() -> None:
    def strip(data: dict[str, Any]) -> None:
        del data["world"]
        for key in ("reason_codes", "required_tools", "policy_ids", "holds", "forbidden_phrases"):
            del data["labels"][key]
        del data["labels"]["extraction"]["flags"]

    case = parse(changed(strip))
    assert case.world.bookings == []
    assert case.labels.extraction is not None
    assert case.labels.extraction.flags == []
    assert (case.labels.holds, case.labels.required_tools) == (0, [])


@pytest.mark.parametrize("kind", ["request", "question", "other", "spam"])
def test_every_kind_is_accepted(kind: str) -> None:
    def set_kind(data: dict[str, Any]) -> None:
        labels(data)["kind"] = kind

    assert parse(changed(set_kind)).labels.kind == kind


def test_a_thread_reply_has_no_kind() -> None:
    """Thread replies skip extraction (no model call), so their kind is null."""

    def reply(data: dict[str, Any]) -> None:
        data["email"]["in_reply_to"] = "<outbox-1@anemos.invalid>"
        labels(data)["kind"] = None

    case = parse(changed(reply))
    assert (case.labels.kind, case.email.in_reply_to) == (None, "<outbox-1@anemos.invalid>")


def test_spam_has_a_kind_but_no_extraction() -> None:
    def spam(data: dict[str, Any]) -> None:
        labels(data).update(kind="spam", extraction=None, route="set_aside", required_tools=[],
                            policy_ids=[])  # fmt: skip

    case = parse(changed(spam))
    assert (case.labels.kind, case.labels.extraction) == ("spam", None)


def test_a_version_1_file_is_refused_with_a_hint() -> None:
    old = changed(_set("schema_version", 1))
    with pytest.raises(CaseError, match="schema version 1 is no longer read; migrate"):
        parse_case(yaml.safe_dump(old), name=old["id"])


def test_greeklish_is_el_in_latin_script() -> None:
    def greeklish(data: dict[str, Any]) -> None:
        labels(data).update(language="el", script="latin")

    assert parse(changed(greeklish)).labels.script is Script.LATIN


def test_incoming_is_the_normalised_email() -> None:
    email = parse(VALID).incoming()
    assert email.message_id == "<en-valid-case@cases.example.test>"
    assert email.from_address == "guest@example.test"
    assert email.received_at == datetime(2026, 12, 1, 10, 0, tzinfo=UTC)


# --- bad examples: one rule each ------------------------------------------------------


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """Edit for `a.b.c = value` (a list index is written as a number)."""

    def edit(data: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        target: Any = data
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[last] = value

    return edit


BAD: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "unknown-key": (_set("labels.reason_code", ["DISCOUNT"]), "reason_code"),
    "schema-version-3": (_set("schema_version", 3), "schema_version"),
    "unknown-kind": (_set("labels.kind", "booking"), "kind"),
    "kind-null-without-reply": (_set("labels.kind", None), "kind is null exactly when"),
    "reply-with-a-kind": (
        _set("email.in_reply_to", "<old@example.test>"),
        "kind is null exactly when",
    ),
    "kind-key-missing": (lambda data: labels(data).pop("kind"), r"labels\.kind\s+Field required"),
    "arrival-24-00": (_set("labels.extraction.arrival_time", "24:00"), "arrival_time"),
    "arrival-without-leading-zero": (
        _set("labels.extraction.arrival_time", "7:30"),
        "arrival_time",
    ),
    "real-looking-sender": (_set("email.from", "Guest <guest@gmail.com>"), "fictional data"),
    "naive-received-at": (_set("email.received_at", "2026-12-01T10:00:00"), "received_at"),
    "escalate-without-codes": (_set("labels.route", "escalate"), "reason_codes are given"),
    "codes-on-a-quote": (_set("labels.reason_codes", ["DISCOUNT"]), "reason_codes are given"),
    "unknown-code": (_set("labels.reason_codes", ["RUDENESS"]), "reason_codes"),
    "day-32": (_set("labels.extraction.date_spec.check_in.day", 32), "day must be 1-31"),
    "unknown-flag": (_set("labels.extraction.flags", ["vip_guest"]), "flags"),
    "greek-script-in-english": (_set("labels.script", "greek"), "script greek needs language el"),
    "greek-script-in-german": (
        lambda data: labels(data).update(language="de", script="greek"),
        "script greek needs language el",
    ),
    "world-check-out-not-after": (
        _set("world.bookings.0.check_out", "2027-07-10"),
        "check_out must be after check_in",
    ),
    "world-unknown-room": (_set("world.bookings.0.room", 999), "not a seeded room"),
    "unknown-room-type": (_set("labels.extraction.room_type", "penthouse"), "room_type"),
    "hold-tool-required": (_set("labels.required_tools", ["hold_room"]), "required_tools"),
    "more-ages-than-children": (_set("labels.extraction.child_ages", [8]), "more child_ages"),
    "bad-id": (_set("id", "En Valid"), "id"),
}


@pytest.mark.parametrize("name", list(BAD))
def test_a_file_breaking_a_rule_is_refused(name: str) -> None:
    edit, message = BAD[name]
    data = changed(edit)
    with pytest.raises(CaseError, match=message):
        parse_case(yaml.safe_dump(data, allow_unicode=True), name=data["id"])


def test_language_no_is_a_yaml_trap_and_refused() -> None:
    """Unquoted `no` is the boolean false in YAML 1.1; a strict string refuses it, so
    a Norwegian case must write "no" in quotes."""
    text = yaml.safe_dump(VALID).replace("language: en", "language: no")
    with pytest.raises(CaseError, match="language"):
        parse_case(text, name="en-valid-case")


def test_id_must_match_the_file_name() -> None:
    with pytest.raises(CaseError, match="does not match the file name"):
        parse_case(yaml.safe_dump(VALID), name="another-name")


def test_broken_yaml_names_the_file() -> None:
    with pytest.raises(CaseError, match=r"^en-broken: "):
        parse_case("labels: [unclosed", name="en-broken")


def test_an_id_used_twice_is_refused() -> None:
    with pytest.raises(CaseError, match="id used twice"):
        check_unique([parse(VALID), parse(VALID)])
