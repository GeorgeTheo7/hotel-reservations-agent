"""Price a stay: nightly lines, subtotals, city tax, deposit, cancellation deadline.

Pure and integer-only: every amount is int cents (D14). Each night is priced at its
own season. The calendar rules (CLOSED, MIN_STAY) are checked separately by
`stay_rules.check_stay`; pricing only refuses what it cannot price:
OVER_CAPACITY, NO_RATE, and the plan escalations GROUP (3+ rooms) and
UNACCOMPANIED_CHILDREN (D32).
"""

from dataclasses import dataclass
from datetime import date

from anemos.reservations.errors import NeedsEscalation, NoRate, OverCapacity, ReasonCode
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import ExtraBedBand, Party, Season, Stay
from anemos.reservations.money import Cents, deposit_cents
from anemos.reservations.occupancy import (
    adults_cover_rooms,
    extra_bed_band,
    extra_bed_payers,
    fits,
    head_count,
)
from anemos.reservations.seasons import season_of
from anemos.reservations.stay_rules import cancellation_deadline, group


@dataclass(frozen=True)
class NightLine:
    """One night of the stay, all rooms together."""

    night: date
    season: Season
    room_rate_cents: Cents  # per room
    room_cents: Cents  # room_rate x rooms
    extra_bed_cents: Cents  # all extra beds this night
    city_tax_cents: Cents  # city tax per room x rooms


@dataclass(frozen=True)
class Quote:
    room_type_id: str
    rooms: int
    stay: Stay
    party: Party
    extra_bed_bands: tuple[ExtraBedBand, ...]  # one per paying guest, oldest first
    lines: tuple[NightLine, ...]
    room_subtotal_cents: Cents
    extra_bed_subtotal_cents: Cents
    accommodation_subtotal_cents: Cents  # rooms + extra beds (D24)
    city_tax_cents: Cents  # paid on arrival, not in the deposit
    deposit_cents: Cents  # ceil(30% of accommodation) to a whole euro (D14)
    free_cancellation_until: date  # last day to cancel free (D26)


def price_stay(
    hotel: HotelData, room_type_id: str, stay: Stay, party: Party, rooms: int = 1
) -> Quote | OverCapacity | NoRate | NeedsEscalation:
    room_type = hotel.room_types.get(room_type_id)
    if room_type is None:
        raise ValueError(f"unknown room type {room_type_id!r}")  # a bug, not a guest error
    if rooms < 1:
        raise ValueError(f"rooms must be >= 1, got {rooms}")
    too_many_rooms = group(rooms)
    if too_many_rooms is not None:
        return too_many_rooms
    if not fits(room_type, party, rooms):
        return OverCapacity(head_count(party), rooms * room_type.max_occupancy)
    if not adults_cover_rooms(party, rooms):
        return NeedsEscalation(
            ReasonCode.UNACCOMPANIED_CHILDREN, f"fewer adults than {rooms} room(s)"
        )

    bands = tuple(extra_bed_band(g) for g in extra_bed_payers(room_type, party, rooms))
    # Extra-bed prices are the same every night (one price for all seasons).
    extra_beds_per_night = sum(hotel.extra_bed_rates[band] for band in bands)

    lines: list[NightLine] = []
    unpriced: list[date] = []
    for night in stay.nights:
        season = season_of(hotel, night)
        rate = None if season is None else hotel.rate(room_type_id, season)
        if season is None or rate is None:
            unpriced.append(night)
            continue
        lines.append(
            NightLine(
                night=night,
                season=season,
                room_rate_cents=rate,
                room_cents=Cents(rate * rooms),
                extra_bed_cents=Cents(extra_beds_per_night),
                city_tax_cents=Cents(hotel.city_tax_cents[season] * rooms),
            )
        )
    if unpriced:
        return NoRate(tuple(unpriced))

    room_subtotal = Cents(sum(line.room_cents for line in lines))
    extra_bed_subtotal = Cents(sum(line.extra_bed_cents for line in lines))
    accommodation = Cents(room_subtotal + extra_bed_subtotal)
    return Quote(
        room_type_id=room_type_id,
        rooms=rooms,
        stay=stay,
        party=party,
        extra_bed_bands=bands,
        lines=tuple(lines),
        room_subtotal_cents=room_subtotal,
        extra_bed_subtotal_cents=extra_bed_subtotal,
        accommodation_subtotal_cents=accommodation,
        city_tax_cents=Cents(sum(line.city_tax_cents for line in lines)),
        deposit_cents=deposit_cents(accommodation),
        free_cancellation_until=cancellation_deadline(hotel, stay),
    )
