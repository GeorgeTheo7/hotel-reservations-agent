"""The run's final write on an agent route (D72, D74), inside phase 3's transaction:
the sender lock is held and the email is still `processing` (the caller checked).

An escalation (the model's, or one code ended the loop with) is written as is. A draft
the model wrote is checked against what this run's tools returned, then rendered and
stored:

- **Route (D72):** any offer -> quote; else CLOSED or NONE_FOUND seen in this run ->
  decline; else NEEDS_INFO -> ask_info; else the router's route.
- **Hold (D72):** exactly one offer, and its quote's hold_allowed (D9).
- **Checks** (every result stored with the draft version): guardrail 1 (quote block
  exactly once on the quote route, never elsewhere), 2 (every number in the model's
  text is a number of a returned policy, D78), 3 (the request summary where required,
  D80), 4 (the reply's script, D79), 5 (no forbidden phrase), 6 (every cited policy
  id was returned by search_policies in this run), 7 (offers are this run's quotes; a
  hold satisfies D9), and the placeholders (only {{QUOTE_BLOCK}}, {{REQUEST_SUMMARY}}
  and {{OPEN_SEASON}}, each renderable here), as `agent.checks` defines them. The loop
  ran them already (D76: one redraft); a failure here is GUARDRAIL_FAIL, no draft.
- **Rendering:** from hotel data read in this transaction and the quotes the tools
  priced (kept in memory: the stored row alone cannot rebuild a quote block).
  {{HOLD_UNTIL}} inside a held block stays for approval to fill (D8).
"""

from datetime import datetime

import psycopg
from psycopg.rows import TupleRow

from anemos.agent.checks import (  # re-exported: tests and callers import them from here
    check_draft,
    draft_route,
    load_facts,
    placeholders_valid,
    policies_returned,
    proposed_offers,
)
from anemos.agent.loop import DraftWanted, EscalationWanted, LoopResult, LoopState
from anemos.drafting import (
    OPEN_SEASON,
    REQUEST_SUMMARY,
    Drafted,
    known_summary,
    open_season,
    render_offers,
    store_draft,
)
from anemos.inbox import escalate
from anemos.reservations.errors import NeedsInfo, ReasonCode
from anemos.reservations.formatting import Lang
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.tools import load_known, load_request

type Conn = psycopg.Connection[TupleRow]

__all__ = ["draft_route", "finish_agent", "placeholders_valid", "policies_returned",
           "proposed_offers"]  # fmt: skip


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
    facts = load_facts(conn, request_id, router_route, lang)
    checked = check_draft(
        wanted.text, wanted.offer_quote_ids, wanted.policy_ids, facts, quotes=state.quotes,
        hits=state.hits, outcomes=state.outcomes, closed_year=state.closed_year,
    )  # fmt: skip
    if not checked.passed:  # D76: the loop already checked; this is the last guard
        detail = "; ".join(checked.failures)
        failed = EscalationWanted(ReasonCode.GUARDRAIL_FAIL, detail, "agent")
        escalate(conn, now, email_id, (failed.code,), detail, actor="agent")
        return failed

    # An ask_info request is incomplete: summarised from the part it knows (D80).
    found = None if request_id is None else load_request(conn, request_id)
    loaded = None if isinstance(found, NeedsInfo) else found
    known = None if loaded is not None or request_id is None else load_known(conn, request_id)
    hotel = load_hotel_data(conn)
    text, offers = wanted.text, checked.offers
    blocks: dict[int, str] = {}
    if loaded is not None:
        quotes = [state.quotes[o.quote_id] for o in offers]
        text, blocks = render_offers(hotel, text, loaded, quotes, offers, lang)
    elif known is not None:
        text = text.replace(REQUEST_SUMMARY, known_summary(hotel, known, lang))
    if state.closed_year is not None:
        text = text.replace(OPEN_SEASON, open_season(hotel, state.closed_year, lang))
    return store_draft(conn, now, email_id, request_id, checked.route, text, checked.results,
                       offers, blocks, audit={"policy_ids": list(wanted.policy_ids)})  # fmt: skip
