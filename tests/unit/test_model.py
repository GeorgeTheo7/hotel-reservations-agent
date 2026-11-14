from datetime import date, datetime

import pytest

from anemos.reservations.errors import InvalidDates, NeedsInfo
from anemos.reservations.model import (
    Adult,
    Child,
    Party,
    RoomType,
    Stay,
    party_from_counts,
    stay_or_error,
)

pytestmark = pytest.mark.unit


def test_stay_nights_exclude_check_out_day() -> None:
    # Ex 2: 12-16 July = nights of 12, 13, 14, 15 July.
    stay = Stay(date(2027, 7, 12), date(2027, 7, 16))
    assert stay.night_count == 4
    assert stay.nights == (
        date(2027, 7, 12),
        date(2027, 7, 13),
        date(2027, 7, 14),
        date(2027, 7, 15),
    )


def test_one_night_stay() -> None:
    # Ex 1: 20-21 May = one night.
    assert Stay(date(2027, 5, 20), date(2027, 5, 21)).nights == (date(2027, 5, 20),)


def test_stay_across_month_end() -> None:
    # Ex 3: 29 May - 3 Jun = 5 nights.
    assert Stay(date(2027, 5, 29), date(2027, 6, 3)).night_count == 5


@pytest.mark.parametrize(
    ("check_in", "check_out"),
    [(date(2027, 5, 20), date(2027, 5, 20)), (date(2027, 5, 21), date(2027, 5, 20))],
)
def test_stay_rejects_zero_or_negative_nights(check_in: date, check_out: date) -> None:
    with pytest.raises(ValueError, match="not after check-in"):
        Stay(check_in, check_out)


def test_stay_rejects_datetimes() -> None:
    noon = datetime(2027, 5, 20, 12, 0)  # noqa: DTZ001 - type check only
    with pytest.raises(TypeError, match="must be a date"):
        Stay(noon, date(2027, 5, 21))


def test_stay_or_error_returns_typed_errors() -> None:
    same_day = stay_or_error(date(2027, 5, 20), date(2027, 5, 20))
    backwards = stay_or_error(date(2027, 5, 21), date(2027, 5, 20))
    assert isinstance(same_day, InvalidDates)
    assert isinstance(backwards, InvalidDates)
    assert same_day.code == "INVALID_DATES"
    assert stay_or_error(date(2027, 5, 20), date(2027, 5, 21)) == Stay(
        date(2027, 5, 20), date(2027, 5, 21)
    )


def test_party_of_builds_adults_then_children() -> None:
    assert Party.of(2, (13, 6)).guests == (Adult(), Adult(), Child(13), Child(6))


def test_party_needs_a_guest() -> None:
    with pytest.raises(ValueError, match="at least one guest"):
        Party(())


@pytest.mark.parametrize("age", [0, 1, 2, 11, 12, 17])
def test_child_accepts_ages_0_to_17(age: int) -> None:
    assert Child(age).age == age


@pytest.mark.parametrize("age", [-1, 18, True, 8.0])
def test_child_rejects_bad_ages(age: object) -> None:
    with pytest.raises(ValueError, match="child age"):
        Child(age)  # type: ignore[arg-type]


def test_room_type_rejects_standard_above_max() -> None:
    with pytest.raises(ValueError, match="standard"):
        RoomType("x", "X", "Δοκιμή", 3, 2, "cot_only")


# --- party_from_counts: a stored request's counts -> Party, or what to ask -----------


def test_party_from_complete_counts() -> None:
    assert party_from_counts(2, 2, [8, 1]) == Party.of(2, (8, 1))


def test_children_not_mentioned_means_none() -> None:
    assert party_from_counts(2, None, []) == Party.of(2)


@pytest.mark.parametrize(("children", "ages"), [(2, []), (2, [8])])
def test_children_without_ages_needs_info(children: int, ages: list[int]) -> None:
    """Spec Unmet requests: children without ages -> ask."""
    assert party_from_counts(2, children, ages) == NeedsInfo("children's ages")


def test_adults_unknown_needs_info() -> None:
    assert party_from_counts(None, 1, [5]) == NeedsInfo("number of adults")


def test_nobody_needs_info() -> None:
    assert party_from_counts(0, 0, []) == NeedsInfo("number of guests")


def test_children_only_party_is_built() -> None:
    """D32 (an adult in every room) is a pricing rule, not a missing fact."""
    assert party_from_counts(0, 2, [15, 13]) == Party.of(0, (15, 13))


def test_more_ages_than_children_is_a_bug() -> None:
    with pytest.raises(ValueError, match="2 ages for 1 children"):
        party_from_counts(2, 1, [4, 6])
