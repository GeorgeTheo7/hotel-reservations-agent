"""Calendar rules of a stay: closed nights, minimum stay, open part, cancellation
deadline, hold permission (D9) and the two request escalations SHORT_NOTICE and GROUP.

All pure: the calendar comes from HotelData, times are passed in (never "now").
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from anemos.reservations.errors import Closed, MinStay, NeedsEscalation, ReasonCode
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.hotel_time import check_in_moment, require_aware
from anemos.reservations.model import RoomOffer, RoomRequest, Season, Stay
from anemos.reservations.seasons import is_open, season_of

FREE_CANCELLATION_DAYS = 14
FREE_CANCELLATION_DAYS_HIGH = 30
SHORT_NOTICE = timedelta(hours=48)
GROUP_FROM_ROOMS = 3


def min_stay_for(hotel: HotelData, arrival: date) -> int | None:
    """Minimum nights for this arrival date; None if no rule covers it (closed)."""
    for rule in hotel.min_stay_rules:
        if rule.first_arrival <= arrival < rule.end:
            return rule.min_nights
    return None


def check_stay(hotel: HotelData, stay: Stay) -> Closed | MinStay | None:
    """None = the stay is bookable by the calendar. Closed nights are reported first."""
    closed = tuple(night for night in stay.nights if not is_open(hotel, night))
    if closed:
        return Closed(closed)
    min_nights = min_stay_for(hotel, stay.check_in)
    if min_nights is None:
        # Seed tests guarantee a rule for every open arrival date; this is a data bug.
        raise LookupError(f"no minimum-stay rule for open arrival date {stay.check_in}")
    if stay.night_count < min_nights:
        return MinStay(min_nights)
    return None


def open_part(hotel: HotelData, stay: Stay) -> Stay | None:
    """The longest run of open nights in the stay (Ex 7), or None if all are closed.

    A stay can only cross the season's start or end, so in practice there is one run.
    On a tie the earlier run wins. D34: the result is re-checked like any stay
    (check_stay): trimming can make it shorter than its minimum stay.
    """
    best: Stay | None = None
    run_start: date | None = None
    for night in (*stay.nights, stay.check_out):  # check_out closes the last run
        night_open = night != stay.check_out and is_open(hotel, night)
        if night_open and run_start is None:
            run_start = night
        elif not night_open and run_start is not None:
            run = Stay(run_start, night)
            if best is None or run.night_count > best.night_count:
                best = run
            run_start = None
    return best


def min_stay_version(hotel: HotelData, stay: Stay) -> Stay:
    """Same arrival, lengthened to the minimum stay if shorter (Ex 4: 10-13 -> 10-15 Aug)."""
    min_nights = min_stay_for(hotel, stay.check_in)
    if min_nights is None:
        raise ValueError(f"arrival {stay.check_in} is closed")
    if stay.night_count >= min_nights:
        return stay
    return Stay(stay.check_in, stay.check_in + timedelta(days=min_nights))


def cancellation_deadline(hotel: HotelData, stay: Stay) -> date:
    """Last day of free cancellation: 14 days before arrival, 30 if the *arrival* night
    is in High season (D26), even when later nights are High."""
    season = season_of(hotel, stay.check_in)
    if season is None:
        raise ValueError(f"arrival {stay.check_in} is closed")
    days = FREE_CANCELLATION_DAYS_HIGH if season is Season.HIGH else FREE_CANCELLATION_DAYS
    return stay.check_in - timedelta(days=days)


def hold_allowed(requested: RoomRequest, offers: Sequence[RoomOffer]) -> bool:
    """D9: hold only if the reply quotes exactly what the guest asked: one offer, same
    dates, same number of rooms, and the requested type (any type if none was named).
    Any substitution or choice of options -> no hold."""
    if len(offers) != 1:
        return False
    offer = offers[0]
    return (
        offer.stay == requested.stay
        and offer.rooms == requested.rooms
        and requested.room_type_id in (None, offer.room_type_id)
    )


def short_notice(arrival: date, received_at: datetime) -> NeedsEscalation | None:
    """D31: less than 48 real hours from received_at to 15:00 Athens on arrival day.

    Both times are converted to UTC before subtracting, so a DST change in between
    counts as real hours (see hotel_time). Exactly 48 h is not short notice.
    """
    require_aware(received_at, "received_at")
    lead = check_in_moment(arrival) - received_at.astimezone(UTC)
    if lead < SHORT_NOTICE:
        return NeedsEscalation(ReasonCode.SHORT_NOTICE, f"{lead} before check-in on {arrival}")
    return None


def group(rooms: int) -> NeedsEscalation | None:
    """D31: 3 or more rooms is a group for the owner."""
    if rooms >= GROUP_FROM_ROOMS:
        return NeedsEscalation(ReasonCode.GROUP, f"{rooms} rooms requested")
    return None
