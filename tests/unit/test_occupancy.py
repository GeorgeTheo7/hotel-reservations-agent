import pytest

from anemos.db.seed import hotel_data
from anemos.reservations.errors import NeedsEscalation, ReasonCode
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import Adult, Child, ExtraBedBand, Party, RoomType
from anemos.reservations.occupancy import (
    RoomPlan,
    adults_cover_rooms,
    extra_bed_band,
    extra_bed_payers,
    fits,
    head_count,
    smallest_fitting_plan,
)

pytestmark = pytest.mark.unit

HOTEL: HotelData = hotel_data()
STANDARD = HOTEL.room_types["standard"]
SEA_VIEW = HOTEL.room_types["sea_view"]
SUITE = HOTEL.room_types["family_suite"]
ACCESSIBLE = HOTEL.room_types["accessible"]


# --- head count (D13) ---


@pytest.mark.parametrize(
    ("party", "expected"),
    [
        (Party.of(2), 2),
        (Party.of(2, (1,)), 2),  # Ex 5: infant aged 1 not counted
        (Party.of(2, (0, 1)), 2),
        (Party.of(2, (2,)), 3),  # aged 2 counts
    ],
)
def test_head_count_leaves_out_infants(party: Party, expected: int) -> None:
    assert head_count(party) == expected


# --- fits ---


@pytest.mark.parametrize(
    ("room_type", "party", "rooms", "expected"),
    [
        (STANDARD, Party.of(2, (1,)), 1, True),  # Ex 5
        (SEA_VIEW, Party.of(4), 1, False),  # Ex 8: OVER_CAPACITY
        (SUITE, Party.of(4), 1, True),  # Ex 8 alternative
        (STANDARD, Party.of(4), 2, True),  # Ex 11
        (SEA_VIEW, Party.of(2, (16, 12, 4)), 2, True),  # Ex 12
        (SEA_VIEW, Party.of(7), 2, False),
    ],
)
def test_fits(room_type: RoomType, party: Party, rooms: int, expected: bool) -> None:
    assert fits(room_type, party, rooms) is expected


def test_fits_rejects_zero_rooms() -> None:
    with pytest.raises(ValueError, match="rooms"):
        fits(STANDARD, Party.of(1), 0)


# --- extra-bed payers (D25) ---


@pytest.mark.parametrize(
    ("example", "room_type", "party", "rooms", "payers"),
    [
        ("ex02", SEA_VIEW, Party.of(2, (8,)), 1, (Child(8),)),
        ("ex05", STANDARD, Party.of(2, (1,)), 1, ()),
        ("ex06", SUITE, Party.of(2, (13, 6)), 1, (Child(13), Child(6))),
        ("ex08", SUITE, Party.of(4), 1, (Adult(), Adult())),
        ("ex09", SEA_VIEW, Party.of(1, (13, 8)), 1, (Child(8),)),
        ("ex11", STANDARD, Party.of(4), 2, ()),
        ("ex12", SEA_VIEW, Party.of(2, (16, 12, 4)), 2, (Child(4),)),
    ],
)
def test_extra_bed_payers_examples(
    example: str, room_type: RoomType, party: Party, rooms: int, payers: tuple[object, ...]
) -> None:
    assert extra_bed_payers(room_type, party, rooms) == payers


def test_ex12_bands_whole_party_not_room_by_room() -> None:
    # Ex 12: only the 4-year-old pays, at the child rate. Room by room (adult + 16 + 12
    # in one room) would wrongly make the 12-year-old pay the 12+ rate.
    payers = extra_bed_payers(SEA_VIEW, Party.of(2, (16, 12, 4)), rooms=2)
    assert [extra_bed_band(g) for g in payers] == [ExtraBedBand.CHILD_2_11]


def test_payers_do_not_depend_on_listing_order() -> None:
    children_first = Party((Child(4), Child(12), Adult(), Child(16), Adult()))
    assert extra_bed_payers(SEA_VIEW, children_first, rooms=2) == (Child(4),)


def test_payers_require_a_fit() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        extra_bed_payers(SEA_VIEW, Party.of(4))


@pytest.mark.parametrize(
    ("guest", "band"),
    [
        (Child(2), ExtraBedBand.CHILD_2_11),
        (Child(11), ExtraBedBand.CHILD_2_11),
        (Child(12), ExtraBedBand.AGE_12_PLUS),
        (Child(17), ExtraBedBand.AGE_12_PLUS),
        (Adult(), ExtraBedBand.AGE_12_PLUS),
    ],
)
def test_extra_bed_band_age_edges(guest: Adult | Child, band: ExtraBedBand) -> None:
    assert extra_bed_band(guest) == band


def test_infant_has_no_extra_bed_band() -> None:
    with pytest.raises(ValueError, match="cot"):
        extra_bed_band(Child(1))


# --- smallest fitting plan (D27) ---


@pytest.mark.parametrize(
    ("party", "plan"),
    [
        (Party.of(1), RoomPlan(STANDARD, 1)),
        (Party.of(2, (0, 1)), RoomPlan(STANDARD, 1)),  # infants don't count
        (Party.of(3), RoomPlan(SEA_VIEW, 1)),
        (Party.of(4), RoomPlan(SUITE, 1)),  # Ex 8: one Suite, not 2 Standards
        (Party.of(2, (16, 12, 4)), RoomPlan(SEA_VIEW, 2)),  # Ex 12: no single type fits 5
        (Party.of(6), RoomPlan(SEA_VIEW, 2)),
        (Party.of(7), RoomPlan(SUITE, 2)),
        (Party.of(8), RoomPlan(SUITE, 2)),
    ],
)
def test_smallest_fitting_plan(party: Party, plan: RoomPlan) -> None:
    assert smallest_fitting_plan(HOTEL, party) == plan


def test_two_room_plans_use_one_type() -> None:
    # 6 people: 2 x Sea View (same type), never Suite + Standard (D27, mixed types: Later).
    plan = smallest_fitting_plan(HOTEL, Party.of(6))
    assert plan == RoomPlan(SEA_VIEW, 2)


def reason(result: RoomPlan | NeedsEscalation) -> ReasonCode | None:
    return result.reason if isinstance(result, NeedsEscalation) else None


def test_no_fit_in_two_rooms_is_group() -> None:
    assert reason(smallest_fitting_plan(HOTEL, Party.of(9))) == ReasonCode.GROUP


def test_accessible_room_only_with_stated_need() -> None:
    assert smallest_fitting_plan(HOTEL, Party.of(2)) == RoomPlan(STANDARD, 1)
    assert smallest_fitting_plan(HOTEL, Party.of(2), accessibility_need=True) == RoomPlan(
        ACCESSIBLE, 1
    )
    # Infants don't count, so 2 adults + infant still fit the Accessible room.
    assert smallest_fitting_plan(HOTEL, Party.of(2, (1,)), accessibility_need=True) == RoomPlan(
        ACCESSIBLE, 1
    )


@pytest.mark.parametrize("party", [Party.of(3), Party.of(2, (5,)), Party.of(5)])
def test_accessibility_need_that_does_not_fit_escalates(party: Party) -> None:
    # D33: no "next size up" substitute; the owner decides.
    result = smallest_fitting_plan(HOTEL, party, accessibility_need=True)
    assert reason(result) == ReasonCode.ACCESSIBILITY


@pytest.mark.parametrize(
    ("party", "accessibility_need"),
    [
        (Party.of(0, (15,)), False),  # children only, 1 room
        (Party.of(0, (15, 12)), True),  # children only, Accessible room
        (Party.of(1, (16, 12, 8, 4)), False),  # 5 people -> 2 rooms, 1 adult
    ],
)
def test_fewer_adults_than_rooms_escalates(party: Party, accessibility_need: bool) -> None:
    result = smallest_fitting_plan(HOTEL, party, accessibility_need=accessibility_need)
    assert reason(result) == ReasonCode.UNACCOMPANIED_CHILDREN


def test_one_adult_per_room_is_enough() -> None:
    # Ex 12 has 2 adults for 2 rooms: allowed.
    assert smallest_fitting_plan(HOTEL, Party.of(2, (16, 12, 4))) == RoomPlan(SEA_VIEW, 2)
    assert adults_cover_rooms(Party.of(2, (16, 12, 4)), 2)
    assert not adults_cover_rooms(Party.of(1, (16,)), 2)


def test_group_takes_precedence_over_unaccompanied_children() -> None:
    assert reason(smallest_fitting_plan(HOTEL, Party.of(0, (5,) * 9))) == ReasonCode.GROUP
