"""DateSpec: dates as the guest wrote them, and their resolution into a Stay (D6).

The model only fills in a DateSpec; this code does all calendar maths. Kinds so far:
exact dates. Relative, holiday and approximate kinds join `DateSpec` as a union, each
with its own resolver.

Rules:
- "Today" is the hotel date of `received_at` (Europe/Athens, D23), never the clock.
- A date without a year is its next occurrence on or after today (D21, D30).
- A check-out without a year is its first occurrence on or after check-in, so
  28 Dec - 3 Jan crosses into the next year.
- Length: check-out or nights. Neither -> NEEDS_INFO; both but disagreeing ->
  NEEDS_INFO (ask, don't guess).
- An explicit year is kept even if the date is past; SHORT_NOTICE (D31) then escalates.
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from anemos.reservations.errors import InvalidDates, NeedsInfo
from anemos.reservations.hotel_time import hotel_date
from anemos.reservations.model import Stay, stay_or_error

# A leap year, for "can this day exist in this month at all?" (29 Feb: yes).
_ANY_LEAP_YEAR = 2000
# 29 Feb recurs within 8 years (century years like 2100 skip a leap day).
_MAX_YEARS_AHEAD = 8


@dataclass(frozen=True)
class PartialDate:
    """A day and month, with the year only if the guest wrote one."""

    day: int
    month: int
    year: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError(f"month must be 1-12, got {self.month}")
        if not 1 <= self.day <= 31:
            raise ValueError(f"day must be 1-31, got {self.day}")


@dataclass(frozen=True)
class ExactDates:
    """Dates as written, e.g. "12 July for 4 nights", "20-24 May 2027", "28 Dec to 3 Jan"."""

    check_in: PartialDate
    check_out: PartialDate | None = None
    nights: int | None = None


type DateSpec = ExactDates


def resolve(spec: DateSpec, received_at: datetime) -> Stay | NeedsInfo | InvalidDates:
    today = hotel_date(received_at)
    return _resolve_exact(spec, today)


def _resolve_exact(spec: ExactDates, today: date) -> Stay | NeedsInfo | InvalidDates:
    check_in = _on_or_after(spec.check_in, today)
    if isinstance(check_in, InvalidDates):
        return check_in

    check_out: date | None = None
    if spec.check_out is not None:
        resolved = _on_or_after(spec.check_out, check_in)
        if isinstance(resolved, InvalidDates):
            return resolved
        check_out = resolved
    if spec.nights is not None:
        if spec.nights < 1:
            return InvalidDates(f"{spec.nights} nights")
        by_nights = check_in + timedelta(days=spec.nights)
        if check_out is not None and check_out != by_nights:
            return NeedsInfo(
                f"check-out {check_out} and {spec.nights} nights from {check_in} disagree"
            )
        check_out = by_nights
    if check_out is None:
        return NeedsInfo("length of stay (check-out date or number of nights)")
    return stay_or_error(check_in, check_out)


def _on_or_after(partial: PartialDate, earliest: date) -> date | InvalidDates:
    """The date itself if it has a year; else its first occurrence on or after `earliest`."""
    if partial.year is not None:
        try:
            return date(partial.year, partial.month, partial.day)
        except ValueError:
            return InvalidDates(f"{partial.day}/{partial.month}/{partial.year} does not exist")
    if partial.day > calendar.monthrange(_ANY_LEAP_YEAR, partial.month)[1]:
        return InvalidDates(f"{partial.day}/{partial.month} does not exist in any year")
    for year in range(earliest.year, earliest.year + _MAX_YEARS_AHEAD + 1):
        try:
            candidate = date(year, partial.month, partial.day)
        except ValueError:  # 29 Feb outside a leap year
            continue
        if candidate >= earliest:
            return candidate
    raise AssertionError(f"no occurrence of {partial} found")  # unreachable
