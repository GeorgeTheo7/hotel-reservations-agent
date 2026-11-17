"""The checks on a draft the model wrote (D72, D74, D76): one definition, run twice.

The loop runs them on every `write_draft` (D76: a first failure goes back to the model,
a second is GUARDRAIL_FAIL); the run's final write runs them again before storing.

- `load_facts` reads, once, what the checks need from the database: whether the run's
  request can fill {{REQUEST_SUMMARY}} (complete, or its known part, D80) and whether
  its date was relative or a holiday (guardrail 3). The request is committed in phase 1
  and never changes, so the loop and the final write see the same facts.
- `check_draft` is pure: D72's route and offers, then `with_summary` (D80: code inserts
  {{REQUEST_SUMMARY}} where guardrail 3 requires it), then guardrails 1-7 and the
  placeholders on that text, which the final write renders (`Checked.text`).
"""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

import psycopg
from psycopg.rows import TupleRow

from anemos.guardrails import (
    OPEN_SEASON,
    QUOTE_BLOCK,
    REQUEST_SUMMARY,
    ProposedOffer,
    holds_valid,
    no_forbidden_phrases,
    numbers_from_policies,
    quote_block_once,
    reply_script,
    summary_present,
)
from anemos.policies.search import Hit
from anemos.reservations.errors import Closed, NeedsInfo, NoneFound
from anemos.reservations.formatting import Lang
from anemos.reservations.tools import StoredQuote, load_known, load_request

type Conn = psycopg.Connection[TupleRow]

MODEL_PLACEHOLDERS = (QUOTE_BLOCK, REQUEST_SUMMARY, OPEN_SEASON)
_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")


@dataclass(frozen=True)
class DraftFacts:
    """What the checks need beyond the loop's own results."""

    router_route: str
    lang: Lang
    summarisable: bool  # {{REQUEST_SUMMARY}} can be rendered (D74, D80)
    relative_or_holiday: bool  # guardrail 3 on ask_info (D28, D29)


@dataclass(frozen=True)
class Checked:
    route: str
    offers: list[ProposedOffer]
    results: dict[str, bool | str]
    text: str  # the draft as checked: the model's, with the summary code inserted (D80)

    @property
    def passed(self) -> bool:
        return all(result is True for result in self.results.values())

    @property
    def failures(self) -> list[str]:
        return [str(r) for r in self.results.values() if r is not True]


def load_facts(conn: Conn, request_id: int | None, router_route: str, lang: Lang) -> DraftFacts:
    """Reads in the caller's transaction (the caller ends it)."""
    summarisable = relative = False
    if request_id is not None:
        loaded = load_request(conn, request_id)
        summarisable = not isinstance(loaded, NeedsInfo) or load_known(conn, request_id) is not None
        row = conn.execute(
            "SELECT extracted -> 'date_spec' ->> 'kind' FROM requests WHERE id = %s",
            (request_id,),
        ).fetchone()
        relative = row is not None and row[0] in ("relative", "holiday")
    return DraftFacts(router_route, lang, summarisable, relative)


def check_draft(
    text: str,
    offer_quote_ids: Sequence[int],
    policy_ids: Sequence[str],
    facts: DraftFacts,
    *,
    quotes: Mapping[int, StoredQuote],
    hits: Mapping[str, Hit],
    outcomes: Iterable[str],
    closed_year: int | None,
) -> Checked:
    offers = proposed_offers(offer_quote_ids, quotes)
    route = draft_route([o.quote_id for o in offers], set(outcomes), facts.router_route)
    text = with_summary(text, route, facts)
    policy_texts = [h.text for h in hits.values()]
    results: dict[str, bool | str] = {
        "g1_quote_block_once": quote_block_once(text, route),
        "g2_numbers_from_policies": numbers_from_policies(text, policy_texts),
        "g3_summary_present": summary_present(
            text, route, relative_or_holiday=facts.relative_or_holiday),
        "g4_reply_script": reply_script(text, facts.lang),
        "g5_no_forbidden_phrases": no_forbidden_phrases(text, policy_texts),
        "g6_policies_returned": policies_returned(policy_ids, hits),
        "g7_holds_valid": holds_valid(
            offers, quotes, {i: q.offer.hold_allowed for i, q in quotes.items()}),
        "placeholders": placeholders_valid(
            text, route, offers=len(offers), summarisable=facts.summarisable,
            closed_year=closed_year),
    }  # fmt: skip
    return Checked(route, offers, results, text)


def with_summary(text: str, route: str, facts: DraftFacts) -> str:
    """D80 (owner, week 6): code inserts {{REQUEST_SUMMARY}} wherever guardrail 3
    requires it, as its own paragraph after the greeting (the first blank line; first if
    there is none). Not when the model already wrote it (kept where it is, never twice),
    and not when it cannot render (nothing known): guardrail 3 then fails as before."""
    if not facts.summarisable:
        return text
    if summary_present(text, route, relative_or_holiday=facts.relative_or_holiday) is True:
        return text  # already there (the model's, kept), or not required on this route
    head, sep, rest = text.partition("\n\n")
    if not sep:
        return f"{REQUEST_SUMMARY}\n\n{text}"
    return f"{head}\n\n{REQUEST_SUMMARY}\n\n{rest}"


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
    text: str, route: str, *, offers: int, summarisable: bool, closed_year: int | None
) -> bool | str:
    """Only the three placeholders the model may write, each renderable in this run."""
    problems = sorted({p for p in _PLACEHOLDER.findall(text)} - set(MODEL_PLACEHOLDERS))
    if problems:
        return f"unknown placeholders: {problems}"
    if OPEN_SEASON in text and closed_year is None:
        return f"{OPEN_SEASON} without CLOSED in this run"
    if REQUEST_SUMMARY in text and not summarisable:
        return f"{REQUEST_SUMMARY} but the request cannot be summarised (no dates, no party)"
    if route == "quote" and offers == 0:
        return "quote route with nothing offered"
    return True
