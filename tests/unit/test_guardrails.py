"""Guardrails 1 and 7 (D53): pure checks, no database."""

import pytest

from anemos.guardrails import (
    ProposedOffer,
    all_passed,
    holds_valid,
    quote_block_once,
    rendered_blocks_unchanged,
)

pytestmark = pytest.mark.unit


# --- guardrail 1 ----------------------------------------------------------------------


def test_quote_route_needs_the_block_exactly_once() -> None:
    assert quote_block_once("Dear guest,\n{{QUOTE_BLOCK}}\nRegards", "quote") is True


@pytest.mark.parametrize("text", ["Dear guest, 120 euros.", "{{QUOTE_BLOCK}} {{QUOTE_BLOCK}}"])
def test_quote_route_without_or_with_two_blocks_fails(text: str) -> None:
    result = quote_block_once(text, "quote")
    assert isinstance(result, str)
    assert "expected 1" in result


@pytest.mark.parametrize("route", ["ask_info", "policy", "decline"])
def test_other_routes_must_not_quote(route: str) -> None:
    assert quote_block_once("Could you tell us the ages?", route) is True
    assert isinstance(quote_block_once("{{QUOTE_BLOCK}}", route), str)


# --- guardrail 7 ----------------------------------------------------------------------


def test_single_exact_offer_may_be_held() -> None:
    assert holds_valid([ProposedOffer(1, hold=True)], {1}, {1: True}) is True


def test_offers_without_a_hold_pass() -> None:
    offers = [ProposedOffer(1, hold=False), ProposedOffer(2, hold=False)]
    assert holds_valid(offers, {1, 2}, {1: False, 2: False}) is True


def test_offer_of_a_quote_from_another_run_fails() -> None:
    result = holds_valid([ProposedOffer(9, hold=False)], {1}, {1: True})
    assert isinstance(result, str)
    assert "not priced in this run" in result


def test_hold_among_several_offers_fails() -> None:
    """D9's draft half: the database cannot count offers, so this check must."""
    offers = [ProposedOffer(1, hold=True), ProposedOffer(2, hold=False)]
    result = holds_valid(offers, {1, 2}, {1: True, 2: True})
    assert isinstance(result, str)
    assert "single offer" in result


def test_hold_on_a_changed_quote_fails() -> None:
    result = holds_valid([ProposedOffer(1, hold=True)], {1}, {1: False})
    assert isinstance(result, str)
    assert "cannot be held" in result


def test_all_passed() -> None:
    assert all_passed({"g1": True, "g7": True}) is True
    assert all_passed({"g1": True, "g7": "a hold with 2 offers"}) is False


# --- price check on owner edits (D63) ---------------------------------------------

BLOCK = "Our offer: Sea View Double, 1 room\nAccommodation total: €390.00"


def test_block_kept_verbatim_passes() -> None:
    assert rendered_blocks_unchanged(f"Dear Maria,\n{BLOCK}\nThanks!", [BLOCK]) is True


def test_changed_price_warns() -> None:
    edited = BLOCK.replace("€390.00", "€350.00")
    result = rendered_blocks_unchanged(f"Dear guest,\n{edited}", [BLOCK])
    assert result == "edited-price warning: quote block 1 of 1 changed or removed"


def test_deleted_block_warns_and_names_it() -> None:
    other = "Our offer: Family Suite, 1 room\nAccommodation total: €540.00"
    result = rendered_blocks_unchanged(f"Dear guest,\n{BLOCK}", [BLOCK, other])
    assert result == "edited-price warning: quote block 2 of 2 changed or removed"


def test_no_blocks_nothing_to_check() -> None:
    """ask_info and decline drafts offer nothing; guardrail 2 (week 6) covers numbers
    an owner might add to free text."""
    assert rendered_blocks_unchanged("Could you tell us the ages?", []) is True
