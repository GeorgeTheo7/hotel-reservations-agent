"""Options inside a check-in window (D7): what to offer for "Easter" or "late June".

Up to 3 stays starting in the window, check-ins at least 2 days apart, earliest first.
If no stay of the requested length fits *only because of the minimum stay*, offer the
min-stay length instead and say so (mid-August: 3 nights asked, 5 required).
Options are never held (D9): the guest asked for a period, not for these dates.
Pure: availability is passed in as a function, so the tools can add the database later.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from anemos.reservations.datespec import Window
from anemos.reservations.errors import MinStay
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import Stay
from anemos.reservations.stay_rules import check_stay, min_stay_version

MAX_OPTIONS = 3
MIN_DAYS_BETWEEN_OPTIONS = 2


@dataclass(frozen=True)
class WindowOptions:
    options: tuple[Stay, ...]  # empty = nothing fits (alternatives / decline)
    requested_nights: int
    # Why the options are longer than asked; None if they have the requested length.
    length_change_reason: str | None = None

    @property
    def length_changed(self) -> bool:
        return self.length_change_reason is not None

    @property
    def hold_allowed(self) -> bool:
        """Always False: options are a choice, never exactly what was asked (D9)."""
        return False


def window_options(
    hotel: HotelData, window: Window, is_available: Callable[[Stay], bool]
) -> WindowOptions:
    def bookable(stay: Stay) -> bool:
        return check_stay(hotel, stay) is None and is_available(stay)

    options = pick_options(window, bookable)
    if options:
        return WindowOptions(options, window.nights)

    def min_stay_length(check_in: date) -> Stay | None:
        """The min-stay version of this check-in, if MIN_STAY is the only problem."""
        asked = Stay(check_in, check_in + timedelta(days=window.nights))
        if not isinstance(check_stay(hotel, asked), MinStay):
            return None  # closed, no calendar, or already fine: not a min-stay case
        longer = min_stay_version(hotel, asked)
        return longer if bookable(longer) else None

    longer_options = _greedy(window, min_stay_length)
    if longer_options:
        return WindowOptions(longer_options, window.nights, length_change_reason=MinStay.code)
    return WindowOptions((), window.nights)


def pick_options(window: Window, is_bookable: Callable[[Stay], bool]) -> tuple[Stay, ...]:
    """Up to 3 bookable stays of the window's length, check-ins >= 2 days apart."""

    def requested_length(check_in: date) -> Stay | None:
        stay = Stay(check_in, check_in + timedelta(days=window.nights))
        return stay if is_bookable(stay) else None

    return _greedy(window, requested_length)


def _greedy(window: Window, candidate_for: Callable[[date], Stay | None]) -> tuple[Stay, ...]:
    """Earliest first: walk the check-in dates in order and take a candidate when there
    is one and it is far enough from the last one taken."""
    options: list[Stay] = []
    check_in = window.first_check_in
    while check_in <= window.last_check_in and len(options) < MAX_OPTIONS:
        far_enough = (
            not options or (check_in - options[-1].check_in).days >= MIN_DAYS_BETWEEN_OPTIONS
        )
        if far_enough:
            candidate = candidate_for(check_in)
            if candidate is not None:
                options.append(candidate)
        check_in += timedelta(days=1)
    return tuple(options)
