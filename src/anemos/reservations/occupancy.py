"""Who counts, who fits, who pays an extra bed, and which room type to offer.

D13: infants under 2 sleep in a free cot and do not count toward occupancy.
D25: extra-bed payers: sort counted guests oldest first; the first
     rooms x standard_occupancy fill the standard places, the rest pay an extra bed.
D27: over capacity: the smallest single room type that fits; only if none fits,
     2 rooms of the smallest type that fits.
No prices here: this module says *who* pays and in which age band; pricing (step 5)
turns that into cents.
"""

from dataclasses import dataclass

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


def smallest_fitting_plan(
    hotel: HotelData, party: Party, *, accessibility_need: bool = False
) -> RoomPlan | None:
    """D27: one room of the smallest type that fits; else 2 rooms of the smallest type
    that fits; else None (no fit in 2 rooms = GROUP escalation).

    "Smallest" = lowest max occupancy. Types flagged offer_only_if_needed (the
    Accessible room) are candidates only with a stated accessibility need, and then
    win a tie on size. Availability is not considered here (week 3 alternatives).
    """
    candidates = [
        room_type
        for room_type in hotel.room_types.values()
        if accessibility_need or not room_type.offer_only_if_needed
    ]
    candidates.sort(
        key=lambda t: (
            t.max_occupancy,
            not (accessibility_need and t.offer_only_if_needed),
            t.id,  # last tie-break, so the result never depends on dict order
        )
    )
    for rooms in range(1, MAX_ROOMS_PER_REQUEST + 1):
        for room_type in candidates:
            if fits(room_type, party, rooms):
                return RoomPlan(room_type, rooms)
    return None


def _age_order(guest: Guest) -> tuple[int, int]:
    if isinstance(guest, Adult):
        return (0, 0)
    return (1, -guest.age)
