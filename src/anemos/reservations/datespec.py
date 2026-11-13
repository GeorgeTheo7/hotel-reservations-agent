"""DateSpec: dates as the guest wrote them, and their resolution into a Stay (D6).

The model only fills in a DateSpec; this code does all calendar maths. Kinds so far:
exact dates, relative weekends (D29) and holidays (D28). An approximate kind joins
`DateSpec` later. Exact and relative dates resolve to one Stay; a holiday resolves to
a Window of possible check-in dates, from which options are picked (D7).

Rules:
- "Today" is the hotel date of `received_at` (Europe/Athens, D23), never the clock.
- A date without a year is its next occurrence on or after today (D21, D30).
- A check-out without a year is its first occurrence on or after check-in, so
  28 Dec - 3 Jan crosses into the next year.
- Length: check-out or nights. Neither -> NEEDS_INFO; both but disagreeing ->
  NEEDS_INFO (ask, don't guess).
- An explicit year is kept even if the date is past; SHORT_NOTICE (D31) then escalates.
- Weekend = check-in Friday, check-out Sunday. "Next weekend" = the coming Friday, or
  the Friday after if received Fri-Sun; "this weekend" = Friday of the current Mon-Sun
  week (received Fri-Sun: always SHORT_NOTICE). A stated night count other than 2 ->
  NEEDS_INFO.
- Holiday: Easter only. Tradition `unspecified` -> Orthodox. Window = Easter +/- 3 days;
  without a year, the first Easter whose window has not ended; check-ins before today
  are cut off. No night count -> NEEDS_INFO. Any other holiday -> NEEDS_INFO.
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, assert_never

from anemos.reservations.easter import orthodox_easter, western_easter
from anemos.reservations.errors import InvalidDates, NeedsInfo
from anemos.reservations.hotel_time import hotel_date
from anemos.reservations.model import Stay, stay_or_error

# A leap year, for "can this day exist in this month at all?" (29 Feb: yes).
_ANY_LEAP_YEAR = 2000
# 29 Feb recurs within 8 years (century years like 2100 skip a leap day).
_MAX_YEARS_AHEAD = 8

FRIDAY = 4  # date.weekday(): Monday = 0
WEEKEND_NIGHTS = 2  # Friday to Sunday
EASTER_WINDOW_DAYS = 3  # check-in from Easter - 3 to Easter + 3 (D7)


@dataclass(frozen=True)
class PartialDate:
    """A day and month, with the year only if the guest wrote one."""

    day: int
    month: int
    year: int | None = None
    # Set by the model when the guest wrote numbers whose order is unclear ("03/04":
    # 3 April or 4 March?). D37: code decides if it really is ambiguous.
    ambiguous_order: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError(f"month must be 1-12, got {self.month}")
        if not 1 <= self.day <= 31:
            raise ValueError(f"day must be 1-31, got {self.day}")

    @property
    def truly_ambiguous(self) -> bool:
        """Both readings are real, different dates: day and month both 1-12 and unequal.
        13/04 has only one reading, 04/04 reads the same both ways."""
        return self.ambiguous_order and self.day <= 12 and self.day != self.month


@dataclass(frozen=True)
class ExactDates:
    """Dates as written, e.g. "12 July for 4 nights", "20-24 May 2027", "28 Dec to 3 Jan"."""

    check_in: PartialDate
    check_out: PartialDate | None = None
    nights: int | None = None


@dataclass(frozen=True)
class RelativeDates:
    """A relative phrase the code knows: "this weekend", "next weekend"."""

    phrase: Literal["this_weekend", "next_weekend"]
    nights: int | None = None


@dataclass(frozen=True)
class HolidayDates:
    """A holiday as named by the guest, e.g. "Easter", "Greek Easter 2027"."""

    holiday: str
    tradition: Literal["orthodox", "western", "unspecified"] = "unspecified"
    year: int | None = None
    nights: int | None = None


type DateSpec = ExactDates | RelativeDates | HolidayDates


@dataclass(frozen=True)
class Window:
    """Check-in on any date from first_check_in to last_check_in (inclusive), for
    `nights` nights. Options inside it are picked against availability (D7)."""

    first_check_in: date
    last_check_in: date
    nights: int


def resolve(spec: DateSpec, received_at: datetime) -> Stay | Window | NeedsInfo | InvalidDates:
    today = hotel_date(received_at)
    match spec:
        case ExactDates():
            return _resolve_exact(spec, today)
        case RelativeDates():
            return _resolve_weekend(spec, today)
        case HolidayDates():
            return _resolve_holiday(spec, today)
        case _:
            # mypy proves this is unreachable; a new DateSpec kind without a branch
            # becomes a type error here.
            assert_never(spec)


def _resolve_weekend(spec: RelativeDates, today: date) -> Stay | NeedsInfo:
    if spec.nights is not None and spec.nights != WEEKEND_NIGHTS:
        return NeedsInfo(
            f"a weekend is Friday to Sunday ({WEEKEND_NIGHTS} nights), but {spec.nights}"
            " nights were asked"
        )
    weekday = today.weekday()
    this_friday = today + timedelta(days=FRIDAY - weekday)  # in the past on Sat/Sun
    if spec.phrase == "next_weekend" and weekday >= FRIDAY:
        friday = this_friday + timedelta(days=7)
    else:
        friday = this_friday
    return Stay(friday, friday + timedelta(days=WEEKEND_NIGHTS))


def _resolve_holiday(spec: HolidayDates, today: date) -> Window | NeedsInfo | InvalidDates:
    if spec.holiday.strip().lower() != "easter":
        return NeedsInfo(f"dates for {spec.holiday!r} (only Easter is known)")
    if spec.nights is None:
        return NeedsInfo("length of stay (number of nights)")
    if spec.nights < 1:
        return InvalidDates(f"{spec.nights} nights")
    easter = western_easter if spec.tradition == "western" else orthodox_easter
    window = timedelta(days=EASTER_WINDOW_DAYS)
    try:
        if spec.year is not None:
            sunday = easter(spec.year)
            return Window(sunday - window, sunday + window, spec.nights)
        year = today.year
        while easter(year) + window < today:  # this year's window is over
            year += 1
        sunday = easter(year)
    except ValueError as error:  # a year the Easter functions do not support
        return InvalidDates(str(error))
    return Window(max(sunday - window, today), sunday + window, spec.nights)


def _resolve_exact(spec: ExactDates, today: date) -> Stay | NeedsInfo | InvalidDates:
    for partial in (spec.check_in, spec.check_out):
        if partial is not None and partial.truly_ambiguous:
            return NeedsInfo(
                f"day/month order of {partial.day:02d}/{partial.month:02d}: "
                f"{_month_day(partial.day, partial.month)} or "
                f"{_month_day(partial.month, partial.day)}?"
            )
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


def _month_day(day: int, month: int) -> str:
    """For owner-facing notes only (English); guest wording comes from templates."""
    return f"{day} {calendar.month_name[month]}"


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
