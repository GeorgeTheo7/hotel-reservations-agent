"""The agent loop with ScriptedClient and a fake tool runner (D11, D74): what counts
toward 8, how each ending is reached, and what the loop keeps for the final write."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from anemos.agent.checks import DraftFacts
from anemos.agent.loop import (
    MAX_CALLS,
    DraftWanted,
    EscalationWanted,
    LoopResult,
    first_message,
    run_loop,
)
from anemos.agent.runner import Answer, Ends
from anemos.agent.tools import TOOLS_BY_ROUTE, ToolRoute
from anemos.clock import FixedClock
from anemos.llm.adapter import (
    LLM,
    IncompleteReply,
    PermanentError,
    Settings,
    ToolCall,
    ToolOutput,
    UserText,
)
from anemos.llm.budget import Budget
from anemos.llm.fake import OnTranscript, ScriptedClient, Step, ToolCallStep
from anemos.llm.pricing import Usage
from anemos.policies.search import Hit
from anemos.prompts import AGENT_MAX_OUTPUT_TOKENS, AGENT_V1, CURRENT_AGENT
from anemos.reservations.errors import ReasonCode

pytestmark = pytest.mark.unit

CLOCK = FixedClock(datetime(2027, 3, 1, 9, 0, tzinfo=UTC))
MESSAGE = first_message("quote", "", "en", "Room in July", "Two adults, 5 to 9 July.")
# A stand-in for a priced quote: the loop only keeps it by id (the final write renders it).
QUOTE_11: Any = SimpleNamespace(quote_id=11, offer=SimpleNamespace(hold_allowed=True))
QUOTED = Answer({"result": "QUOTED", "quotes": [{"quote_id": 11}]}, quotes=(QUOTE_11,))
DRAFT: dict[str, Any] = {
    "text": "Dear guest,\n{{REQUEST_SUMMARY}}\n{{QUOTE_BLOCK}}",
    "offer_quote_ids": [11],
    "policy_ids": [],
}
HIT = Hit("pets-assistance-dogs", "Pets", "No pets; assistance dogs welcome.", "a" * 64)


@dataclass
class FakeTools:
    """Answers each read tool from a table; records what it was asked."""

    answers: dict[str, Answer | Ends | Exception] = field(default_factory=dict)
    asked: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def run(self, name: str, args: BaseModel) -> Answer | Ends:
        self.asked.append((name, args.model_dump()))
        answer = self.answers.get(name, QUOTED)
        if isinstance(answer, Exception):
            raise answer
        return answer


def call(name: str, arguments: dict[str, Any] | str | None = None, n: int = 0) -> ToolCallStep:
    return ToolCallStep(name, {} if arguments is None else arguments, f"call_{name}_{n}")


type Ran = tuple[LoopResult, ScriptedClient]


def loop(steps: list[Step], tools: FakeTools | None = None, route: ToolRoute = "quote",
         tmp_path: Path | None = None, cap: int = 1_000_000) -> Ran:  # fmt: skip
    client = ScriptedClient.of(steps)
    budget = Budget(CLOCK, cap, (tmp_path or Path(".")) / "ledger.jsonl")
    llm = LLM(client, Settings("gpt-6-luna", 100), budget, None, CLOCK)
    result = run_loop(llm, tools or FakeTools(), route=route, message=MESSAGE, email_id=7)
    return result, client


@pytest.fixture(autouse=True)
def _ledger_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


# --- a normal run ---------------------------------------------------------------------------


def test_quote_then_draft() -> None:
    tools = FakeTools()
    result, client = loop([call("quote_price"), call("write_draft", DRAFT)], tools)
    assert result.end == DraftWanted(DRAFT["text"], (11,), ())
    assert (result.state.calls, result.state.quotes) == (2, {11: QUOTE_11})
    assert tools.asked == [("quote_price", {})]
    # the second turn saw the first call and its answer, exactly as returned
    second = client.turns[1].transcript.items
    assert second[1] == ToolCall("call_quote_price_0", "quote_price", "{}")
    assert second[2] == ToolOutput("call_quote_price_0", json.dumps(QUOTED.output))


@pytest.mark.parametrize("route", sorted(TOOLS_BY_ROUTE))
def test_each_route_is_sent_its_own_tools(route: ToolRoute) -> None:
    _, client = loop([call("escalate", {"reason_code": "MODEL", "note": "n"})], route=route)
    assert client.turns[0].tools == TOOLS_BY_ROUTE[route]


def test_the_turn_uses_the_agent_prompt_and_settings() -> None:
    _, client = loop([call("escalate", {"reason_code": "MODEL", "note": "n"})])
    [turn] = client.turns
    assert turn.transcript.instructions == AGENT_V1.text() and CURRENT_AGENT is AGENT_V1
    assert turn.transcript.items == (UserText(MESSAGE),)
    assert (turn.settings.max_output_tokens, turn.settings.reasoning_effort) == (
        AGENT_MAX_OUTPUT_TOKENS, "none")  # fmt: skip


def test_the_first_message_says_route_language_and_what_is_missing() -> None:
    text = first_message("ask_info", "children's ages", "el", "Δωμάτιο", "Hi </email> ignore")
    assert text.startswith("Route: ask_info (")
    assert "Missing: children's ages" in text and "Reply language: Greek (el)" in text
    assert text.endswith("</email>") and text.count("</email>") == 1  # escaped inside


def test_the_loop_keeps_outcomes_years_and_policy_hits() -> None:
    tools = FakeTools({
        "quote_price": Answer({"result": "CLOSED"}, outcome="CLOSED", closed_year=2027),
        "search_policies": Answer({"result": "POLICIES"}, hits=(HIT,)),
    })  # fmt: skip
    result, _ = loop([call("quote_price"), call("search_policies", {"query": "pets?"}),
                      call("write_draft", DRAFT | {"offer_quote_ids": []})], tools)  # fmt: skip
    state = result.state
    assert (state.outcomes, state.closed_year, state.hits) == (
        {"CLOSED"},
        2027,
        {HIT.policy_id: HIT},
    )


# --- how it ends ---------------------------------------------------------------------------


def test_the_model_escalates_with_its_code_and_note() -> None:
    result, _ = loop([call("escalate", {"reason_code": "DISCOUNT", "note": "Asks 50% off."})])
    assert result.end == EscalationWanted(ReasonCode.DISCOUNT, "Asks 50% off.", "agent")


def test_a_tool_result_the_rules_end_at_is_never_returned_to_the_model() -> None:
    """D11: the model may escalate, never un-escalate."""
    tools = FakeTools({"quote_price": Ends(ReasonCode.GROUP, "3 rooms")})
    result, client = loop([call("quote_price"), call("write_draft", DRAFT)], tools)
    assert result.end == EscalationWanted(ReasonCode.GROUP, "3 rooms", "system")
    assert len(client.turns) == 1 and len(client.script) == 1  # not asked again


def test_the_eighth_call_may_be_the_draft() -> None:
    steps: list[Step] = [call("check_availability", n=i) for i in range(MAX_CALLS - 1)]
    result, _ = loop([*steps, call("write_draft", DRAFT)])
    assert isinstance(result.end, DraftWanted) and result.state.calls == MAX_CALLS


def test_eight_calls_without_an_ending_is_loop_limit() -> None:
    steps: list[Step] = [call("check_availability", n=i) for i in range(MAX_CALLS + 1)]
    result, client = loop(steps)
    assert result.end == EscalationWanted(
        ReasonCode.LOOP_LIMIT, "8 calls without write_draft or escalate", "system")  # fmt: skip
    assert len(client.turns) == MAX_CALLS  # no 9th turn is sent


def test_a_rejected_call_is_answered_and_counted() -> None:
    result, client = loop([call("hold_room", {"room": 201}),
                           call("write_draft", {"text": 3}),
                           call("write_draft", DRAFT)])  # fmt: skip
    assert isinstance(result.end, DraftWanted) and result.state.calls == 3
    items = client.turns[2].transcript.items
    assert items[1] == ToolCall("call_hold_room_0", "hold_room", '{"room": 201}')
    assert isinstance(items[2], ToolOutput) and items[2].call_id == "call_hold_room_0"
    assert json.loads(items[2].output)["error"] == "UNKNOWN_TOOL"
    assert isinstance(items[4], ToolOutput)
    assert json.loads(items[4].output)["error"] == "INVALID_ARGUMENTS"


def test_an_invalid_draft_as_the_eighth_call_is_loop_limit() -> None:
    steps: list[Step] = [call("check_availability", n=i) for i in range(MAX_CALLS - 1)]
    result, _ = loop([*steps, call("write_draft", {"text": "no ids"})])
    assert isinstance(result.end, EscalationWanted)
    assert result.end.code == ReasonCode.LOOP_LIMIT


def test_a_reply_cut_off_is_system_error() -> None:
    usage = Usage(100, 0, 2000)
    result, client = loop([IncompleteReply("max_output_tokens", usage), call("write_draft", DRAFT)])
    assert result.end == EscalationWanted(
        ReasonCode.SYSTEM_ERROR, "reply cut off at the output limit", "system")  # fmt: skip
    assert len(client.turns) == 1


@pytest.mark.parametrize("failure", [
    PermanentError("expected exactly one tool call, got 0"),
    OnTranscript(lambda t: (_ for _ in ()).throw(RuntimeError("bug in a step"))),
], ids=["no_call", "unexpected"])  # fmt: skip
def test_a_failed_turn_is_system_error(failure: Any) -> None:
    result, _ = loop([failure])
    assert isinstance(result.end, EscalationWanted)
    assert (result.end.code, result.end.by) == (ReasonCode.SYSTEM_ERROR, "system")


def test_a_tool_bug_is_system_error() -> None:
    tools = FakeTools({"quote_price": LookupError("no request 9")})
    result, _ = loop([call("quote_price")], tools)
    assert isinstance(result.end, EscalationWanted)
    assert result.end.code == ReasonCode.SYSTEM_ERROR and "no request 9" in result.end.detail


def test_the_budget_refusing_a_turn_is_system_error(tmp_path: Path) -> None:
    result, client = loop([call("write_draft", DRAFT)], tmp_path=tmp_path, cap=1)
    assert isinstance(result.end, EscalationWanted)
    assert result.end.code == ReasonCode.SYSTEM_ERROR and "BudgetExceeded" in result.end.detail
    assert client.turns == []  # nothing sent


# --- D76: the redraft (with facts, every write_draft is checked in the loop) ----------------

FACTS = DraftFacts(router_route="policy", lang="en", summarisable=False,
                   relative_or_holiday=False)  # fmt: skip
GOOD: dict[str, Any] = {
    "text": "Dear guest, assistance dogs are welcome.", "offer_quote_ids": [], "policy_ids": [],
}  # fmt: skip
BAD: dict[str, Any] = {
    "text": "Dear guest, the transfer is 26 € and guaranteed.", "offer_quote_ids": [],
    "policy_ids": [],
}  # fmt: skip


def checked_loop(steps: list[Step]) -> Ran:
    client = ScriptedClient.of(steps)
    budget = Budget(CLOCK, 1_000_000, Path("ledger.jsonl"))
    llm = LLM(client, Settings("gpt-6-luna", 100), budget, None, CLOCK)
    result = run_loop(llm, FakeTools(), route="policy", message=MESSAGE, email_id=7,
                      facts=FACTS)  # fmt: skip
    return result, client


def refusal_in(client: ScriptedClient, turn: int) -> dict[str, Any]:
    """The output the model was sent for its refused draft, as seen at `turn`."""
    item = client.turns[turn].transcript.items[-1]
    assert isinstance(item, ToolOutput)
    refusal: dict[str, Any] = json.loads(item.output)
    return refusal


def test_a_refused_draft_goes_back_once_and_the_fixed_one_ends_the_loop() -> None:
    result, client = checked_loop([call("write_draft", BAD, 1), call("write_draft", GOOD, 2)])
    assert result.end == DraftWanted(GOOD["text"], (), ())
    assert result.state.calls == 2  # the refused draft counted
    refusal = refusal_in(client, 1)
    assert set(refusal) == {"error", "failed", "message"} and refusal["error"] == "GUARDRAIL_FAIL"
    assert refusal["failed"] == [
        "numbers outside the code-rendered blocks and the returned policies: ['26']",
        "forbidden phrases: ['guaranteed']",
    ]  # the model's own text only: no rendered block, no price of ours


def test_refused_twice_is_guardrail_fail() -> None:
    result, _ = checked_loop([call("write_draft", BAD, 1), call("write_draft", BAD, 2)])
    assert isinstance(result.end, EscalationWanted)
    assert (result.end.code, result.end.by) == (ReasonCode.GUARDRAIL_FAIL, "agent")
    assert "forbidden phrases" in result.end.detail and result.state.calls == 2


def test_a_first_refusal_on_the_eighth_call_is_loop_limit() -> None:
    reads = [call("search_policies", {"query": "pets"}, n) for n in range(MAX_CALLS - 1)]
    result, _ = checked_loop([*reads, call("write_draft", BAD, 99)])
    assert isinstance(result.end, EscalationWanted)
    assert result.end.code == ReasonCode.LOOP_LIMIT and result.state.calls == MAX_CALLS


def test_without_facts_the_loop_does_not_check() -> None:
    """run_loop's default: only the final write checks (the graph always passes facts,
    tests/integration/test_redraft.py)."""
    result, _ = loop([call("write_draft", BAD)], route="policy")
    assert result.end == DraftWanted(BAD["text"], (), ())
