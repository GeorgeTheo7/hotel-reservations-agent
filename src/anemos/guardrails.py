"""Guardrails: code checks on every draft before the owner sees it (spec Guardrails).

Week 3 has guardrails 1 and 7 (D53); the others arrive in week 6. Each check returns
True or a short reason, never raises, so all results can be stored with the draft
version (draft_versions.guardrail_results) and a failure is visible, not hidden.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass

QUOTE_BLOCK = "{{QUOTE_BLOCK}}"
PRICE_CHECK = "price_check"  # guardrail result key of an owner edit (D63)


@dataclass(frozen=True)
class ProposedOffer:
    quote_id: int
    hold: bool


def quote_block_once(text: str, route: str) -> bool | str:
    """Guardrail 1: on the quote route, {{QUOTE_BLOCK}} exactly once (prices come only
    from the code-rendered block, D8); on other routes, not at all."""
    found = text.count(QUOTE_BLOCK)
    expected = 1 if route == "quote" else 0
    if found == expected:
        return True
    return f"{QUOTE_BLOCK} appears {found} time(s) on the {route} route, expected {expected}"


def holds_valid(
    offers: Sequence[ProposedOffer], run_quote_ids: Collection[int], hold_allowed: dict[int, bool]
) -> bool | str:
    """Guardrail 7: proposed holds come from this run's quotes and satisfy D9: the held
    quote matches the request exactly (`hold_allowed`) and is the draft's only offer.

    The database enforces the first two again (draft_offers foreign keys and CHECK);
    "only offer" needs a count across rows, so this check is its only guard.
    """
    unknown = [o.quote_id for o in offers if o.quote_id not in run_quote_ids]
    if unknown:
        return f"offers quotes not priced in this run: {unknown}"
    held = [o for o in offers if o.hold]
    if not held:
        return True
    if len(offers) != 1:
        return f"a hold with {len(offers)} offers; D9 allows a hold only on a single offer"
    if not hold_allowed.get(held[0].quote_id, False):
        return f"quote {held[0].quote_id} differs from the request; it cannot be held (D9)"
    return True


def rendered_blocks_unchanged(text: str, blocks: Sequence[str]) -> bool | str:
    """Price check on an owner edit (D63, D10): every quote block of the agent's
    version must still appear verbatim. A changed amount, a reworded line or a deleted
    block is the edited-price warning, which approval refuses unless confirm_warnings.
    """
    missing = [i + 1 for i, block in enumerate(blocks) if block not in text]
    if not missing:
        return True
    which = ", ".join(map(str, missing))
    return f"edited-price warning: quote block {which} of {len(blocks)} changed or removed"


def all_passed(results: dict[str, bool | str]) -> bool:
    return all(result is True for result in results.values())
