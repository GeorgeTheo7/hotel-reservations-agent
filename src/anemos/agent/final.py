"""The run's final write on an agent route (D72, D74), inside phase 3's transaction:
the sender lock is held and the email is still `processing` (the caller checked).

An escalation (the model's, or one code ended the loop with) is written as is. A draft
the model wrote is checked against what this run's tools returned, then rendered and
stored:

- **Route (D72):** any offer -> quote; else CLOSED or NONE_FOUND seen in this run ->
  decline; else NEEDS_INFO -> ask_info; else the router's route.
- **Hold (D72):** exactly one offer, and its quote's hold_allowed (D9).
- **Checks** (every result stored with the draft version): guardrail 1 (quote block
  exactly once on the quote route, never elsewhere), guardrail 6 (every cited policy
  id was returned by search_policies in this run), guardrail 7 (offers are this run's
  quotes; a hold satisfies D9), and the placeholders (only {{QUOTE_BLOCK}},
  {{REQUEST_SUMMARY}} and {{OPEN_SEASON}}, each renderable here). Any failure ->
  GUARDRAIL_FAIL, no draft (a redraft is week 6).
- **Rendering:** from hotel data read in this transaction and the quotes the tools
  priced (kept in memory: the stored row alone cannot rebuild a quote block).
  {{HOLD_UNTIL}} inside a held block stays for approval to fill (D8).
"""

import re
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime

import psycopg
from psycopg.rows import TupleRow

from anemos.agent.loop import DraftWanted, EscalationWanted, LoopResult, LoopState
from anemos.drafting import (
    OPEN_SEASON,
    REQUEST_SUMMARY,
    Drafted,
    loaded_request,
    open_season,
    render_offers,
    store_draft,
)
from anemos.guardrails import (
    QUOTE_BLOCK,
    ProposedOffer,
    all_passed,
    holds_valid,
    quote_block_once,
)
from anemos.inbox import escalate
from anemos.reservations.errors import Closed, NeedsInfo, NoneFound, ReasonCode
from anemos.reservations.formatting import Lang
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.tools import StoredQuote

type Conn = psycopg.Connection[TupleRow]

MODEL_PLACEHOLDERS = (QUOTE_BLOCK, REQUEST_SUMMARY, OPEN_SEASON)
_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")


def draft_route(offers: Sequence[int], outcomes: Collection[str], router_route: str) -> str:
    """D72: a pure function of the run, never the model's choice."""
    if offers:
        return "quote"
    if Closed.code in outcomes or NoneFound.code in outcomes:
        return "decline"
    if NeedsInfo.code in outcomes:
        return "ask_info"
    return router_route


def proposed_offers(
    offer_ids: Sequence[int], quotes: Mapping[int, StoredQuote]
) -> list[ProposedOffer]:
    """D72: a hold only on a single offer whose quote is exactly the request (D9)."""
    ids = list(dict.fromkeys(offer_ids))  # a repeated id is one offer
    single = ids[0] if len(ids) == 1 else None
    hold = single is not None and single in quotes and quotes[single].offer.hold_allowed
    return [ProposedOffer(i, hold=hold) for i in ids]


def policies_returned(cited: Sequence[str], returned: Collection[str]) -> bool | str:
    """Guardrail 6: every cited policy id was returned by search_policies in this run.
    (Limit: proves citation, not faithfulness; measured in evaluation.)"""
    missing = [p for p in cited if p not in returned]
    if not missing:
        return True
    return f"cites policies not returned by search_policies in this run: {missing}"


def placeholders_valid(
    text: str, route: str, *, offers: int, has_request: bool, closed_year: int | None
) -> bool | str:
    """Only the three placeholders the model may write, each renderable in this run."""
    problems = sorted({p for p in _PLACEHOLDER.findall(text)} - set(MODEL_PLACEHOLDERS))
    if problems:
        return f"unknown placeholders: {problems}"
    if OPEN_SEASON in text and closed_year is None:
        return f"{OPEN_SEASON} without CLOSED in this run"
    if REQUEST_SUMMARY in text and not has_request:
        return f"{REQUEST_SUMMARY} but the email has no stored request"
    if route == "quote" and offers == 0:
        return "quote route with nothing offered"
    return True


def finish_agent(
    conn: Conn,
    now: datetime,
    email_id: int,
    loop: LoopResult,
    *,
    router_route: str,
    request_id: int | None,
    lang: Lang,
) -> Drafted | EscalationWanted:
    end = loop.end
    if isinstance(end, EscalationWanted):
        escalate(conn, now, email_id, (end.code,), end.detail, actor=end.by)
        return end
    return _draft(conn, now, email_id, end, loop.state, router_route, request_id, lang)


def _draft(
    conn: Conn, now: datetime, email_id: int, wanted: DraftWanted, state: LoopState,
    router_route: str, request_id: int | None, lang: Lang,
) -> Drafted | EscalationWanted:  # fmt: skip
    offers = proposed_offers(wanted.offer_quote_ids, state.quotes)
    route = draft_route([o.quote_id for o in offers], state.outcomes, router_route)
    text = wanted.text
    results: dict[str, bool | str] = {
        "g1_quote_block_once": quote_block_once(text, route),
        "g6_policies_returned": policies_returned(wanted.policy_ids, state.hits),
        "g7_holds_valid": holds_valid(
            offers, state.quotes, {i: q.offer.hold_allowed for i, q in state.quotes.items()}
        ),
        "placeholders": placeholders_valid(
            text, route, offers=len(offers), has_request=request_id is not None,
            closed_year=state.closed_year,
        ),
    }  # fmt: skip
    if not all_passed(results):
        detail = "; ".join(str(r) for r in results.values() if r is not True)
        failed = EscalationWanted(ReasonCode.GUARDRAIL_FAIL, detail, "agent")
        escalate(conn, now, email_id, (failed.code,), detail, actor="agent")
        return failed

    hotel = load_hotel_data(conn)
    blocks: dict[int, str] = {}
    if request_id is not None:
        quotes = [state.quotes[o.quote_id] for o in offers]
        text, blocks = render_offers(hotel, text, loaded_request(conn, request_id), quotes,
                                     offers, lang)  # fmt: skip
    if state.closed_year is not None:
        text = text.replace(OPEN_SEASON, open_season(hotel, state.closed_year, lang))
    return store_draft(conn, now, email_id, request_id, route, text, results, offers, blocks,
                       audit={"policy_ids": list(wanted.policy_ids)})  # fmt: skip
