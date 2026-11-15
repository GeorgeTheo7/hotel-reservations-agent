"""DateSpec: dates as the guest wrote them, and their resolution into a Stay (D6).

The model only fills in a DateSpec; this code does all calendar maths. Kinds so far:
exact dates, relative weekends (D29), holidays (D28) and approximate periods (D7).
Exact and relative dates resolve to one Stay; holidays and approximate periods resolve
to a Window of possible check-in dates; `options.window_options` picks up to 3.

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
- Holiday: easter. Tradition `unspecified` -> Orthodox. Window = Easter +/- 3 days;
  without a year, the first Easter whose window has not ended; check-ins before today
  are cut off. No night count -> NEEDS_INFO.
- Fixed-date holidays (D68): Christmas 24-26 Dec, New Year 31 Dec - 1 Jan, resolve to
  a FixedHoliday (their days, not a stay): the caller decides with the calendar
  (NO_CALENDAR, then CLOSED if every day is outside the season, else NEEDS_INFO).
  Without a year, the next occurrence that has not ended, days before today cut off.
  "New Year 2028" is 31 Dec 2027 - 1 Jan 2028 (the year of 1 January). Names are
  matched exactly (easter, christmas, new_year: the canonical names of prompt
  extraction_v2); any other name (15 August too, deliberately) -> NEEDS_INFO.
- Approximate: early = 1-10, mid = 11-20, late = 21-end of month. Year and cut-off as
  for Easter. No night count -> NEEDS_INFO. A whole month ("sometime in June") ->
  NEEDS_INFO in v1 (D7).
"""

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, assert_never

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
# Part of a month -> (first day, last day or None for the month's last day). D7.
MONTH_PARTS: dict[str, tuple[int, int | None]] = {
    "early": (1, 10),
    "mid": (11, 20),
    "late": (21, None),
}


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


@dataclass(frozen=True)
class ApproximateDates:
    """Part of a month, e.g. "late June for a week", "early October 2027, 3 nights"."""

    part: Literal["early", "mid", "late", "whole_month"]
    month: int
    year: int | None = None
    nights: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError(f"month must be 1-12, got {self.month}")


type DateSpec = ExactDates | RelativeDates | HolidayDates | ApproximateDates

# D68: holidays on fixed dates, (month, day) of the first and last day; the last day may
# be in the next year (New Year). Keys are the canonical names extraction writes
# (prompt extraction_v2); a name is matched exactly, anything else is NEEDS_INFO.
FIXED_HOLIDAYS: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "christmas": ((12, 24), (12, 26)),
    "new_year": ((12, 31), (1, 1)),
}


@dataclass(frozen=True)
class Window:
    """Check-in on any date from first_check_in to last_check_in (inclusive), for
    `nights` nights. Options inside it are picked against availability (D7)."""

    first_check_in: date
    last_check_in: date
    nights: int

    def candidate_nights(self) -> tuple[date, ...]:
        """Every night of every stay the window allows: first check-in up to last
        check-in + nights (D70: NO_CALENDAR and CLOSED look at all of them)."""
        end = self.last_check_in + timedelta(days=self.nights)
        return tuple(
            self.first_check_in + timedelta(days=i) for i in range((end - self.first_check_in).days)
        )


@dataclass(frozen=True)
class FixedHoliday:
    """The days of a fixed-date holiday (D68), from today on. Not a stay: only the
    calendar decides what happens (NO_CALENDAR, CLOSED or NEEDS_INFO)."""

    key: str  # a FIXED_HOLIDAYS key
    first_day: date
    last_day: date

    def days(self) -> tuple[date, ...]:
        count = (self.last_day - self.first_day).days + 1
        return tuple(self.first_day + timedelta(days=i) for i in range(count))


def resolve(
    spec: DateSpec, received_at: datetime
) -> Stay | Window | FixedHoliday | NeedsInfo | InvalidDates:
    today = hotel_date(received_at)
    match spec:
        case ExactDates():
            return _resolve_exact(spec, today)
        case RelativeDates():
            return _resolve_weekend(spec, today)
        case HolidayDates():
            return _resolve_holiday(spec, today)
        case ApproximateDates():
            return _resolve_approximate(spec, today)
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


def _resolve_holiday(
    spec: HolidayDates, today: date
) -> Window | FixedHoliday | NeedsInfo | InvalidDates:
    if spec.holiday in FIXED_HOLIDAYS:  # exact canonical names only (extraction_v2)
        return _resolve_fixed(spec.holiday, spec.year, today)
    if spec.holiday != "easter":
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


def _resolve_fixed(key: str, year: int | None, today: date) -> FixedHoliday | InvalidDates:
    (m1, d1), (m2, d2) = FIXED_HOLIDAYS[key]
    crosses = (m2, d2) < (m1, d1)  # ends in the next year (New Year)

    def days_of(last_year: int) -> tuple[date, date]:
        """The occurrence whose last day is in `last_year`."""
        return date(last_year - 1 if crosses else last_year, m1, d1), date(last_year, m2, d2)

    try:
        if year is not None:
            first, last = days_of(year)
            return FixedHoliday(key, first, last)
        last_year = today.year
        while days_of(last_year)[1] < today:  # this year's occurrence is over
            last_year += 1
        first, last = days_of(last_year)
    except ValueError as error:  # a year date() cannot hold
        return InvalidDates(str(error))
    return FixedHoliday(key, max(first, today), last)


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


def _resolve_approximate(spec: ApproximateDates, today: date) -> Window | NeedsInfo | InvalidDates:
    if spec.part == "whole_month":
        month = calendar.month_name[spec.month]
        return NeedsInfo(f"which dates in {month} (early, mid or late {month}, or exact dates)")
    if spec.nights is None:
        return NeedsInfo("length of stay (number of nights)")
    if spec.nights < 1:
        return InvalidDates(f"{spec.nights} nights")

    def part_of(year: int) -> tuple[date, date]:
        first_day, last_day = MONTH_PARTS[spec.part]
        last = last_day or calendar.monthrange(year, spec.month)[1]
        return date(year, spec.month, first_day), date(year, spec.month, last)

    if spec.year is not None:
        first, last = part_of(spec.year)
        return Window(first, last, spec.nights)
    year = today.year
    while part_of(year)[1] < today:  # this year's part of the month is over
        year += 1
    first, last = part_of(year)
    return Window(max(first, today), last, spec.nights)


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


# --- a DateSpec stored as JSON --------------------------------------------------------

_KEYS: dict[str, frozenset[str]] = {
    "exact": frozenset({"kind", "check_in", "check_out", "nights"}),
    "relative": frozenset({"kind", "phrase", "nights"}),
    "holiday": frozenset({"kind", "holiday", "tradition", "year", "nights"}),
    "approximate": frozenset({"kind", "part", "month", "year", "nights"}),
}
_PARTIAL_KEYS = frozenset({"day", "month", "year", "ambiguous_order"})
_PHRASES: tuple[Literal["this_weekend", "next_weekend"], ...] = ("this_weekend", "next_weekend")
_TRADITIONS: tuple[Literal["orthodox", "western", "unspecified"], ...] = (
    "orthodox", "western", "unspecified",
)  # fmt: skip
_PARTS: tuple[Literal["early", "mid", "late", "whole_month"], ...] = (
    "early", "mid", "late", "whole_month",
)  # fmt: skip


def date_spec_from_json(raw: Mapping[str, Any]) -> DateSpec:
    """A DateSpec as extraction stored it (`requests.extracted["date_spec"]`: every key
    written out, unknown = null) back to its dataclass. Strict: an unknown kind, a
    missing or extra key, or a wrong type is a bug in what was stored -> ValueError."""
    kind = raw.get("kind")
    if kind not in _KEYS:
        raise ValueError(f"stored date_spec: unknown kind {kind!r}")
    if set(raw) != _KEYS[kind]:
        raise ValueError(
            f"stored date_spec ({kind}): keys {sorted(raw)}, expected {sorted(_KEYS[kind])}"
        )
    match kind:
        case "exact":
            out = raw["check_out"]
            return ExactDates(
                _partial(raw["check_in"]), None if out is None else _partial(out),
                _int_or_none(raw, "nights"),
            )  # fmt: skip
        case "relative":
            return RelativeDates(_choice(raw, "phrase", _PHRASES),
                                 _int_or_none(raw, "nights"))  # fmt: skip
        case "holiday":
            holiday = raw["holiday"]
            if not isinstance(holiday, str):
                raise ValueError(f"stored date_spec: holiday {holiday!r} is not text")
            tradition = _choice(raw, "tradition", _TRADITIONS)
            return HolidayDates(holiday, tradition, _int_or_none(raw, "year"),
                                _int_or_none(raw, "nights"))  # fmt: skip
        case _:
            part = _choice(raw, "part", _PARTS)
            return ApproximateDates(part, _int(raw, "month"), _int_or_none(raw, "year"),
                                    _int_or_none(raw, "nights"))  # fmt: skip


def _partial(raw: Any) -> PartialDate:
    if not isinstance(raw, Mapping) or set(raw) != _PARTIAL_KEYS:
        raise ValueError(f"stored date_spec: date {raw!r}, expected keys {sorted(_PARTIAL_KEYS)}")
    flag = raw["ambiguous_order"]
    if not isinstance(flag, bool):
        raise ValueError(f"stored date_spec: ambiguous_order {flag!r} is not true/false")
    return PartialDate(_int(raw, "day"), _int(raw, "month"), _int_or_none(raw, "year"), flag)


def _int(raw: Mapping[str, Any], key: str) -> int:
    value = raw[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"stored date_spec: {key} {value!r} is not a whole number")
    return value


def _int_or_none(raw: Mapping[str, Any], key: str) -> int | None:
    return None if raw[key] is None else _int(raw, key)


def _choice[T: str](raw: Mapping[str, Any], key: str, allowed: tuple[T, ...]) -> T:
    value = raw[key]
    for option in allowed:
        if value == option:
            return option
    raise ValueError(f"stored date_spec: {key} {value!r} is not one of {allowed}")
