from datetime import date, timedelta
from itertools import pairwise

import pytest

from anemos.db.seed import hotel_data
from anemos.reservations.datespec import Window
from anemos.reservations.errors import MinStay
from anemos.reservations.model import Stay
from anemos.reservations.options import pick_options, window_options
from anemos.reservations.stay_rules import check_stay

pytestmark = pytest.mark.unit

# --- picking options (D7) ---

HOTEL = hotel_data()


def calendar_ok(stay: Stay) -> bool:
    """Bookable by the calendar alone (availability joins in the tools)."""
    return check_stay(HOTEL, stay) is None


EASTER_2027 = Window(date(2027, 4, 29), date(2027, 5, 5), 4)


def test_easter_options_earliest_first_two_days_apart() -> None:
    assert pick_options(EASTER_2027, calendar_ok) == (
        Stay(date(2027, 4, 29), date(2027, 5, 3)),
        Stay(date(2027, 5, 1), date(2027, 5, 5)),
        Stay(date(2027, 5, 3), date(2027, 5, 7)),
    )


def test_unbookable_days_are_skipped() -> None:
    full = {date(2027, 4, 29), date(2027, 4, 30)}
    options = pick_options(EASTER_2027, lambda s: s.check_in not in full)
    assert [s.check_in for s in options] == [date(2027, 5, 1), date(2027, 5, 3), date(2027, 5, 5)]


def test_a_gap_of_one_day_is_not_enough() -> None:
    # Only 29 and 30 Apr bookable: 30 Apr is 1 day after 29 Apr, so one option only.
    ok = {date(2027, 4, 29), date(2027, 4, 30)}
    options = pick_options(EASTER_2027, lambda s: s.check_in in ok)
    assert [s.check_in for s in options] == [date(2027, 4, 29)]


def test_options_never_run_past_closing() -> None:
    # Late October, 3 nights: check-in 30 or 31 Oct would include the closed 1 Nov.
    options = pick_options(Window(date(2027, 10, 21), date(2027, 10, 31), 3), calendar_ok)
    assert [s.check_in for s in options] == [
        date(2027, 10, 21),
        date(2027, 10, 23),
        date(2027, 10, 25),
    ]
    assert all(calendar_ok(s) for s in options)


def test_western_easter_window_has_no_options() -> None:
    assert pick_options(Window(date(2027, 3, 25), date(2027, 3, 31), 3), calendar_ok) == ()


def test_mid_august_min_stay_applies_to_options() -> None:
    mid_august = date(2027, 8, 11), date(2027, 8, 20)
    # Arrivals 10-20 Aug need 5 nights: 3-night options do not exist ...
    assert pick_options(Window(*mid_august, 3), calendar_ok) == ()
    # ... 5-night options do.
    options = pick_options(Window(*mid_august, 5), calendar_ok)
    assert [s.check_in for s in options] == [
        date(2027, 8, 11),
        date(2027, 8, 13),
        date(2027, 8, 15),
    ]


def test_options_always_respect_the_limits() -> None:
    # Every window start in the season, a few lengths: never more than 3, always in the
    # window, always >= 2 days apart.
    start = date(2027, 4, 15)
    for offset in range(0, 200, 7):
        first = start + timedelta(days=offset)
        for nights in (1, 3, 5):
            window = Window(first, first + timedelta(days=9), nights)
            options = pick_options(window, calendar_ok)
            assert len(options) <= 3
            assert all(window.first_check_in <= s.check_in <= window.last_check_in for s in options)
            gaps = [(b.check_in - a.check_in).days for a, b in pairwise(options)]
            assert all(gap >= 2 for gap in gaps)


# --- window options with the min-stay fallback (D7) ---


def always_free(stay: Stay) -> bool:
    return True


def test_mid_august_3_nights_falls_back_to_5_night_options() -> None:
    result = window_options(HOTEL, Window(date(2027, 8, 11), date(2027, 8, 20), 3), always_free)
    assert result.options == (
        Stay(date(2027, 8, 11), date(2027, 8, 16)),
        Stay(date(2027, 8, 13), date(2027, 8, 18)),
        Stay(date(2027, 8, 15), date(2027, 8, 20)),
    )
    assert result.length_changed
    assert result.length_change_reason == MinStay.code == "MIN_STAY"
    assert result.requested_nights == 3
    assert not result.hold_allowed


def test_no_fallback_when_the_requested_length_fits_somewhere() -> None:
    # 8-12 Aug, 3 nights: 8 Aug (High, min 3) fits, so no 5-night options are offered.
    result = window_options(HOTEL, Window(date(2027, 8, 8), date(2027, 8, 12), 3), always_free)
    assert [s.check_in for s in result.options] == [date(2027, 8, 8)]
    assert all(s.night_count == 3 for s in result.options)
    assert not result.length_changed


def test_no_fallback_when_closed() -> None:
    # Western Easter 2027: closed, not a min-stay problem -> nothing to offer.
    result = window_options(HOTEL, Window(date(2027, 3, 25), date(2027, 3, 31), 3), always_free)
    assert result.options == ()
    assert not result.length_changed


def test_fallback_options_must_be_available() -> None:
    # Only 15 Aug has a room for 5 nights: one longer option.
    window = Window(date(2027, 8, 11), date(2027, 8, 20), 3)
    result = window_options(HOTEL, window, lambda s: s.check_in == date(2027, 8, 15))
    assert result.options == (Stay(date(2027, 8, 15), date(2027, 8, 20)),)
    assert result.length_changed


def test_options_are_never_held() -> None:
    result = window_options(HOTEL, Window(date(2027, 4, 29), date(2027, 5, 5), 4), always_free)
    assert len(result.options) == 3
    assert not result.length_changed
    assert not result.hold_allowed
