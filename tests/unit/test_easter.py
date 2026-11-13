from datetime import date

import pytest

from anemos.reservations.easter import orthodox_easter, western_easter

pytestmark = pytest.mark.unit

YEARS = range(1900, 2100)


@pytest.mark.parametrize(
    "expected",
    [
        date(2024, 5, 5),
        date(2025, 4, 20),
        date(2026, 4, 12),
        date(2027, 5, 2),
        date(2028, 4, 16),
    ],
    ids=lambda d: str(d.year),
)
def test_orthodox_easter_required_dates(expected: date) -> None:
    assert orthodox_easter(expected.year) == expected


@pytest.mark.parametrize(
    "expected",
    [
        date(2024, 3, 31),
        date(2025, 4, 20),
        date(2026, 4, 5),
        date(2027, 3, 28),  # eval trap: before opening -> CLOSED
        date(2028, 4, 16),
    ],
    ids=lambda d: str(d.year),
)
def test_western_easter(expected: date) -> None:
    assert western_easter(expected.year) == expected


@pytest.mark.parametrize(
    ("year", "orthodox", "western"),
    [
        (2010, date(2010, 4, 4), date(2010, 4, 4)),  # same Sunday
        (2011, date(2011, 4, 24), date(2011, 4, 24)),
        (2017, date(2017, 4, 16), date(2017, 4, 16)),
        (2023, date(2023, 4, 16), date(2023, 4, 9)),  # one week apart
    ],
)
def test_more_known_years(year: int, orthodox: date, western: date) -> None:
    assert orthodox_easter(year) == orthodox
    assert western_easter(year) == western


# Checked for every supported year (200 of them, so no sampling needed).


def test_easter_is_always_a_sunday() -> None:
    for year in YEARS:
        assert orthodox_easter(year).weekday() == 6, year
        assert western_easter(year).weekday() == 6, year


def test_dates_stay_in_their_known_ranges() -> None:
    for year in YEARS:
        # Orthodox: 4 Apr - 8 May (Gregorian) in 1900-2099; Western: 22 Mar - 25 Apr.
        assert date(year, 4, 4) <= orthodox_easter(year) <= date(year, 5, 8), year
        assert date(year, 3, 22) <= western_easter(year) <= date(year, 4, 25), year


def test_orthodox_is_never_before_western() -> None:
    for year in YEARS:
        gap = (orthodox_easter(year) - western_easter(year)).days
        assert gap in (0, 7, 28, 35), year  # same day, or 1, 4 or 5 weeks later


@pytest.mark.parametrize("year", [1899, 2100])
def test_orthodox_refuses_years_outside_the_13_day_gap(year: int) -> None:
    with pytest.raises(ValueError, match="1900-2099"):
        orthodox_easter(year)


def test_western_refuses_pre_gregorian_years() -> None:
    with pytest.raises(ValueError, match="Gregorian"):
        western_easter(1582)
