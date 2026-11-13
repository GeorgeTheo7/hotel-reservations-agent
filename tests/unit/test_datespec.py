from datetime import UTC, date, datetime

import pytest

from anemos.clock import FixedClock
from anemos.db.seed import hotel_data
from anemos.reservations.datespec import (
    ExactDates,
    HolidayDates,
    PartialDate,
    RelativeDates,
    Window,
    resolve,
)
from anemos.reservations.errors import Closed, InvalidDates, NeedsInfo
from anemos.reservations.hotel_time import hotel_date
from anemos.reservations.model import Stay
from anemos.reservations.stay_rules import check_stay, short_notice

pytestmark = pytest.mark.unit


def received(year: int, month: int, day: int, hour: int = 9, minute: int = 0) -> datetime:
    """received_at as the app passes it: UTC, from the injected clock."""
    return FixedClock(datetime(year, month, day, hour, minute, tzinfo=UTC)).now()


def pd(day: int, month: int, year: int | None = None) -> PartialDate:
    return PartialDate(day, month, year)


# --- hotel date of received_at (D23) ---


def test_hotel_date_is_athens_not_utc() -> None:
    # Winter: Athens is UTC+2. 21:30 UTC = 23:30 Athens (same day); 22:30 UTC = next day.
    assert hotel_date(received(2026, 12, 31, 21, 30)) == date(2026, 12, 31)
    assert hotel_date(received(2026, 12, 31, 22, 30)) == date(2027, 1, 1)


# --- year inference (D21, D30) ---


def test_date_already_passed_resolves_to_next_year() -> None:
    # Eval trap: "20 October" received 20 Nov 2026 -> 2027 (the seeded season).
    stay = resolve(ExactDates(pd(20, 10), nights=3), received(2026, 11, 20))
    assert stay == Stay(date(2027, 10, 20), date(2027, 10, 23))


def test_today_counts_as_future() -> None:
    stay = resolve(ExactDates(pd(13, 11), nights=1), received(2026, 11, 13))
    assert stay == Stay(date(2026, 11, 13), date(2026, 11, 14))


def test_yesterday_moves_to_next_year() -> None:
    stay = resolve(ExactDates(pd(12, 11), nights=1), received(2026, 11, 13))
    assert stay == Stay(date(2027, 11, 12), date(2027, 11, 13))


def test_new_year_in_athens_changes_the_inferred_year() -> None:
    spec = ExactDates(pd(31, 12), nights=1)
    # Still 31 Dec in Athens: today counts.
    assert resolve(spec, received(2026, 12, 31, 21, 30)) == Stay(
        date(2026, 12, 31), date(2027, 1, 1)
    )
    # Already 1 Jan in Athens: 31 Dec is a year away.
    assert resolve(spec, received(2026, 12, 31, 22, 30)) == Stay(
        date(2027, 12, 31), date(2028, 1, 1)
    )


def test_explicit_year_is_kept() -> None:
    stay = resolve(ExactDates(pd(20, 10, 2027), nights=2), received(2026, 11, 20))
    assert stay == Stay(date(2027, 10, 20), date(2027, 10, 22))


def test_explicit_past_year_is_kept_for_short_notice_to_catch() -> None:
    stay = resolve(ExactDates(pd(20, 10, 2026), nights=2), received(2026, 11, 20))
    assert stay == Stay(date(2026, 10, 20), date(2026, 10, 22))


# --- length of stay ---


def test_nights_give_the_check_out() -> None:
    # Ex 2 dates: 12 July for 4 nights -> 12-16 July.
    stay = resolve(ExactDates(pd(12, 7), nights=4), received(2026, 12, 1))
    assert stay == Stay(date(2027, 7, 12), date(2027, 7, 16))


def test_check_out_without_year_crosses_new_year() -> None:
    stay = resolve(ExactDates(pd(28, 12), pd(3, 1)), received(2026, 11, 20))
    assert stay == Stay(date(2026, 12, 28), date(2027, 1, 3))


def test_check_out_and_nights_that_agree() -> None:
    stay = resolve(ExactDates(pd(20, 5), pd(24, 5), nights=4), received(2026, 12, 1))
    assert stay == Stay(date(2027, 5, 20), date(2027, 5, 24))


def test_check_out_and_nights_that_disagree_ask_the_guest() -> None:
    result = resolve(ExactDates(pd(20, 5), pd(24, 5), nights=3), received(2026, 12, 1))
    assert isinstance(result, NeedsInfo)
    assert "disagree" in result.missing


def test_no_length_asks_the_guest() -> None:
    result = resolve(ExactDates(pd(20, 5)), received(2026, 12, 1))
    assert isinstance(result, NeedsInfo)
    assert result.code == "NEEDS_INFO"


# --- invalid dates ---


@pytest.mark.parametrize(
    "spec",
    [
        ExactDates(pd(31, 4), nights=2),  # 31 April never exists
        ExactDates(pd(29, 2, 2027), nights=2),  # 2027 is not a leap year
        ExactDates(pd(20, 5), nights=0),
        ExactDates(pd(24, 5, 2027), pd(20, 5, 2027)),  # check-out before check-in
        ExactDates(pd(20, 5), pd(20, 5)),  # same day: 0 nights
    ],
)
def test_invalid_dates(spec: ExactDates) -> None:
    assert isinstance(resolve(spec, received(2026, 12, 1)), InvalidDates)


def test_29_february_without_year_is_the_next_leap_day() -> None:
    stay = resolve(ExactDates(pd(29, 2), nights=1), received(2026, 12, 1))
    assert stay == Stay(date(2028, 2, 29), date(2028, 3, 1))


# --- ambiguous day/month order (D37) ---


def test_ambiguous_03_04_asks_the_guest() -> None:
    spec = ExactDates(PartialDate(3, 4, ambiguous_order=True), nights=2)
    result = resolve(spec, received(2026, 12, 1))
    assert isinstance(result, NeedsInfo)
    assert "3 April or 4 March" in result.missing


def test_ambiguous_check_out_also_asks() -> None:
    spec = ExactDates(pd(1, 5), PartialDate(5, 6, ambiguous_order=True))
    assert isinstance(resolve(spec, received(2026, 12, 1)), NeedsInfo)


@pytest.mark.parametrize(
    ("day", "month", "expected_check_in"),
    [
        (13, 4, date(2027, 4, 13)),  # 13 can't be a month: one reading only
        (4, 4, date(2027, 4, 4)),  # same date both ways
    ],
)
def test_flag_ignored_when_only_one_reading_exists(
    day: int, month: int, expected_check_in: date
) -> None:
    spec = ExactDates(PartialDate(day, month, ambiguous_order=True), nights=1)
    result = resolve(spec, received(2026, 12, 1))
    assert isinstance(result, Stay)
    assert result.check_in == expected_check_in


def test_unflagged_numbers_are_taken_as_read() -> None:
    assert resolve(ExactDates(pd(3, 4), nights=1), received(2026, 12, 1)) == Stay(
        date(2027, 4, 3), date(2027, 4, 4)
    )


@pytest.mark.parametrize(("day", "month"), [(0, 5), (32, 5), (10, 0), (10, 13)])
def test_partial_date_rejects_impossible_numbers(day: int, month: int) -> None:
    with pytest.raises(ValueError):
        PartialDate(day, month)


def test_naive_received_at_is_refused() -> None:
    naive = datetime(2026, 12, 1, 9, 0)  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve(ExactDates(pd(20, 5), nights=1), naive)


# --- relative: weekends (D29) ---
# Week of Mon 7 Jun 2027: this Friday = 11 Jun, the Friday after = 18 Jun.

THIS_WEEKEND = Stay(date(2027, 6, 11), date(2027, 6, 13))
WEEKEND_AFTER = Stay(date(2027, 6, 18), date(2027, 6, 20))


@pytest.mark.parametrize(
    ("day", "weekday", "next_weekend"),
    [
        (7, "Mon", THIS_WEEKEND),
        (8, "Tue", THIS_WEEKEND),
        (9, "Wed", THIS_WEEKEND),
        (10, "Thu", THIS_WEEKEND),
        (11, "Fri", WEEKEND_AFTER),
        (12, "Sat", WEEKEND_AFTER),
        (13, "Sun", WEEKEND_AFTER),
    ],
)
def test_next_weekend_for_every_weekday(day: int, weekday: str, next_weekend: Stay) -> None:
    at = received(2027, 6, day, 6)  # 09:00 Athens
    assert hotel_date(at).strftime("%a") == weekday
    assert resolve(RelativeDates("next_weekend"), at) == next_weekend


@pytest.mark.parametrize("day", range(7, 14))
def test_this_weekend_is_the_friday_of_the_current_week(day: int) -> None:
    assert resolve(RelativeDates("this_weekend"), received(2027, 6, day, 6)) == THIS_WEEKEND


@pytest.mark.parametrize("day", [11, 12, 13])
def test_this_weekend_received_fri_to_sun_is_short_notice(day: int) -> None:
    at = received(2027, 6, day, 6)
    stay = resolve(RelativeDates("this_weekend"), at)
    assert isinstance(stay, Stay)
    assert short_notice(stay.check_in, at) is not None


def test_weekend_uses_the_athens_date() -> None:
    # Sun 13 Jun 20:30 UTC = 23:30 Sunday in Athens; 21:30 UTC = 00:30 Monday.
    assert resolve(RelativeDates("this_weekend"), received(2027, 6, 13, 20, 30)) == THIS_WEEKEND
    assert resolve(RelativeDates("this_weekend"), received(2027, 6, 13, 21, 30)) == WEEKEND_AFTER


def test_weekend_with_2_nights_stated_is_fine() -> None:
    assert resolve(RelativeDates("next_weekend", nights=2), received(2027, 6, 7)) == THIS_WEEKEND


@pytest.mark.parametrize("nights", [1, 3])
def test_weekend_with_other_night_count_asks(nights: int) -> None:
    # 1 night covers the Greek "Saturday night only" reading: ask, don't guess.
    result = resolve(RelativeDates("next_weekend", nights=nights), received(2027, 6, 7))
    assert isinstance(result, NeedsInfo)


# --- holiday: Easter (D28) ---


def test_plain_easter_is_orthodox_window() -> None:
    # Eval trap: "Easter" from an English guest, received Nov 2026 -> Orthodox 2 May 2027.
    result = resolve(HolidayDates("Easter", nights=4), received(2026, 11, 20))
    assert result == Window(date(2027, 4, 29), date(2027, 5, 5), 4)


def test_orthodox_easter_named_explicitly() -> None:
    result = resolve(HolidayDates("easter", "orthodox", nights=3), received(2026, 11, 20))
    assert result == Window(date(2027, 4, 29), date(2027, 5, 5), 3)


def test_western_easter_2027_window_is_before_opening() -> None:
    # Eval trap: "Catholic Easter" -> 28 Mar 2027, before the 15 Apr opening -> CLOSED.
    result = resolve(HolidayDates("Easter", "western", nights=3), received(2026, 11, 20))
    assert result == Window(date(2027, 3, 25), date(2027, 3, 31), 3)
    on_sunday = Stay(date(2027, 3, 28), date(2027, 3, 31))
    assert isinstance(check_stay(hotel_data(), on_sunday), Closed)


def test_easter_without_nights_asks() -> None:
    result = resolve(HolidayDates("Easter"), received(2026, 11, 20))
    assert isinstance(result, NeedsInfo)


def test_unknown_holiday_asks_for_dates() -> None:
    result = resolve(HolidayDates("Christmas", nights=3), received(2026, 11, 20))
    assert isinstance(result, NeedsInfo)
    assert "Christmas" in result.missing


def test_window_starts_today_when_received_inside_it() -> None:
    # Received on Orthodox Easter Monday 2027 (3 May): earlier check-ins are gone.
    result = resolve(HolidayDates("Easter", nights=2), received(2027, 5, 3))
    assert result == Window(date(2027, 5, 3), date(2027, 5, 5), 2)


def test_window_over_moves_to_next_year() -> None:
    # Received 6 May 2027, after the window: Easter 2028 (16 Apr) -> NO_CALENDAR later.
    result = resolve(HolidayDates("Easter", nights=2), received(2027, 5, 6))
    assert result == Window(date(2028, 4, 13), date(2028, 4, 19), 2)


def test_explicit_easter_year() -> None:
    result = resolve(HolidayDates("Easter", year=2028, nights=2), received(2026, 11, 20))
    assert result == Window(date(2028, 4, 13), date(2028, 4, 19), 2)


@pytest.mark.parametrize(
    "spec", [HolidayDates("Easter", year=2150, nights=2), HolidayDates("Easter", nights=0)]
)
def test_invalid_holiday_requests(spec: HolidayDates) -> None:
    assert isinstance(resolve(spec, received(2026, 11, 20)), InvalidDates)
