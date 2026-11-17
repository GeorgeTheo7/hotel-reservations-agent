"""Running the model's read-tool calls (D11, D71), on the run's one connection.

Each call is its own short transaction on the run's connection (D71: no transaction
open while the model works; one connection per run, not one per tool, so a run does
not churn local ports, NOTES 2026-11-16): the tool reads the committed request and
hotel data, stores what it prices (quotes are write-once, D58), writes one
`agent.tool_called` audit row, and commits. `search_policies` makes its selector call
first, with no transaction open, then records the result in a short transaction.

What a call gives back is either an `Answer` for the model or `Ends`: a result the loop
may not continue past (D11: the model may escalate, never un-escalate):
- a rule escalation from a tool (NO_CALENDAR, GROUP, ACCESSIBILITY, ...) -> its code;
- NO_MATCH from search_policies -> NO_POLICY (D73);
- NO_RATE (a data error) -> SYSTEM_ERROR; MIN_STAY_CLOSED -> MODEL (D62).

An `Answer` carries no money amount: the model never sees a price, so it cannot restate
one; a price reaches the guest only through the code-rendered {{QUOTE_BLOCK}} (D8).
It also carries what the loop keeps: the quotes stored, the outcome codes seen (D72
decides the draft's route from them) and the policy sections returned (guardrail 6).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel

from anemos.agent.tools import (
    CHECK_AVAILABILITY,
    QUOTE_PRICE,
    SEARCH_POLICIES,
    SUGGEST_ALTERNATIVES,
    SearchPoliciesArgs,
)
from anemos.audit import AuditEvent, write_audit
from anemos.clock import Clock
from anemos.llm.adapter import LLM
from anemos.policies.query import InvalidQuery
from anemos.policies.search import Hit, NoMatch, select
from anemos.reservations.errors import (
    Closed,
    FullyBooked,
    InvalidDates,
    MinStay,
    MinStayIntoClosed,
    NeedsEscalation,
    NeedsInfo,
    NoneFound,
    NoRate,
    OriginalAvailable,
    ReasonCode,
)
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.tools import (
    Alternatives,
    Availability,
    LoadedRequest,
    StoredQuote,
    WindowQuotes,
    check_availability,
    load_request,
    quote_price,
    suggest_alternatives,
)

type Conn = psycopg.Connection[TupleRow]


@dataclass(frozen=True)
class Answer:
    """What goes back to the model (`output`, JSON), and what the loop keeps."""

    output: dict[str, Any]
    quotes: tuple[StoredQuote, ...] = ()  # rendered from these in phase 3 (D8)
    outcome: str | None = None  # CLOSED, NONE_FOUND or NEEDS_INFO: D72 reads them
    closed_year: int | None = None  # CLOSED: the year {{OPEN_SEASON}} names
    hits: tuple[Hit, ...] = ()


@dataclass(frozen=True)
class Ends:
    """A result the loop stops at: the email is escalated with this code."""

    code: ReasonCode
    detail: str


@dataclass
class ToolRunner:
    conn: Conn
    clock: Clock
    llm: LLM
    email_id: int
    request_id: int | None  # None: no request (a policy question; no reservation tools)
    calls: list[str] = field(default_factory=list)

    def run(self, name: str, args: BaseModel) -> Answer | Ends:
        """One read-tool call. An exception (a bug, a failed selector call) rolls the
        transaction back and propagates: the loop escalates SYSTEM_ERROR."""
        self.calls.append(name)
        if name == SEARCH_POLICIES:
            assert isinstance(args, SearchPoliciesArgs)
            return self._search(args.query)
        tool = _RESERVATION_TOOLS[name]  # KeyError: a tool the loop should not have run
        if self.request_id is None:
            raise ValueError(f"{name} needs a stored request; email {self.email_id} has none")
        request_id = self.request_id
        return self._in_transaction(
            name, {}, lambda hotel: tool(self.conn, hotel, self.clock, request_id)
        )

    def _search(self, query: str) -> Answer | Ends:
        found = select(self.llm, query, email_id=self.email_id)  # nothing open meanwhile
        result: Answer | Ends
        match found:
            case InvalidQuery(message=message):
                result = Answer({"error": "INVALID_QUERY", "message": message})
            case NoMatch():
                result = Ends(ReasonCode.NO_POLICY, f"no policy answers: {found.query}")
            case _:
                sections = [{"policy_id": h.policy_id, "title": h.title, "text": h.text}
                            for h in found.hits]  # fmt: skip
                result = Answer({"result": "POLICIES", "sections": sections}, hits=found.hits)
        return self._in_transaction(SEARCH_POLICIES, {"query": query}, lambda _: result,
                                    record=found.record())  # fmt: skip

    def _in_transaction(
        self,
        name: str,
        arguments: dict[str, Any],
        call: Callable[[HotelData], Answer | Ends],
        *,
        record: dict[str, Any] | None = None,
    ) -> Answer | Ends:
        try:
            result = call(load_hotel_data(self.conn))
            output: dict[str, Any] = {"tool": name, "arguments": arguments}
            if isinstance(result, Ends):
                output["ends"] = {"code": result.code.value, "detail": result.detail}
            else:
                output["answer"] = result.output
            if record is not None:
                output["record"] = record  # search_policies: texts, hashes, model (D15)
            write_audit(self.conn, self.clock.now(), "agent", AuditEvent.AGENT_TOOL_CALLED,
                        email_id=self.email_id, output=output)  # fmt: skip
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return result


# --- the reservation tools, bound to the stored request (D11) ---------------------------


def _check_availability(
    conn: Conn, hotel: HotelData, clock: Clock, request_id: int
) -> Answer | Ends:
    """The stored request's own stay; a window or a holiday is refused (quote_price
    lists a window's options and decides a holiday)."""
    loaded = load_request(conn, request_id)
    if isinstance(loaded, NeedsInfo):
        return _needs_info(loaded)
    if not isinstance(loaded, LoadedRequest):
        return Answer({"error": "NOT_FOR_THIS_REQUEST",
                       "message": "the guest gave a range of dates or a holiday, not one "
                                  "stay: call quote_price"})  # fmt: skip
    stay = loaded.request.stay
    result = check_availability(conn, hotel, clock, stay.check_in, stay.check_out)
    match result:
        case Availability(stay=checked, free_rooms=free):
            return Answer({"result": "AVAILABLE", "nights": len(checked.nights),
                           "free_rooms": {t: len(rooms) for t, rooms in free.items()}})  # fmt: skip
        case InvalidDates(reason=reason):
            return Answer({"result": "INVALID_DATES", "reason": reason})
        case MinStay(min_nights=n):
            return Answer({"result": "MIN_STAY", "min_nights": n})
        case Closed() | NeedsEscalation():
            return _common(result)


def _quote_price(conn: Conn, hotel: HotelData, clock: Clock, request_id: int) -> Answer | Ends:
    result = quote_price(conn, hotel, clock, request_id)
    match result:
        case StoredQuote():
            return _quoted("QUOTED", (result,))
        case WindowQuotes(options=options):
            return _quoted("WINDOW_OPTIONS", options)
        case FullyBooked(room_type_id=room_type, rooms_needed=needed, rooms_free=free):
            return Answer({"result": "FULLY_BOOKED", "room_type": room_type,
                           "rooms_needed": needed, "rooms_free": free,
                           "next": SUGGEST_ALTERNATIVES})  # fmt: skip
        case _:
            return _common(result)


def _suggest_alternatives(
    conn: Conn, hotel: HotelData, clock: Clock, request_id: int
) -> Answer | Ends:
    loaded = load_request(conn, request_id)
    if not isinstance(loaded, LoadedRequest | NeedsInfo):
        return Answer({"error": "NOT_FOR_THIS_REQUEST",
                       "message": "a range of dates or a holiday has no alternatives: "
                                  "quote_price gives its options"})  # fmt: skip
    result = suggest_alternatives(conn, hotel, clock, request_id)
    match result:
        case Alternatives(kind=kind, options=options):
            return _quoted("ALTERNATIVES", options, kind=kind.value)
        case OriginalAvailable(room_type_id=room_type, rooms=rooms):
            return Answer({"result": "ORIGINAL_AVAILABLE", "room_type": room_type,
                           "rooms": rooms, "next": QUOTE_PRICE})  # fmt: skip
        case _:
            return _common(result)


_RESERVATION_TOOLS: dict[str, Callable[[Conn, HotelData, Clock, int], Answer | Ends]] = {
    CHECK_AVAILABILITY: _check_availability,
    QUOTE_PRICE: _quote_price,
    SUGGEST_ALTERNATIVES: _suggest_alternatives,
}


def _quoted(result: str, quotes: tuple[StoredQuote, ...], **extra: str) -> Answer:
    """Quote ids and what each is, never an amount."""
    shown = [{"quote_id": q.quote_id, "room_type": q.offer.quote.room_type_id,
              "rooms": q.offer.quote.rooms, "nights": len(q.offer.quote.stay.nights),
              "changes": sorted(c.value for c in q.offer.changes)}
             for q in quotes]  # fmt: skip
    return Answer({"result": result, **extra, "quotes": shown}, quotes=tuple(quotes))


def _needs_info(info: NeedsInfo) -> Answer:
    return Answer({"result": "NEEDS_INFO", "missing": info.missing}, outcome=NeedsInfo.code)


def _common(
    result: NeedsInfo | Closed | NoneFound | MinStayIntoClosed | NoRate | NeedsEscalation,
) -> Answer | Ends:
    """The outcomes several tools share."""
    match result:
        case NeedsInfo():
            return _needs_info(result)
        case Closed(nights=nights):
            return Answer({"result": "CLOSED"}, outcome=Closed.code, closed_year=nights[0].year)
        case NoneFound():
            return Answer({"result": "NONE_FOUND"}, outcome=NoneFound.code)
        case MinStayIntoClosed(min_nights=n):
            return Ends(ReasonCode.MODEL, f"MIN_STAY_CLOSED: minimum {n} nights runs into "
                                          "closed nights")  # fmt: skip
        case NoRate(nights=nights):
            return Ends(ReasonCode.SYSTEM_ERROR, f"no rate for nights {list(map(str, nights))}")
        case NeedsEscalation(reason=reason, detail=detail):
            return Ends(reason, detail)
