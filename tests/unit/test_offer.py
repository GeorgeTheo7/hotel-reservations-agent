"""build_offer: the paths the 12 pricing examples do not reach.

The examples (test_pricing.py) cover: exact quotes, min-stay version (Ex 4), open part
(Ex 7), over capacity to a bigger type (Ex 8) and 2-room requests (Ex 11, 12).
"""

import dataclasses
from datetime import date

import pytest

from anemos.db.seed import hotel_data
from anemos.reservations.errors import (
    Closed,
    MinStayIntoClosed,
    NeedsEscalation,
    NoRate,
    ReasonCode,
)
from anemos.reservations.hotel_data import HotelData, MinStayRule
from anemos.reservations.model import Party, RoomRequest, Stay
from anemos.reservations.offer import Offer, OfferChange, build_offer

pytestmark = pytest.mark.unit

HOTEL = hotel_data()


def d(month: int, day: int, year: int = 2027) -> date:
    return date(year, month, day)


def offer(
    stay: Stay, party: Party, *, rooms: int = 1, room_type_id: str | None = None,
    hotel: HotelData = HOTEL, accessibility_need: bool = False,
) -> Offer | Closed | MinStayIntoClosed | NoRate | NeedsEscalation:  # fmt: skip
    return build_offer(
        hotel,
        RoomRequest(stay, rooms, room_type_id),
        party,
        accessibility_need=accessibility_need,
    )


def escalation(result: object) -> ReasonCode:
    assert isinstance(result, NeedsEscalation), result
    return result.reason


def with_min_stay(nights: int, arrival: date) -> HotelData:
    """The seed with the minimum stay for `arrival`'s rule changed to `nights`.

    The 2027 seed has a 1-night minimum at opening and closing, so the D34 two-step
    path (trim, then lengthen) cannot happen with it; this hotel makes it happen.
    """
    rules = tuple(
        MinStayRule(r.first_arrival, r.end, nights) if r.first_arrival <= arrival < r.end else r
        for r in HOTEL.min_stay_rules
    )
    return dataclasses.replace(HOTEL, min_stay_rules=rules)


# --- calendar -------------------------------------------------------------------------


def test_every_night_closed() -> None:
    result = offer(Stay(d(11, 5), d(11, 8)), Party.of(2), room_type_id="standard")
    assert isinstance(result, Closed)
    assert result.nights == (d(11, 5), d(11, 6), d(11, 7))


def test_year_without_calendar_escalates() -> None:
    """D36: NO_CALENDAR, not CLOSED, and not trimmed to an 'open part'."""
    result = offer(Stay(d(6, 10, 2028), d(6, 13, 2028)), Party.of(2), room_type_id="standard")
    assert escalation(result) is ReasonCode.NO_CALENDAR


def test_open_part_then_its_min_stay_version() -> None:
    """D34: 13-17 Apr, hotel opens 15 Apr -> open part 15-17 (2 nights); with a 3-night
    minimum that part is too short -> its min-stay version 15-18 Apr."""
    hotel = with_min_stay(3, d(4, 15))
    result = offer(Stay(d(4, 13), d(4, 17)), Party.of(2), room_type_id="standard", hotel=hotel)
    assert isinstance(result, Offer)
    assert result.quote.stay == Stay(d(4, 15), d(4, 18))
    assert result.changes == {OfferChange.OPEN_PART, OfferChange.MIN_STAY}
    assert result.hold_allowed is False


def test_min_stay_version_reaching_closed_nights_says_both() -> None:
    """D57. 30 Oct-2 Nov: open part 30-31 Oct (2 nights); with a 3-night minimum the
    min-stay version runs to 2 Nov again, into closed nights. No offer is invented, and
    the result says both facts: short of the minimum, and the minimum reaches closing."""
    hotel = with_min_stay(3, d(10, 30))
    result = offer(Stay(d(10, 30), d(11, 2)), Party.of(2), room_type_id="standard", hotel=hotel)
    assert result == MinStayIntoClosed(min_nights=3, closed_nights=(d(11, 1),))


def test_short_stay_whose_min_stay_version_reaches_closed_nights() -> None:
    """D57 without trimming: 31 Oct, 1 night asked, 3 required; 31 Oct-3 Nov is closed
    from 1 Nov. The nights asked are open, so plain CLOSED would be untrue."""
    hotel = with_min_stay(3, d(10, 31))
    result = offer(Stay(d(10, 31), d(11, 1)), Party.of(2), room_type_id="standard", hotel=hotel)
    assert result == MinStayIntoClosed(min_nights=3, closed_nights=(d(11, 1), d(11, 2)))


def test_fits_and_is_open_means_no_changes() -> None:
    result = offer(Stay(d(6, 1), d(6, 4)), Party.of(2), room_type_id="sea_view")
    assert isinstance(result, Offer)
    assert result.changes == frozenset()
    assert result.hold_allowed is True


# --- rooms ------------------------------------------------------------------------------

STAY = Stay(d(6, 1), d(6, 4))  # Mid, 3 nights, min 2


@pytest.mark.parametrize(
    ("party", "expected_type"),
    [(Party.of(2), "standard"), (Party.of(3), "sea_view"), (Party.of(2, (10, 5)), "family_suite")],
)
def test_no_type_named_gives_the_smallest_type_that_fits(party: Party, expected_type: str) -> None:
    """Any type is 'what was asked' when none was named, so the offer may be held."""
    result = offer(STAY, party)
    assert isinstance(result, Offer)
    assert (result.quote.room_type_id, result.quote.rooms) == (expected_type, 1)
    assert result.hold_allowed is True


def test_no_type_named_never_offers_the_accessible_room() -> None:
    """D33: without a stated need the plan skips the Accessible room (it is offered only
    when nothing else is free, which is availability)."""
    result = offer(STAY, Party.of(2))
    assert isinstance(result, Offer)
    assert result.quote.room_type_id != "accessible"


def test_no_type_named_two_rooms_asked() -> None:
    result = offer(STAY, Party.of(4), rooms=2)
    assert isinstance(result, Offer)
    assert (result.quote.room_type_id, result.quote.rooms) == ("standard", 2)
    assert result.hold_allowed is True


def test_party_too_big_for_one_room_gets_two() -> None:
    """D27: no single type fits 6 -> 2 rooms of the smallest type that fits."""
    result = offer(STAY, Party.of(6))
    assert isinstance(result, Offer)
    assert (result.quote.room_type_id, result.quote.rooms) == ("sea_view", 2)
    assert result.changes == {OfferChange.MORE_ROOMS}


def test_named_type_too_small_for_one_room_gets_other_type_and_more_rooms() -> None:
    result = offer(STAY, Party.of(6), room_type_id="standard")
    assert isinstance(result, Offer)
    assert result.changes == {OfferChange.OTHER_TYPE, OfferChange.MORE_ROOMS}


def test_three_rooms_is_a_group() -> None:
    assert escalation(offer(STAY, Party.of(6), rooms=3)) is ReasonCode.GROUP


def test_party_no_two_rooms_fit_is_a_group() -> None:
    assert escalation(offer(STAY, Party.of(9))) is ReasonCode.GROUP


def test_fewer_adults_than_rooms() -> None:
    """D32, checked by pricing for the chosen plan."""
    result = offer(STAY, Party.of(1, (15, 13)), rooms=2, room_type_id="standard")
    assert escalation(result) is ReasonCode.UNACCOMPANIED_CHILDREN


def test_group_is_checked_before_unaccompanied_children() -> None:
    """D32: no fit in 2 rooms (GROUP) wins over too few adults."""
    assert escalation(offer(STAY, Party.of(0, (10,) * 9))) is ReasonCode.GROUP


# --- accessibility need (D33) -------------------------------------------------------


def test_accessibility_need_gets_the_accessible_room() -> None:
    result = offer(STAY, Party.of(2), accessibility_need=True)
    assert isinstance(result, Offer)
    assert result.quote.room_type_id == "accessible"
    assert result.hold_allowed is True  # no type named: the accessible room is what fits


def test_accessibility_need_replaces_a_named_type() -> None:
    result = offer(STAY, Party.of(2), room_type_id="sea_view", accessibility_need=True)
    assert isinstance(result, Offer)
    assert result.quote.room_type_id == "accessible"
    assert result.changes == {OfferChange.OTHER_TYPE}


def test_accessibility_need_party_too_big_escalates() -> None:
    assert escalation(offer(STAY, Party.of(3), accessibility_need=True)) is ReasonCode.ACCESSIBILITY


def test_accessibility_need_with_two_rooms_escalates() -> None:
    result = offer(STAY, Party.of(2), rooms=2, accessibility_need=True)
    assert escalation(result) is ReasonCode.ACCESSIBILITY


def test_unknown_room_type_is_a_bug() -> None:
    with pytest.raises(ValueError, match="unknown room type"):
        offer(STAY, Party.of(2), room_type_id="penthouse")
