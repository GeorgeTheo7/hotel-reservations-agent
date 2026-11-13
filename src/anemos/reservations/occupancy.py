"""Who counts, who fits, who pays an extra bed, and which room type to offer.

D13: infants under 2 sleep in a free cot and do not count toward occupancy.
D25: extra-bed payers: sort counted guests oldest first; the first
     rooms x standard_occupancy fill the standard places, the rest pay an extra bed.
D27: over capacity: the smallest single room type that fits; only if none fits,
     2 rooms of the same, smallest type that fits.
D32: every room needs an adult: adults >= rooms, else UNACCOMPANIED_CHILDREN.
D33: a stated accessibility need is served only by the Accessible room; a party that
     does not fit it escalates as ACCESSIBILITY.
No prices here: this module says *who* pays and in which age band; pricing (step 5)
turns that into cents.
"""

from dataclasses import dataclass

from anemos.reservations.errors import NeedsEscalation, ReasonCode
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import Adult, Child, ExtraBedBand, Guest, Party, RoomType

INFANT_AGE_LIMIT = 2  # under 2 = infant (D13)
OLDER_BAND_FROM_AGE = 12  # extra bed: 2-11 child rate, 12+ older rate
MAX_ROOMS_PER_REQUEST = 2  # 3+ rooms escalate as GROUP (D31)


def is_infant(guest: Guest) -> bool:
    return isinstance(guest, Child) and guest.age < INFANT_AGE_LIMIT


def head_count(party: Party) -> int:
    """People who count toward occupancy: everyone except infants (D13)."""
    return sum(1 for guest in party.guests if not is_infant(guest))


def fits(room_type: RoomType, party: Party, rooms: int = 1) -> bool:
    """True if the party fits in `rooms` rooms of this type (by head count only)."""
    if rooms < 1:
        raise ValueError(f"rooms must be >= 1, got {rooms}")
    return head_count(party) <= rooms * room_type.max_occupancy


def extra_bed_payers(room_type: RoomType, party: Party, rooms: int = 1) -> tuple[Guest, ...]:
    """The guests who pay an extra bed (D25), youngest last.

    The ordering runs over the whole party, not room by room (pricing Ex 12).
    The party must fit: over capacity is the caller's job (OVER_CAPACITY / D27).
    """
    if not fits(room_type, party, rooms):
        raise ValueError(f"party of {head_count(party)} does not fit {rooms} x {room_type.id}")
    counted = [guest for guest in party.guests if not is_infant(guest)]
    # Adults first (no age is asked; they count as oldest), then children by age, oldest
    # first. sorted() is stable, so equal keys keep their order: the result is the same
    # whatever order the guests were listed in.
    oldest_first = sorted(counted, key=_age_order)
    standard_places = rooms * room_type.standard_occupancy
    return tuple(oldest_first[standard_places:])


def extra_bed_band(guest: Guest) -> ExtraBedBand:
    if is_infant(guest):
        raise ValueError("an infant sleeps in a free cot, not an extra bed (D13)")
    if isinstance(guest, Child) and guest.age < OLDER_BAND_FROM_AGE:
        return ExtraBedBand.CHILD_2_11
    return ExtraBedBand.AGE_12_PLUS


@dataclass(frozen=True)
class RoomPlan:
    room_type: RoomType
    rooms: int


def adult_count(party: Party) -> int:
    return sum(1 for guest in party.guests if isinstance(guest, Adult))


def adults_cover_rooms(party: Party, rooms: int) -> bool:
    """D32: every room needs at least one adult. Applies to any plan or request."""
    return adult_count(party) >= rooms


def smallest_fitting_plan(
    hotel: HotelData, party: Party, *, accessibility_need: bool = False
) -> RoomPlan | NeedsEscalation:
    """The room plan to offer for this party, or why the owner must handle it.

    Without an accessibility need (D27): one room of the smallest type that fits; else
    2 rooms of the same, smallest type that fits; else GROUP. "Smallest" = lowest max
    occupancy. Types flagged offer_only_if_needed (the Accessible room) are not
    candidates; offering it when nothing else is free is availability (week 3).
    With a stated need (D33): only flagged types, one room; no fit -> ACCESSIBILITY.
    In both cases the plan must have an adult per room (D32), else UNACCOMPANIED_CHILDREN.
    Availability is not considered here.
    """
    if accessibility_need:
        plan = _first_fit(hotel, party, flagged=True, max_rooms=1)
        if plan is None:
            return NeedsEscalation(
                ReasonCode.ACCESSIBILITY,
                f"accessibility need, party of {head_count(party)} does not fit the"
                " accessible room",
            )
    else:
        plan = _first_fit(hotel, party, flagged=False, max_rooms=MAX_ROOMS_PER_REQUEST)
        if plan is None:
            return NeedsEscalation(
                ReasonCode.GROUP,
                f"party of {head_count(party)} does not fit {MAX_ROOMS_PER_REQUEST} rooms",
            )
    if not adults_cover_rooms(party, plan.rooms):
        return NeedsEscalation(
            ReasonCode.UNACCOMPANIED_CHILDREN,
            f"{adult_count(party)} adult(s) for {plan.rooms} room(s)",
        )
    return plan


def _first_fit(hotel: HotelData, party: Party, *, flagged: bool, max_rooms: int) -> RoomPlan | None:
    """Fewest rooms first, then the smallest type; all rooms of one type (D27)."""
    candidates = sorted(
        (t for t in hotel.room_types.values() if t.offer_only_if_needed == flagged),
        # id is the last tie-break, so the result never depends on dict order.
        key=lambda t: (t.max_occupancy, t.id),
    )
    for rooms in range(1, max_rooms + 1):
        for room_type in candidates:
            if fits(room_type, party, rooms):
                return RoomPlan(room_type, rooms)
    return None


def _age_order(guest: Guest) -> tuple[int, int]:
    if isinstance(guest, Adult):
        return (0, 0)
    return (1, -guest.age)
