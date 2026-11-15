"""The request-to-offer path (spec week 3; D27, D33, D34): one function from what the
guest asked to the priced offer the reply would quote, or why there is none.

Order:
1. Calendar. A partly closed stay is trimmed to its open part (Ex 7) and re-checked
   (D34); a stay shorter than its minimum is lengthened to the min-stay version (Ex 4)
   and re-checked. A min-stay version that reaches closed nights is MIN_STAY_CLOSED
   (D57); no calendar for the year is returned as is.
2. Rooms. GROUP first (3+ rooms), then the room plan: the requested type if it fits;
   else the smallest fitting plan (D27); with an accessibility need, an adult in
   every room asked for first (D32, in the router's order, D70), then only the
   Accessible room (D33). Otherwise an adult in every room is checked by pricing.
3. Price the result.

Every substitution is recorded in `Offer.changes`, with the same names as the
database (`quotes.changes`); an offer with no changes is exactly what was asked and
is the only kind that may be held (D9). Pure: no database, no availability. Whether
the rooms are free is the tools' job (check_availability, suggest_alternatives).
"""

from dataclasses import dataclass
from enum import StrEnum

from anemos.reservations.errors import (
    Closed,
    MinStay,
    MinStayIntoClosed,
    NeedsEscalation,
    NoRate,
    OverCapacity,
    ReasonCode,
)
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import Party, RoomRequest
from anemos.reservations.occupancy import (
    RoomPlan,
    adult_count,
    adults_cover_rooms,
    fits,
    smallest_fitting_plan,
)
from anemos.reservations.pricing import Quote, price_stay
from anemos.reservations.stay_rules import check_stay, group, min_stay_version, open_part


class OfferChange(StrEnum):
    """How an offer differs from the request. Same values as the CHECK on
    quotes.changes (migration 0006); a test keeps the two equal."""

    OPEN_PART = "open_part"  # closed nights cut off (D34, Ex 7)
    MIN_STAY = "min_stay"  # lengthened to the minimum stay (Ex 4)
    OTHER_TYPE = "other_type"  # a room type other than the one asked (D27, D33)
    MORE_ROOMS = "more_rooms"  # 2 rooms instead of 1 (D27)
    OTHER_DATES = "other_dates"  # same length within +-7 d (suggest_alternatives)
    WINDOW_OPTION = "window_option"  # one option for approximate dates (D7, D28)


@dataclass(frozen=True)
class Offer:
    quote: Quote
    changes: frozenset[OfferChange]

    @property
    def hold_allowed(self) -> bool:
        """D9, the offer's half: it is exactly what the guest asked. (The draft's half,
        "it is the only offer", is checked by guardrail 7.)"""
        return not self.changes


def build_offer(
    hotel: HotelData, request: RoomRequest, party: Party, *, accessibility_need: bool = False
) -> Offer | Closed | MinStayIntoClosed | NoRate | NeedsEscalation:
    """The offer for this request, or why the reply cannot quote one.

    Expects a **resolved** request and a **complete** party: `request.stay` is a real
    stay (DateSpec already resolved, D6/D7), and every child has an age (`Party` cannot
    hold a child without one). Missing dates or ages are NEEDS_INFO before this point:
    the caller that turns a stored request into a RoomRequest and a Party (the
    quote_price tool) returns NEEDS_INFO instead of calling this function.

    An offer says nothing about availability: it is what the hotel would sell, priced
    from the database snapshot in `hotel`. Whether the rooms are free is the tools' job
    (check_availability, suggest_alternatives), and approval re-validates both (D47).

    Closed: every night of the stay is closed.
    MinStayIntoClosed: the stay (or its open part) is shorter than its minimum, and the
    min-stay version reaches closed nights (D57).
    NeedsEscalation: NO_CALENDAR, GROUP, ACCESSIBILITY, UNACCOMPANIED_CHILDREN.
    NoRate: a missing rate for an open night (a data error; seed tests prevent it).
    """
    changes: set[OfferChange] = set()

    stay = request.stay
    problem = check_stay(hotel, stay)
    if isinstance(problem, Closed):
        part = open_part(hotel, stay)
        if part is None:
            return problem
        stay = part
        changes.add(OfferChange.OPEN_PART)
        problem = check_stay(hotel, stay)  # D34: the open part is checked like any stay
    if isinstance(problem, MinStay):
        short = problem
        stay = min_stay_version(hotel, stay)
        changes.add(OfferChange.MIN_STAY)
        problem = check_stay(hotel, stay)  # and so is its min-stay version
        if isinstance(problem, Closed):
            # D57: say both facts; "closed" alone is untrue for the nights asked.
            return MinStayIntoClosed(short.min_nights, problem.nights)
    if problem is not None:
        # NO_CALENDAR. MinStay cannot be left: the min-stay version has exactly the
        # minimum length for its arrival.
        if isinstance(problem, MinStay):
            raise AssertionError(f"min-stay version {stay} still too short: {problem}")
        return problem

    too_many_rooms = group(request.rooms)
    if too_many_rooms is not None:
        return too_many_rooms

    plan = _room_plan(hotel, request, party, accessibility_need)
    if isinstance(plan, NeedsEscalation):
        return plan
    if request.room_type_id is not None and plan.room_type.id != request.room_type_id:
        changes.add(OfferChange.OTHER_TYPE)
    if plan.rooms > request.rooms:
        changes.add(OfferChange.MORE_ROOMS)

    quote = price_stay(hotel, plan.room_type.id, stay, party, plan.rooms)
    if isinstance(quote, OverCapacity):
        # _room_plan only returns plans the party fits; this would be a bug.
        raise AssertionError(f"plan {plan} does not fit the party: {quote}")
    if isinstance(quote, NoRate | NeedsEscalation):
        return quote
    return Offer(quote, frozenset(changes))


def _room_plan(
    hotel: HotelData, request: RoomRequest, party: Party, accessibility_need: bool
) -> RoomPlan | NeedsEscalation:
    """Which room type and how many rooms to price."""
    if accessibility_need:
        # D70: an adult in every room asked for (D32) is checked before the need, as
        # the router does; both escalate, this keeps the reason code the same.
        if not adults_cover_rooms(party, request.rooms):
            return NeedsEscalation(
                ReasonCode.UNACCOMPANIED_CHILDREN,
                f"{adult_count(party)} adult(s) for {request.rooms} room(s)",
            )
        # D33: only the Accessible room, one room; a 2-room request cannot be met by
        # the one Accessible room, so it is escalated rather than substituted.
        if request.rooms > 1:
            return NeedsEscalation(
                ReasonCode.ACCESSIBILITY,
                f"{request.rooms} rooms with an accessibility need; one accessible room",
            )
        return smallest_fitting_plan(hotel, party, accessibility_need=True)

    if request.room_type_id is not None:
        asked = hotel.room_types.get(request.room_type_id)
        if asked is None:
            raise ValueError(f"unknown room type {request.room_type_id!r}")  # a bug
        if fits(asked, party, request.rooms):
            return RoomPlan(asked, request.rooms)
        return smallest_fitting_plan(hotel, party)  # over capacity (D27)

    # No type named: the smallest type that fits in the number of rooms asked (the D27
    # ordering, applied to the guest's own room count); if none, the D27 plan.
    for room_type in sorted(
        (t for t in hotel.room_types.values() if not t.offer_only_if_needed),
        key=lambda t: (t.max_occupancy, t.id),
    ):
        if fits(room_type, party, request.rooms):
            return RoomPlan(room_type, request.rooms)
    return smallest_fitting_plan(hotel, party)
