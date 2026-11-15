"""D68: fixed-date holidays (Christmas 24-26 Dec, New Year 31 Dec - 1 Jan). The date
code resolves them to their days; the calendar decides: NO_CALENDAR first (D36), CLOSED
if every day is outside the season (decline, D62), else ask for dates."""

from datetime import UTC, date, datetime

import pytest

from anemos.db.seed import hotel_data
from anemos.extraction import ExtractionOutput
from anemos.reservations.datespec import FixedHoliday, HolidayDates, Window, resolve
from anemos.reservations.errors import Closed, NeedsEscalation, NeedsInfo, ReasonCode
from anemos.reservations.tools import holiday_outcome
from anemos.routing import EmailFacts, Escalate, ToAgent, route

pytestmark = pytest.mark.unit

HOTEL = hotel_data()  # season periods for 2027 only


def at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 9, 0, tzinfo=UTC)


def d(year: int, month: int, day: int) -> date:
    return date(year, month, day)


# --- resolution -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "year", "received", "expected"),
    [
        ("christmas", None, at(2026, 11, 16), ("christmas", d(2026, 12, 24), d(2026, 12, 26))),
        ("christmas", 2027, at(2027, 1, 10), ("christmas", d(2027, 12, 24), d(2027, 12, 26))),
        # Received after Christmas: the next one.
        ("christmas", None, at(2026, 12, 27), ("christmas", d(2027, 12, 24), d(2027, 12, 26))),
        # Received on 25 Dec: the days left of this one.
        ("christmas", None, at(2026, 12, 25), ("christmas", d(2026, 12, 25), d(2026, 12, 26))),
        # New Year crosses the year; its year is the year of 1 January.
        ("new_year", None, at(2027, 1, 10), ("new_year", d(2027, 12, 31), d(2028, 1, 1))),
        ("new_year", 2028, at(2027, 1, 10), ("new_year", d(2027, 12, 31), d(2028, 1, 1))),
        ("new_year", None, at(2026, 12, 31), ("new_year", d(2026, 12, 31), d(2027, 1, 1))),
        ("new_year", None, at(2027, 1, 1), ("new_year", d(2027, 1, 1), d(2027, 1, 1))),
    ],
)
def test_fixed_holidays_resolve_to_their_days(
    name: str, year: int | None, received: datetime, expected: tuple[str, date, date]
) -> None:
    result = resolve(HolidayDates(name, year=year, nights=3), received)
    assert result == FixedHoliday(*expected)


def test_new_year_days_cross_the_year() -> None:
    assert FixedHoliday("new_year", d(2027, 12, 31), d(2028, 1, 1)).days() == (
        d(2027, 12, 31), d(2028, 1, 1),
    )  # fmt: skip


def test_new_years_eve_2027_as_extracted_by_v2() -> None:
    """Prompt extraction_v2: New Year's Eve and New Year's Day are both new_year, with
    the year of 1 January, so "New Year's Eve 2027" arrives as new_year 2028."""
    result = resolve(HolidayDates("new_year", year=2028, nights=2), at(2027, 1, 10))
    assert result == FixedHoliday("new_year", d(2027, 12, 31), d(2028, 1, 1))


@pytest.mark.parametrize(
    "name", ["Christmas", "Xmas", "New Year's Eve", "new year", "Easter", "15 August"]
)
def test_only_canonical_names_are_known(name: str) -> None:
    """Names are matched exactly; anything else asks for dates (safe, never a guess)."""
    result = resolve(HolidayDates(name, year=2027, nights=2), at(2027, 1, 10))
    assert isinstance(result, NeedsInfo)


def test_easter_is_unchanged() -> None:
    result = resolve(HolidayDates("easter", nights=4), at(2026, 12, 10))
    assert result == Window(d(2027, 4, 29), d(2027, 5, 5), 4)


def test_fifteenth_of_august_asks_for_dates() -> None:
    assert isinstance(resolve(HolidayDates("15 August", nights=3), at(2027, 1, 5)), NeedsInfo)


# --- the calendar decides ---------------------------------------------------------------


def test_christmas_2026_has_no_calendar() -> None:
    result = holiday_outcome(HOTEL, FixedHoliday("christmas", d(2026, 12, 24), d(2026, 12, 26)))
    assert isinstance(result, NeedsEscalation) and result.reason is ReasonCode.NO_CALENDAR


def test_christmas_2027_is_closed() -> None:
    result = holiday_outcome(HOTEL, FixedHoliday("christmas", d(2027, 12, 24), d(2027, 12, 26)))
    assert result == Closed((d(2027, 12, 24), d(2027, 12, 25), d(2027, 12, 26)))


def test_new_year_into_a_year_without_calendar_is_no_calendar_not_closed() -> None:
    # 31 Dec 2027 is closed in a year with a calendar; 1 Jan 2028 has none (D36 first).
    result = holiday_outcome(HOTEL, FixedHoliday("new_year", d(2027, 12, 31), d(2028, 1, 1)))
    assert isinstance(result, NeedsEscalation) and result.reason is ReasonCode.NO_CALENDAR


def test_a_holiday_inside_the_season_asks_for_dates() -> None:
    # No fixed-date holiday falls in the 2027 season; the branch is checked directly.
    inside = FixedHoliday("christmas", d(2027, 6, 24), d(2027, 6, 26))
    assert holiday_outcome(HOTEL, inside) == NeedsInfo("dates of the stay")


# --- the router agrees (D70 rules 7, 8 and 14) -----------------------------------------


def routed(holiday: FixedHoliday, received: datetime) -> object:
    extraction = ExtractionOutput.model_validate({
        "kind": "request", "language": "el", "script": "greek", "flags": [],
        "date_spec": None, "adults": 2, "children": 0, "child_ages": [], "rooms": 1,
        "room_type": None, "arrival_time": None,
    })  # fmt: skip
    return route(EmailFacts(received, None), extraction, holiday, HOTEL)


def test_router_christmas_2026_escalates_no_calendar() -> None:
    result = routed(FixedHoliday("christmas", d(2026, 12, 24), d(2026, 12, 26)), at(2026, 11, 16))
    assert isinstance(result, Escalate) and result.codes == (ReasonCode.NO_CALENDAR,)


def test_router_christmas_2027_declines() -> None:
    result = routed(FixedHoliday("christmas", d(2027, 12, 24), d(2027, 12, 26)), at(2027, 1, 10))
    assert isinstance(result, ToAgent) and result.route == "decline"


def test_router_holiday_inside_the_season_asks() -> None:
    result = routed(FixedHoliday("christmas", d(2027, 6, 24), d(2027, 6, 26)), at(2027, 1, 10))
    assert isinstance(result, ToAgent) and result.route == "ask_info"
