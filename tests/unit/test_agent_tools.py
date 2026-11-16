"""The agent's tools as sent (D11, D12, D70): what each route gets, and what no route may
ever get."""

from typing import Any, get_args

import pytest

from anemos.agent.tools import (
    ENDING,
    ESCALATE,
    READ_ONLY,
    TOOLS,
    TOOLS_BY_ROUTE,
    WRITE_TOOLS,
    ModelEscalationCode,
)
from anemos.llm.openai_client import function_tool
from anemos.policies import load_policies
from anemos.reservations.errors import ReasonCode

pytestmark = pytest.mark.unit

ALL_SIX = ["check_availability", "quote_price", "suggest_alternatives", "search_policies",
           "write_draft", "escalate"]  # fmt: skip


def names(route: str) -> list[str]:
    return [t.name for t in TOOLS_BY_ROUTE[route]]  # type: ignore[index]


def test_the_tools_per_route_are_d11s() -> None:
    assert {r: names(r) for r in TOOLS_BY_ROUTE} == {
        "quote": ALL_SIX, "ask_info": ALL_SIX, "decline": ALL_SIX,
        "policy": ["search_policies", "write_draft", "escalate"],
        "other": ["escalate"],
    }  # fmt: skip


def test_no_route_gives_the_model_a_write_tool() -> None:
    """D12: hold_room and send_reply run only in the approval handler. Every tool any
    route sends is a read tool or one that ends the loop (it writes nothing until the
    run's final transaction, which code decides)."""
    sent = {t.name for tools in TOOLS_BY_ROUTE.values() for t in tools}
    assert sent <= READ_ONLY | ENDING
    assert not sent & WRITE_TOOLS
    assert not set(TOOLS) & WRITE_TOOLS


def test_escalate_accepts_only_the_codes_that_need_judgement() -> None:
    """D11: codes raised by code (GROUP, NO_CALENDAR, LOOP_LIMIT ...) are not the model's."""
    expected = {"COMPLAINT", "DISCOUNT", "CHANGE", "CANCELLATION", "NO_POLICY", "MODEL"}
    assert set(get_args(ModelEscalationCode)) == expected
    assert expected <= {c.value for c in ReasonCode}
    schema: Any = function_tool(TOOLS[ESCALATE])["parameters"]
    assert set(schema["properties"]["reason_code"]["enum"]) == expected


@pytest.mark.parametrize("name", ["check_availability", "quote_price", "suggest_alternatives"])
def test_the_reservation_tools_take_no_arguments(name: str) -> None:
    """D11: dates, party and room type come from the stored request, never the model."""
    schema: Any = function_tool(TOOLS[name])["parameters"]
    assert schema["properties"] == {} and schema["additionalProperties"] is False


def test_write_draft_has_no_route_or_hold_and_cites_only_real_ids() -> None:
    """D72: route and hold are code's; policy_ids is an enum of the packaged ids."""
    schema: Any = function_tool(TOOLS["write_draft"])["parameters"]
    assert set(schema["properties"]) == {"text", "offer_quote_ids", "policy_ids"}
    real = {c.policy_id for c in load_policies()}
    assert set(schema["properties"]["policy_ids"]["items"]["enum"]) == real


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_tool_converts_to_a_strict_function(name: str) -> None:
    tool = function_tool(TOOLS[name])
    assert tool["strict"] is True and tool["name"] == name
