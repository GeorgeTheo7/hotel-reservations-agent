from dataclasses import replace
from datetime import date

import pytest

from anemos.db.seed import hotel_data
from anemos.reservations.hotel_data import HotelData, SeasonPeriod
from anemos.reservations.model import ExtraBedBand, Season, Stay
from anemos.reservations.seasons import is_open, season_of

pytestmark = pytest.mark.unit

LOW, MID, HIGH = Season.LOW, Season.MID, Season.HIGH


@pytest.fixture(scope="module")
def hotel() -> HotelData:
    return hotel_data()


@pytest.mark.parametrize(
    ("night", "expected"),
    [
        (date(2027, 4, 14), None),  # day before opening
        (date(2027, 4, 15), LOW),  # opening night
        (date(2027, 5, 31), LOW),
        (date(2027, 6, 1), MID),
        (date(2027, 7, 14), MID),
        (date(2027, 7, 15), HIGH),
        (date(2027, 8, 31), HIGH),
        (date(2027, 9, 1), MID),
        (date(2027, 9, 30), MID),
        (date(2027, 10, 1), LOW),
        (date(2027, 10, 31), LOW),  # last open night
        (date(2027, 11, 1), None),  # closed
        (date(2027, 3, 28), None),  # Western Easter 2027: closed (eval trap)
        (date(2028, 5, 1), None),  # 2028 is not seeded (D21)
    ],
)
def test_season_boundaries(hotel: HotelData, night: date, expected: Season | None) -> None:
    assert season_of(hotel, night) == expected
    assert is_open(hotel, night) == (expected is not None)


def test_each_night_has_its_own_season(hotel: HotelData) -> None:
    # Ex 2: Sea View 12-16 Jul: three Mid nights, then High.
    stay = Stay(date(2027, 7, 12), date(2027, 7, 16))
    assert [season_of(hotel, n) for n in stay.nights] == [MID, MID, MID, HIGH]


def test_snapshot_rejects_overlapping_season_periods(hotel: HotelData) -> None:
    clash = SeasonPeriod(HIGH, date(2027, 5, 30), date(2027, 6, 2))
    with pytest.raises(ValueError, match="overlapping season"):
        replace(hotel, season_periods=(*hotel.season_periods, clash))


def test_snapshot_rejects_float_rates(hotel: HotelData) -> None:
    rates = {**hotel.rates, ("standard", LOW): 70.0}
    with pytest.raises(ValueError, match="int cents"):
        replace(hotel, rates=rates)  # type: ignore[arg-type]


def test_snapshot_needs_a_price_for_every_extra_bed_band(hotel: HotelData) -> None:
    only_child = {ExtraBedBand.CHILD_2_11: hotel.extra_bed_rates[ExtraBedBand.CHILD_2_11]}
    with pytest.raises(ValueError, match="every band"):
        replace(hotel, extra_bed_rates=only_child)


def test_snapshot_cannot_be_changed(hotel: HotelData) -> None:
    with pytest.raises(TypeError):
        hotel.rates[("standard", LOW)] = 1  # type: ignore[index]


def test_rate_lookup(hotel: HotelData) -> None:
    assert hotel.rate("sea_view", HIGH) == 19_000
    assert hotel.rate("unknown", HIGH) is None
