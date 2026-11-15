"""Routing in code (D70): what happens to an email before any agent runs.

`route` is a pure function: no database, no clock, no model. It reads the email's
own facts, the extraction, the dates code resolved from it (`datespec.resolve`) and
the hotel's calendar and room types, and returns one of:

- `SetAside`: spam; no draft, no reply.
- `Escalate(codes)`: the owner handles it; no draft.
- `ToAgent(route)`: drafting goes on, on route quote, ask_info, decline or policy.

The first rule that matches decides, in this order (D70):

 1. thread reply (`in_reply_to` set)  -> THREAD_REPLY (before extraction: no model call)
 2. injection_suspected               -> INJECTION
 3. kind spam                          -> set aside
 4. language not en/el                 -> LANGUAGE
 5. flags legal/billing/medical        -> LEGAL, BILLING, MEDICAL (all present, one escalation)
 6. kind question or other             -> MODEL until the agent loop exists (week 5)
 7. a night in a year with no calendar -> NO_CALENDAR (D36)
 8. every night closed                 -> decline (CLOSED, D62)
 9. less than 48 h to check-in         -> SHORT_NOTICE (D31)
10. 3+ rooms, or no plan in 2 rooms    -> GROUP (D31, D27)
11. fewer adults than rooms            -> UNACCOMPANIED_CHILDREN (D32)
12. accessibility need not met         -> ACCESSIBILITY (D33, D56)
13. arrival 22:01-05:59                -> LATE_ARRIVAL (D69)
14. anything missing                   -> ask_info; else quote

A rule whose input is missing is skipped, not failed: without dates, rules 7-9 are
skipped; without a complete party, the room-plan part of rule 10. The missing input
ends in ask_info (rule 14), but a rule that can be decided without it still applies
(5 rooms and no dates is GROUP, not a question).

Windows (Easter, part of a month; D7, D28): rule 7 looks at every night of every stay
the window allows; rule 8 declines only if all of those nights are closed (a partly
closed window goes on to quote, and the window options decide); rule 9 applies to one
stay only (window options skip short-notice check-ins). Fixed-date holidays (D68):
rules 7 and 8 look at the holiday's days; inside the season it is ask_info.

Rules 10-12 reuse the occupancy helpers that `build_offer` uses, so the router and
the quote tool cannot disagree about who fits; a unit test checks they agree.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

from anemos.extraction import ExtractionFlag, ExtractionOutput
from anemos.reservations.datespec import FixedHoliday, Window
from anemos.reservations.errors import InvalidDates, NeedsEscalation, NeedsInfo, ReasonCode
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import Party, Stay, party_from_counts
from anemos.reservations.occupancy import RoomPlan, smallest_fitting_plan
from anemos.reservations.seasons import is_open
from anemos.reservations.stay_rules import group, late_arrival, short_notice

LANGUAGES = frozenset({"en", "el"})  # spec Scope: EN and EL only
# Rule 5, in this order in the escalation.
FLAG_CODES: tuple[tuple[ExtractionFlag, ReasonCode], ...] = (
    (ExtractionFlag.LEGAL_THREAT, ReasonCode.LEGAL),
    (ExtractionFlag.BILLING_REQUEST, ReasonCode.BILLING),
    (ExtractionFlag.MEDICAL_NEED, ReasonCode.MEDICAL),
)
NOT_BUILT_YET = "week 5: the agent loop does not exist yet"

type Resolved = Stay | Window | FixedHoliday | NeedsInfo | InvalidDates | None
type AgentRoute = Literal["quote", "ask_info", "decline", "policy"]


@dataclass(frozen=True)
class EmailFacts:
    """What the router reads from the stored email itself."""

    received_at: datetime  # timezone-aware
    in_reply_to: str | None


@dataclass(frozen=True)
class SetAside:
    pass


@dataclass(frozen=True)
class Escalate:
    codes: tuple[ReasonCode, ...]
    detail: str

    def __post_init__(self) -> None:
        if not self.codes:
            raise ValueError("an escalation needs at least one reason code")


@dataclass(frozen=True)
class ToAgent:
    route: AgentRoute
    detail: str = ""  # ask_info: what is missing; decline: why


type Routed = SetAside | Escalate | ToAgent


def route(
    email: EmailFacts, extraction: ExtractionOutput | None, resolved: Resolved, hotel: HotelData
) -> Routed:
    # 1. Thread replies are escalated before extraction (spec Escalation rules).
    if email.in_reply_to is not None:
        return _escalate(ReasonCode.THREAD_REPLY, f"reply to {email.in_reply_to}")
    if extraction is None:
        raise ValueError("only a thread reply is routed without an extraction")

    # 2-6: the email as a whole.
    if ExtractionFlag.INJECTION_SUSPECTED in extraction.flags:
        return _escalate(ReasonCode.INJECTION, "extraction flagged injection_suspected")
    if extraction.kind == "spam":
        return SetAside()
    if extraction.language not in LANGUAGES:
        return _escalate(ReasonCode.LANGUAGE, f"language {extraction.language}")
    flagged = tuple(code for flag, code in FLAG_CODES if flag in extraction.flags)
    if flagged:
        return Escalate(flagged, "extraction flags: " + ", ".join(flagged))
    if extraction.kind in ("question", "other"):
        return _escalate(ReasonCode.MODEL, f"kind {extraction.kind}; {NOT_BUILT_YET}")

    # 7-9: the dates, if code could resolve them.
    if isinstance(resolved, Stay | Window | FixedHoliday):
        found = _calendar_rule(hotel, resolved, email.received_at)
        if found is not None:
            return found

    # 10-12: the party and rooms, as far as they are known.
    party = party_from_counts(extraction.adults, extraction.children, extraction.child_ages)
    escalation = _rooms_rule(hotel, extraction, party)
    if escalation is not None:
        return _escalate(escalation.reason, escalation.detail)

    # 13.
    if extraction.arrival_time is not None:
        late = late_arrival(time.fromisoformat(extraction.arrival_time))
        if late is not None:
            return _escalate(late.reason, late.detail)

    # 14.
    if resolved is None:
        return ToAgent("ask_info", "dates of the stay")
    if isinstance(resolved, FixedHoliday):  # D68: inside the season, ask for dates
        return ToAgent("ask_info", f"dates for {resolved.key} (a holiday in the season)")
    if isinstance(resolved, NeedsInfo):
        return ToAgent("ask_info", resolved.missing)
    if isinstance(resolved, InvalidDates):
        return ToAgent("ask_info", f"valid dates ({resolved.reason})")
    if isinstance(party, NeedsInfo):
        return ToAgent("ask_info", party.missing)
    return ToAgent("quote")


def _escalate(code: ReasonCode, detail: str) -> Escalate:
    return Escalate((code,), detail)


def candidate_nights(dates: Stay | Window | FixedHoliday) -> tuple[date, ...]:
    """Every night the request could include: a stay's nights, a window's candidate
    nights (`Window.candidate_nights`), or a fixed-date holiday's days (D68)."""
    if isinstance(dates, Stay):
        return dates.nights
    if isinstance(dates, Window):
        return dates.candidate_nights()
    return dates.days()


def _calendar_rule(
    hotel: HotelData, dates: Stay | Window | FixedHoliday, received_at: datetime
) -> Routed | None:
    nights = candidate_nights(dates)
    missing = sorted({n.year for n in nights if not hotel.has_calendar_for(n.year)})
    if missing:  # 7. D36: before "closed", which would be a guess
        years = ", ".join(map(str, missing))
        return _escalate(ReasonCode.NO_CALENDAR, f"no season calendar for {years}")
    if not any(is_open(hotel, night) for night in nights):  # 8. D62
        return ToAgent("decline", f"closed: every night from {nights[0]} to {nights[-1]}")
    if isinstance(dates, Stay):  # 9. D31; window options skip short-notice check-ins
        notice = short_notice(dates.check_in, received_at)
        if notice is not None:
            return _escalate(notice.reason, notice.detail)
    return None


def _rooms_rule(
    hotel: HotelData, extraction: ExtractionOutput, party: Party | NeedsInfo
) -> NeedsEscalation | None:
    """Rules 10-12 in order. Not stated = one room (as `tools.load_request`)."""
    rooms = extraction.rooms or 1
    need = ExtractionFlag.ACCESSIBILITY_NEED in extraction.flags

    # 10. GROUP: 3+ rooms; or, with a complete party and no accessibility need (a need
    # is served by the Accessible room only, rule 12), no plan fits 2 rooms (D27).
    too_many = group(rooms)
    if too_many is not None:
        return too_many
    plan = smallest_fitting_plan(hotel, party) if isinstance(party, Party) and not need else None
    if isinstance(plan, NeedsEscalation) and plan.reason is ReasonCode.GROUP:
        return plan

    # 11. UNACCOMPANIED_CHILDREN (D32): adults >= the rooms asked for, and >= the rooms
    # of the plan; checked after GROUP. Needs only the adult count, not every age.
    if extraction.adults is not None and extraction.adults < rooms:
        return NeedsEscalation(
            ReasonCode.UNACCOMPANIED_CHILDREN,
            f"{extraction.adults} adult(s) for {rooms} room(s)",
        )
    if isinstance(plan, NeedsEscalation):  # the plan's own D32 check
        return plan
    assert plan is None or isinstance(plan, RoomPlan)

    # 12. ACCESSIBILITY: one Accessible room exists (D56), and the party must fit it (D33).
    if need and rooms > 1:
        return NeedsEscalation(
            ReasonCode.ACCESSIBILITY,
            f"{rooms} rooms with an accessibility need; one accessible room",
        )
    if need and isinstance(party, Party):
        accessible = smallest_fitting_plan(hotel, party, accessibility_need=True)
        if isinstance(accessible, NeedsEscalation):
            return accessible
    return None
