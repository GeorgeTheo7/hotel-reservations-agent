import pytest

from anemos.db.seed import hotel_data
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import Adult, Child, Party, RoomType
from anemos.reservations.occupancy import (
    ExtraBedBand,
    RoomPlan,
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
        (Party.of(9), None),  # no fit in 2 rooms: GROUP
    ],
)
def test_smallest_fitting_plan(party: Party, plan: RoomPlan | None) -> None:
    assert smallest_fitting_plan(HOTEL, party) == plan


def test_accessible_room_only_with_stated_need() -> None:
    assert smallest_fitting_plan(HOTEL, Party.of(2)) == RoomPlan(STANDARD, 1)
    assert smallest_fitting_plan(HOTEL, Party.of(2), accessibility_need=True) == RoomPlan(
        ACCESSIBLE, 1
    )
    # Need stated, but the Accessible room is too small: next size up.
    assert smallest_fitting_plan(HOTEL, Party.of(3), accessibility_need=True) == RoomPlan(
        SEA_VIEW, 1
    )
