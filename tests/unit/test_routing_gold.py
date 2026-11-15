"""The router (D70) on the gold labels of every evaluation case.

Each case is the gold extraction (`EmailCase.gold_output`), its dates resolved by code
at the email's received_at, routed against the seeded 2027 hotel. A case either
matches its labelled route and reason codes exactly, or is listed below with what the
router gives today and why that differs. A listed case that starts to match fails too,
so these lists only ever shrink on purpose.

No model, no database: the gold labels stand in for extraction.
"""

from pathlib import Path
from typing import assert_never

import pytest

from anemos.db.seed import hotel_data
from anemos.email_cases import EmailCase, load_cases
from anemos.reservations.datespec import resolve
from anemos.reservations.errors import ReasonCode
from anemos.routing import EmailFacts, Escalate, Resolved, Routed, SetAside, ToAgent, route

pytestmark = pytest.mark.unit

EVAL_EMAILS = Path(__file__).resolve().parents[2] / "eval" / "emails"
CASES = {case.id: case for case in load_cases(EVAL_EMAILS)}
HOTEL = hotel_data()

type Outcome = tuple[str, tuple[ReasonCode, ...]]

GROUP, LEGAL, MODEL = ReasonCode.GROUP, ReasonCode.LEGAL, ReasonCode.MODEL

# Needs model judgement: the agent loop (week 5) decides these. Router outcome today.
WEEK_5: dict[str, Outcome] = {
    "en-discount-as-agreed": ("quote", ()),  # gold DISCOUNT: "50% off as agreed"
    "el-group-wedding": ("escalate", (GROUP,)),  # gold + DISCOUNT (spec open item)
    "el-complaint-legal": ("escalate", (LEGAL,)),  # gold + COMPLAINT (spec open item)
    "el-cancellation": ("escalate", (MODEL,)),  # gold CANCELLATION
    "el-change-dates": ("escalate", (MODEL,)),  # gold CHANGE
    "el-pets-policy": ("escalate", (MODEL,)),  # gold route policy
    "el-greeklish-transfer-policy": ("escalate", (MODEL,)),  # gold route policy
}

# Decided by the tools, not by routing (D70: availability is not routing).
BY_TOOLS: dict[str, Outcome] = {
    "en-fully-booked-decline": ("quote", ()),  # gold decline: NONE_FOUND (D60)
}

# Matched exactly today, but expected to change when D68 (fixed-date holidays) lands:
# Christmas 2026 has no calendar (NO_CALENDAR), Christmas 2027 is outside the season
# (decline). Until then datespec asks for dates, as the labels still say.
D68_PENDING = frozenset({"el-christmas-holiday", "el-christmas-2027"})

EXACT = sorted(set(CASES) - set(WEEK_5) - set(BY_TOOLS))


def routed(case: EmailCase) -> Routed:
    extraction = case.gold_output()
    resolved: Resolved = None
    if extraction is not None:
        spec = extraction.date_spec_value()
        if spec is not None:
            resolved = resolve(spec, case.email.received_at)
    facts = EmailFacts(case.email.received_at, case.email.in_reply_to)
    return route(facts, extraction, resolved, HOTEL)


def outcome(result: Routed) -> Outcome:
    match result:
        case SetAside():
            return ("set_aside", ())
        case Escalate(codes=codes):
            return ("escalate", tuple(sorted(codes)))
        case ToAgent(route=agent_route):
            return (agent_route, ())
        case _:
            assert_never(result)


def gold(case: EmailCase) -> Outcome:
    return (case.labels.route, tuple(sorted(case.labels.reason_codes)))


def test_every_case_is_counted_once() -> None:
    assert len(CASES) == 43
    listed = set(WEEK_5) | set(BY_TOOLS)
    assert listed <= set(CASES), f"listed but no such case: {listed - set(CASES)}"
    assert not set(WEEK_5) & set(BY_TOOLS)
    assert set(D68_PENDING) <= set(EXACT)
    assert len(EXACT) == 35


@pytest.mark.parametrize("case_id", EXACT)
def test_router_matches_the_gold_label(case_id: str) -> None:
    case = CASES[case_id]
    assert outcome(routed(case)) == gold(case)


@pytest.mark.parametrize(("case_id", "expected"), sorted({**WEEK_5, **BY_TOOLS}.items()))
def test_listed_case_routes_as_recorded_and_still_differs(case_id: str, expected: Outcome) -> None:
    case = CASES[case_id]
    got = outcome(routed(case))
    assert got == expected
    assert got != gold(case), f"{case_id} now matches its label: move it to EXACT"
