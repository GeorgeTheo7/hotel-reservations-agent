"""{{REQUEST_SUMMARY}} on an incomplete request (D80): an ask_info draft shows the
known part, with the resolved dates in full, through the real agent path (gold-style
extraction, a scripted write_draft, the real final write)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from anemos.clock import FixedClock
from anemos.email_cases import EmailCase, load_cases
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import process_email
from anemos.inbox import store_email
from anemos.llm.adapter import LLM, Settings
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, ScriptedClient, ToolCallStep
from anemos.run import agent_finish

if TYPE_CHECKING:
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

CASES = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=100)
TEXT = {
    "en": "Dear guest,\n\n{{REQUEST_SUMMARY}}\n\nHow many guests will stay?\n\nHotel Anemos",
    "el": "Αγαπητέ επισκέπτη,\n\n{{REQUEST_SUMMARY}}\n\nΠόσα άτομα θα μείνουν;\n\nHotel Anemos",
}
NEXT_WEEKEND = {  # received Wednesday 9 June 2027: next weekend is Friday 11 - Sunday 13
    "en": "Friday 11 June 2027 – Sunday 13 June 2027 (2 nights)",
    "el": "Παρασκευή 11 Ιουνίου 2027 – Κυριακή 13 Ιουνίου 2027 (2 νύχτες)",
}
PARTY_WORDS = {"en": "adult", "el": "ενήλικ"}
GREEK_BODY = "Καλησπέρα,\n\nέχετε ένα δίκλινο standard για το επόμενο Σαββατοκύριακο;\n\nΕλένη"


def ask_info_draft(committed_db: CommittedDb, case: EmailCase, extraction: ExtractionOutput,
                   lang: str, tmp: Path) -> tuple[str, str, str]:  # fmt: skip
    """Run the email with this extraction and a write_draft using the placeholder;
    returns (email status, draft route, draft body)."""
    clock = FixedClock(case.email.received_at)
    conn = committed_db.connect()
    email_id = store_email(conn, clock, case.incoming()).email_id
    conn.commit()
    write = ToolCallStep("write_draft", {"text": TEXT[lang], "offer_quote_ids": [],
                                         "policy_ids": []})  # fmt: skip
    client = ScriptedClient.of([Answer(extraction), write])
    budget = Budget(clock, 1_000_000, tmp / "llm_spend.jsonl")
    llm = LLM(client, SETTINGS, budget, committed_db.connect, clock, sleep=lambda s: None)
    process_email(committed_db.connect, clock, llm, email_id, finish=agent_finish(llm))
    row: Any = conn.execute(
        "SELECT e.status, d.route, v.body FROM emails e JOIN drafts d ON d.email_id = e.id"
        " JOIN draft_versions v ON v.draft_id = d.id WHERE e.id = %s",
        (email_id,),
    ).fetchone()
    conn.commit()
    assert row is not None, "no draft"
    return row[0], row[1], row[2]


@pytest.mark.parametrize("lang", ["en", "el"])
def test_next_weekend_without_the_party_shows_the_resolved_dates(
    committed_db: CommittedDb, tmp_path: Path, lang: str
) -> None:
    case = CASES["en-next-weekend-june"]
    gold = case.gold_output()
    assert gold is not None
    changes: dict[str, Any] = {"adults": None, "children": None}  # the party is missing
    if lang == "el":
        greek_email = case.email.model_copy(update={"body": GREEK_BODY})
        case = case.model_copy(update={"email": greek_email})
        changes |= {"language": "el", "script": "greek"}
    extraction = ExtractionOutput.model_validate(gold.model_dump() | changes)
    status, route, body = ask_info_draft(committed_db, case, extraction, lang, tmp_path)
    assert (status, route) == ("awaiting_approval", "ask_info")
    assert "{{REQUEST_SUMMARY}}" not in body
    assert NEXT_WEEKEND[lang] in body
    assert PARTY_WORDS[lang] not in body.lower()


def test_el_adults_null_we_shows_its_dates_without_a_party(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    """The dev agent eval's finding: the model wrote these dates itself because the
    placeholder could not render (NOTES 2026-11-17); now code renders them."""
    case = CASES["el-adults-null-we"]
    gold = case.gold_output()
    assert gold is not None and gold.adults is None
    status, route, body = ask_info_draft(committed_db, case, gold, "el", tmp_path)
    assert (status, route) == ("awaiting_approval", "ask_info")
    assert "Σάββατο 12 Ιουνίου 2027 – Τρίτη 15 Ιουνίου 2027 (3 νύχτες)" in body
    assert "ενήλικ" not in body.lower()
