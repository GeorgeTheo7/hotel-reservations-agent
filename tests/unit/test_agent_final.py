"""The final write's pure parts (D72, D74): route and hold by code, guardrail 6, the
placeholder check."""

from types import SimpleNamespace
from typing import Any, get_args

import pytest

from anemos.agent.final import (
    draft_route,
    placeholders_valid,
    policies_returned,
    proposed_offers,
)
from anemos.agent.tools import ToolRoute
from anemos.guardrails import ProposedOffer
from anemos.routing import AgentRoute

pytestmark = pytest.mark.unit


def quote(hold_allowed: bool) -> Any:
    return SimpleNamespace(offer=SimpleNamespace(hold_allowed=hold_allowed))


@pytest.mark.parametrize(("offers", "outcomes", "router", "expected"), [
    ([3], {"CLOSED", "NEEDS_INFO"}, "ask_info", "quote"),  # any offer -> quote
    ([], {"CLOSED"}, "quote", "decline"),
    ([], {"NONE_FOUND", "NEEDS_INFO"}, "quote", "decline"),  # decline before ask_info
    ([], {"NEEDS_INFO"}, "quote", "ask_info"),
    ([], set(), "policy", "policy"),  # else the router's route
    ([], set(), "ask_info", "ask_info"),
])  # fmt: skip
def test_the_route_is_d72s(offers: list[int], outcomes: set[str], router: str,
                           expected: str) -> None:  # fmt: skip
    assert draft_route(offers, outcomes, router) == expected


def test_one_exact_offer_is_held() -> None:
    assert proposed_offers([3], {3: quote(True)}) == [ProposedOffer(3, hold=True)]


@pytest.mark.parametrize(("ids", "quotes"), [
    ([3, 4], {3: quote(True), 4: quote(True)}),  # options are never held (D9)
    ([3], {3: quote(False)}),  # differs from the request
    ([9], {3: quote(True)}),  # not priced in this run: guardrail 7 refuses it
], ids=["two_offers", "substitute", "unknown"])  # fmt: skip
def test_no_other_offer_is_held(ids: list[int], quotes: dict[int, Any]) -> None:
    assert all(not o.hold for o in proposed_offers(ids, quotes))


def test_a_repeated_quote_id_is_one_offer() -> None:
    assert proposed_offers([3, 3], {3: quote(True)}) == [ProposedOffer(3, hold=True)]


def test_guardrail_6_only_cited_ids_that_were_returned() -> None:
    assert policies_returned(["pets-assistance-dogs"], {"pets-assistance-dogs", "wifi"}) is True
    assert policies_returned([], set()) is True
    result = policies_returned(["parking"], {"wifi"})
    assert isinstance(result, str) and "parking" in result


@pytest.mark.parametrize(("text", "route", "kwargs", "problem"), [
    ("Hi {{PRICE}}", "policy", {}, "unknown placeholders"),
    ("Hi {{HOLD_UNTIL}}", "quote", {"offers": 1}, "unknown placeholders"),  # code's own
    ("Open {{OPEN_SEASON}}", "decline", {}, "without CLOSED"),
    ("{{REQUEST_SUMMARY}}", "ask_info", {"summarisable": False}, "cannot be summarised"),
    ("{{QUOTE_BLOCK}}", "quote", {"offers": 0}, "nothing offered"),
])  # fmt: skip
def test_placeholders_the_run_cannot_render_fail(
    text: str, route: str, kwargs: dict[str, Any], problem: str
) -> None:
    args = {"offers": 0, "summarisable": True, "closed_year": None} | kwargs
    result = placeholders_valid(text, route, **args)
    assert isinstance(result, str) and problem in result


def test_renderable_placeholders_pass() -> None:
    text = "{{REQUEST_SUMMARY}}\n{{QUOTE_BLOCK}}\nOpen {{OPEN_SEASON}}"
    assert placeholders_valid(text, "quote", offers=1, summarisable=True, closed_year=2027)


def test_the_router_and_the_tools_know_the_same_routes() -> None:
    assert get_args(AgentRoute.__value__) == get_args(ToolRoute.__value__)
