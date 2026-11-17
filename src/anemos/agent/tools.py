"""The tools the model may call (D11, D12, D70), as the adapter sends them.

- The three reservation tools take **no arguments**: code binds the run's stored request
  (dates, party, room type), so the model cannot price a request the guest did not make
  or compute a date for a tool (D6, D11).
- `search_policies(query)`: an English query; the selector picks the sections (D15).
- `write_draft(text, offer_quote_ids, policy_ids)` and `escalate(reason_code, note)` end
  the loop. write_draft has no route and no hold argument: code decides both (D72).
  `policy_ids` is an enum of the real ids, so the model cannot cite an invented one.
- `escalate` accepts only the codes that need model judgement (D11); codes raised by
  code (GROUP, NO_CALENDAR, ...) are not in its enum.
- No tool writes bookings or the outbox (D12): `hold_room` and `send_reply` run only in
  the approval handler and are never given to the model. A test keeps every tool of
  every route in `READ_ONLY | ENDING`.

Tools per route (D11, D70): quote, ask_info and decline get all six; policy gets
search_policies, write_draft and escalate; kind `other` gets escalate only.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from anemos.llm.adapter import Tool
from anemos.policies import PolicyId
from anemos.policies.query import QUERY_MAX_CHARS

type ToolRoute = Literal["quote", "ask_info", "decline", "policy", "other"]
# A plain alias, not `type X = ...`: pydantic writes a PEP 695 alias as a $ref, which
# the flat-schema check refuses (tool arguments must be flat).
ModelEscalationCode = Literal[
    "COMPLAINT", "DISCOUNT", "CHANGE", "CANCELLATION", "NO_POLICY", "MODEL"
]

CHECK_AVAILABILITY = "check_availability"
QUOTE_PRICE = "quote_price"
SUGGEST_ALTERNATIVES = "suggest_alternatives"
SEARCH_POLICIES = "search_policies"
WRITE_DRAFT = "write_draft"
ESCALATE = "escalate"

READ_ONLY = frozenset({CHECK_AVAILABILITY, QUOTE_PRICE, SUGGEST_ALTERNATIVES, SEARCH_POLICIES})
ENDING = frozenset({WRITE_DRAFT, ESCALATE})  # end the loop; nothing written until phase 3
WRITE_TOOLS = frozenset({"hold_room", "send_reply"})  # D12: approval handler only


class NoArgs(BaseModel):
    """No arguments: the tool reads the request stored for this email."""

    model_config = ConfigDict(extra="forbid")


class SearchPoliciesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        description=f"The guest's question in English, up to {QUERY_MAX_CHARS} characters, "
        "without < or >."
    )


class WriteDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="The whole reply to the guest, with placeholders.")
    offer_quote_ids: list[int] = Field(
        description="The quote ids this reply offers (from quote_price or "
        "suggest_alternatives in this conversation); empty if it offers none."
    )
    policy_ids: list[PolicyId] = Field(
        description="The policy sections this reply answers from (from search_policies "
        "in this conversation); empty if none."
    )


class EscalateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: ModelEscalationCode
    note: str = Field(description="One or two sentences in English for the hotel owner.")


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(CHECK_AVAILABILITY, "Which room types are free for the guest's stay, as "
             "stored for this email. Not for a range of dates or a holiday: use "
             "quote_price.", NoArgs),
        Tool(QUOTE_PRICE, "Price the guest's request as stored for this email and keep "
             "the quote; returns quote ids, or why it cannot be quoted.", NoArgs),
        Tool(SUGGEST_ALTERNATIVES, "After FULLY_BOOKED: other room types for the same "
             "stay, else the same stay up to a week earlier or later. Returns quote ids.",
             NoArgs),
        Tool(SEARCH_POLICIES, "Find the hotel policy sections that answer a question; "
             "returns their text.", SearchPoliciesArgs),
        Tool(WRITE_DRAFT, "Finish: the reply for the hotel owner to approve.",
             WriteDraftArgs),
        Tool(ESCALATE, "Finish: hand the email to the hotel owner instead of replying.",
             EscalateArgs),
    )
}  # fmt: skip


def _by_route(reservation: tuple[str, ...]) -> dict[ToolRoute, tuple[Tool, ...]]:
    every = (*reservation, SEARCH_POLICIES, WRITE_DRAFT, ESCALATE)
    return {
        "quote": tuple(TOOLS[n] for n in every),
        "ask_info": tuple(TOOLS[n] for n in every),
        "decline": tuple(TOOLS[n] for n in every),
        "policy": tuple(TOOLS[n] for n in (SEARCH_POLICIES, WRITE_DRAFT, ESCALATE)),
        "other": (TOOLS[ESCALATE],),
    }


# Each agent prompt version is hashed with the tool lists it was sent with (prompts,
# ToolPromptSpec), so each keeps its own table. agent_v1 (week 5) offered
# check_availability; agent_v2 (week 6) does not: the dev eval never called it, and
# quote_price checks availability itself (D59, D11). The function stays for MCP (D16).
TOOLS_BY_ROUTE_V1 = _by_route((CHECK_AVAILABILITY, QUOTE_PRICE, SUGGEST_ALTERNATIVES))
TOOLS_BY_ROUTE_V2 = _by_route((QUOTE_PRICE, SUGGEST_ALTERNATIVES))
TOOLS_BY_ROUTE = TOOLS_BY_ROUTE_V2  # the current prompt's (agent_v2)
