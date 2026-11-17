"""The gold client's decisions on transcripts the gold cases do not all produce (a case
whose labels list every tool hides the agent's own next-step rules)."""

import json
from pathlib import Path
from typing import Any

import pytest

from anemos.agent.loop import first_message
from anemos.agent.tools import TOOLS_BY_ROUTE, ToolRoute
from anemos.email_cases import EmailCase, load_cases
from anemos.gold import GoldClient
from anemos.llm.adapter import Settings, ToolCall, ToolOutput, Transcript, UserText

pytestmark = pytest.mark.unit

CASES = {c.id: c for c in load_cases(Path(__file__).resolve().parents[2] / "eval" / "emails")}
SETTINGS = Settings("gpt-6-luna", 100)


def without_required_tools(case: EmailCase) -> EmailCase:
    return case.model_copy(update={"labels": case.labels.model_copy(update={"required_tools": []})})


def turn(
    case: EmailCase, route: ToolRoute, *answers: tuple[str, dict[str, Any]]
) -> tuple[str, Any]:
    message = first_message(route, "", case.labels.language, case.email.subject,
                            case.email.body.strip())  # fmt: skip
    items: list[Any] = [UserText(message)]
    for n, (name, output) in enumerate(answers):
        items += [ToolCall(f"c{n}", name, "{}"), ToolOutput(f"c{n}", json.dumps(output))]
    reply = GoldClient([case]).send_turn(Transcript("x", tuple(items)), TOOLS_BY_ROUTE[route],
                                         SETTINGS)  # fmt: skip
    return reply.call.name, json.loads(reply.call.arguments)


def test_after_fully_booked_it_asks_for_alternatives() -> None:
    case = without_required_tools(CASES["en-quote-sea-view-june"])
    name, _ = turn(case, "quote", ("quote_price", {"result": "FULLY_BOOKED"}))
    assert name == "suggest_alternatives"


def test_after_original_available_it_prices_again() -> None:
    case = without_required_tools(CASES["en-quote-sea-view-june"])
    name, _ = turn(case, "quote", ("quote_price", {"result": "FULLY_BOOKED"}),
                   ("suggest_alternatives", {"result": "ORIGINAL_AVAILABLE"}))  # fmt: skip
    assert name == "quote_price"


def test_it_offers_every_quote_of_the_latest_answer() -> None:
    case = without_required_tools(CASES["en-quote-sea-view-june"])
    options = {"result": "ALTERNATIVES", "quotes": [{"quote_id": 4}, {"quote_id": 5}]}
    name, args = turn(case, "quote", ("quote_price", {"result": "FULLY_BOOKED"}),
                      ("suggest_alternatives", options))  # fmt: skip
    assert name == "write_draft" and args["offer_quote_ids"] == [4, 5]
    assert "{{QUOTE_BLOCK}}" in args["text"] and "{{REQUEST_SUMMARY}}" in args["text"]


def test_a_closed_decline_names_the_season_by_placeholder() -> None:
    case = without_required_tools(CASES["el-christmas-2027"])
    name, args = turn(case, "decline", ("quote_price", {"result": "CLOSED"}))
    assert name == "write_draft" and "{{OPEN_SEASON}}" in args["text"]
    assert "$opens" not in args["text"]


def test_kind_other_escalates_its_labelled_code() -> None:
    name, args = turn(CASES["el-cancellation"], "other")
    assert (name, args["reason_code"]) == ("escalate", "CANCELLATION")
