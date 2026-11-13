from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from anemos.clock import FixedClock
from anemos.db.seed import hotel_data
from anemos.reservations.errors import Closed, MinStay, NeedsEscalation, ReasonCode
from anemos.reservations.hotel_data import MinStayRule
from anemos.reservations.hotel_time import HOTEL_TZ
from anemos.reservations.model import RoomOffer, RoomRequest, Stay
from anemos.reservations.stay_rules import (
    cancellation_deadline,
    check_stay,
    group,
    hold_allowed,
    min_stay_for,
    min_stay_version,
    open_part,
    short_notice,
)

pytestmark = pytest.mark.unit

HOTEL = hotel_data()


def d(month: int, day: int, year: int = 2027) -> date:
    return date(year, month, day)


def stay(m1: int, d1: int, m2: int, d2: int) -> Stay:
    return Stay(d(m1, d1), d(m2, d2))


# --- minimum stay by arrival date ---


@pytest.mark.parametrize(
    ("arrival", "nights"),
    [
        (d(4, 14), None),  # closed
        (d(4, 15), 1),
        (d(5, 31), 1),
        (d(6, 1), 2),
        (d(7, 14), 2),
        (d(7, 15), 3),
        (d(8, 9), 3),
        (d(8, 10), 5),  # 10-20 Aug rule
        (d(8, 20), 5),
        (d(8, 21), 3),
        (d(9, 1), 2),
        (d(10, 31), 1),
        (d(11, 1), None),
    ],
)
def test_min_stay_for(arrival: date, nights: int | None) -> None:
    assert min_stay_for(HOTEL, arrival) == nights


# --- check_stay: the examples' rule outcomes ---


@pytest.mark.parametrize(
    ("example", "s"),
    [
        ("ex01", stay(5, 20, 5, 21)),
        ("ex02", stay(7, 12, 7, 16)),
        ("ex03", stay(5, 29, 6, 3)),
        ("ex05", stay(9, 1, 9, 4)),
        ("ex06", stay(7, 20, 7, 24)),
        ("ex08", stay(6, 5, 6, 8)),
        ("ex09", stay(10, 3, 10, 5)),
        ("ex10", stay(7, 13, 7, 17)),  # arrival Mid: min 2, not High's 3
        ("ex11", stay(8, 24, 8, 27)),  # 24 Aug is outside 10-20 Aug: min 3, stay 3
        ("ex12", stay(10, 4, 10, 7)),
    ],
)
def test_examples_pass_the_calendar(example: str, s: Stay) -> None:
    assert check_stay(HOTEL, s) is None


def test_ex04_fails_min_stay_5() -> None:
    assert check_stay(HOTEL, stay(8, 10, 8, 13)) == MinStay(5)


def test_ex07_reports_the_closed_night() -> None:
    # 30 Oct - 2 Nov = nights of 30 Oct, 31 Oct, 1 Nov; only 1 Nov is closed.
    assert check_stay(HOTEL, stay(10, 30, 11, 2)) == Closed((d(11, 1),))


def test_closed_is_reported_before_min_stay() -> None:
    # Arrival 14 Apr (closed) for 1 night: CLOSED, not MIN_STAY.
    assert check_stay(HOTEL, stay(4, 14, 4, 15)) == Closed((d(4, 14),))


def test_western_easter_2027_is_closed() -> None:
    result = check_stay(HOTEL, stay(3, 28, 3, 31))
    assert result == Closed((d(3, 28), d(3, 29), d(3, 30)))


def test_year_without_calendar_escalates_not_closed() -> None:
    # D36: June 2028 has no season periods at all (only 2027 is seeded).
    result = check_stay(HOTEL, Stay(date(2028, 6, 10), date(2028, 6, 13)))
    assert isinstance(result, NeedsEscalation)
    assert result.reason == ReasonCode.NO_CALENDAR
    assert "2028" in result.detail


def test_stay_crossing_into_a_year_without_calendar_escalates() -> None:
    # 30 Dec 2027 is CLOSED (configured year), but 1 Jan 2028 has no calendar: escalate.
    result = check_stay(HOTEL, Stay(date(2027, 12, 30), date(2028, 1, 2)))
    assert isinstance(result, NeedsEscalation)
    assert result.reason == ReasonCode.NO_CALENDAR


def test_has_calendar_for() -> None:
    assert HOTEL.has_calendar_for(2027)
    assert not HOTEL.has_calendar_for(2026)
    assert not HOTEL.has_calendar_for(2028)


def test_high_arrival_needs_3_nights() -> None:
    assert check_stay(HOTEL, stay(7, 15, 7, 17)) == MinStay(3)
    assert check_stay(HOTEL, stay(7, 15, 7, 18)) is None


# --- open part and min-stay version ---


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (stay(10, 30, 11, 2), stay(10, 30, 11, 1)),  # Ex 7: runs past closing
        (stay(4, 13, 4, 17), stay(4, 15, 4, 17)),  # starts before opening (agreed default)
        (stay(5, 20, 5, 21), stay(5, 20, 5, 21)),  # all open: unchanged
        (stay(3, 28, 3, 31), None),  # all closed
    ],
)
def test_open_part(requested: Stay, expected: Stay | None) -> None:
    assert open_part(HOTEL, requested) == expected


def test_open_part_of_ex07_passes_the_calendar() -> None:
    part = open_part(HOTEL, stay(10, 30, 11, 2))
    assert part is not None
    assert check_stay(HOTEL, part) is None


def test_trimmed_stay_is_rechecked_for_min_stay() -> None:
    # D34, made-up calendar: October arrivals need 3 nights (seed: 1).
    october = MinStayRule(d(10, 1), d(11, 1), 3)
    rules = (*(r for r in HOTEL.min_stay_rules if r.first_arrival != d(10, 1)), october)
    strict = replace(HOTEL, min_stay_rules=rules)
    part = open_part(strict, stay(10, 30, 11, 2))
    assert part == stay(10, 30, 11, 1)  # 2 open nights
    assert check_stay(strict, part) == MinStay(3)  # too short for its arrival date
    # Its min-stay version runs back into the closed night, and is caught again.
    assert check_stay(strict, min_stay_version(strict, part)) == Closed((d(11, 1),))


def test_min_stay_version_ex04() -> None:
    version = min_stay_version(HOTEL, stay(8, 10, 8, 13))
    assert version == stay(8, 10, 8, 15)
    assert check_stay(HOTEL, version) is None


def test_min_stay_version_leaves_a_valid_stay_alone() -> None:
    assert min_stay_version(HOTEL, stay(7, 12, 7, 16)) == stay(7, 12, 7, 16)


# --- cancellation deadline (D26) ---


@pytest.mark.parametrize(
    ("example", "s", "deadline"),
    [
        ("ex01", stay(5, 20, 5, 21), d(5, 6)),
        ("ex02", stay(7, 12, 7, 16), d(6, 28)),
        ("ex03", stay(5, 29, 6, 3), d(5, 15)),
        ("ex04", stay(8, 10, 8, 15), d(7, 11)),  # High: 30 days
        ("ex05", stay(9, 1, 9, 4), d(8, 18)),
        ("ex06", stay(7, 20, 7, 24), d(6, 20)),
        ("ex07", stay(10, 30, 11, 1), d(10, 16)),
        ("ex08", stay(6, 5, 6, 8), d(5, 22)),
        ("ex09", stay(10, 3, 10, 5), d(9, 19)),
        ("ex10", stay(7, 13, 7, 17), d(6, 29)),  # arrival Mid: 14 days though 2 nights High
        ("ex11", stay(8, 24, 8, 27), d(7, 25)),
        ("ex12", stay(10, 4, 10, 7), d(9, 20)),
    ],
)
def test_cancellation_deadline_examples(example: str, s: Stay, deadline: date) -> None:
    assert cancellation_deadline(HOTEL, s) == deadline


def test_cancellation_deadline_switches_on_arrival_season() -> None:
    assert cancellation_deadline(HOTEL, stay(7, 14, 7, 16)) == d(6, 30)  # Mid: 14
    assert cancellation_deadline(HOTEL, stay(7, 15, 7, 18)) == d(6, 15)  # High: 30
    assert cancellation_deadline(HOTEL, stay(9, 1, 9, 3)) == d(8, 18)  # Mid again: 14


# --- hold allowed (D9) ---


def offer(s: Stay, type_id: str, rooms: int = 1) -> RoomOffer:
    return RoomOffer(s, rooms, type_id)


def test_hold_allowed_for_an_exact_match() -> None:
    ex1 = stay(5, 20, 5, 21)
    assert hold_allowed(RoomRequest(ex1, 1, "standard"), [offer(ex1, "standard")])
    ex11 = stay(8, 24, 8, 27)
    assert hold_allowed(RoomRequest(ex11, 2, "standard"), [offer(ex11, "standard", 2)])


def test_no_hold_for_min_stay_version_ex04() -> None:
    asked = RoomRequest(stay(8, 10, 8, 13), 1, "sea_view")
    assert not hold_allowed(asked, [offer(stay(8, 10, 8, 15), "sea_view")])


def test_no_hold_for_open_part_ex07() -> None:
    asked = RoomRequest(stay(10, 30, 11, 2), 1, "standard")
    assert not hold_allowed(asked, [offer(stay(10, 30, 11, 1), "standard")])


def test_no_hold_for_other_room_type_ex08() -> None:
    s = stay(6, 5, 6, 8)
    assert not hold_allowed(RoomRequest(s, 1, "sea_view"), [offer(s, "family_suite")])


@pytest.mark.parametrize("count", [2, 3])
def test_no_hold_when_two_or_more_options_are_offered(count: int) -> None:
    # Even if one option is exactly what was asked, a choice means no hold (D9).
    s = stay(5, 20, 5, 21)
    options = [offer(s, "standard"), offer(s, "sea_view"), offer(s, "family_suite")][:count]
    assert not hold_allowed(RoomRequest(s, 1, "standard"), options)
    assert not hold_allowed(RoomRequest(s), options)


def test_no_hold_for_two_identical_options() -> None:
    s = stay(5, 20, 5, 21)
    assert not hold_allowed(RoomRequest(s), [offer(s, "standard"), offer(s, "standard")])


def test_hold_allowed_when_guest_named_no_type() -> None:
    s = stay(5, 20, 5, 21)
    assert hold_allowed(RoomRequest(s), [offer(s, "standard")])


def test_no_hold_for_a_different_room_count() -> None:
    s = stay(8, 24, 8, 27)
    assert not hold_allowed(RoomRequest(s, 1, "standard"), [offer(s, "standard", 2)])


# --- SHORT_NOTICE (D31) ---


def athens(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2027, month, day, hour, minute, tzinfo=HOTEL_TZ)


def as_given_and_via_fixed_clock(at: datetime) -> list[datetime]:
    """The same instant twice: in Athens time (same tzinfo as check-in, the DST trap)
    and in UTC as the injected clock returns it (D20, D23)."""
    return [at, FixedClock(at).now()]


# Clocks go forward on 28 Mar 2027 (03:00 -> 04:00). Arrival 29 Mar 15:00 Athens =
# 12:00 UTC; 48 h earlier = 27 Mar 12:00 UTC = 14:00 Athens, not 15:00.
# Clocks go back on 31 Oct 2027 (04:00 -> 03:00). Arrival 31 Oct 15:00 Athens =
# 13:00 UTC; 48 h earlier = 29 Oct 13:00 UTC = 16:00 Athens, not 15:00.
DST_CASES = [
    # arrival, received (Athens wall clock), short notice?
    ("spring exactly 48 h", d(3, 29), athens(3, 27, 14, 0), False),
    ("spring 1 min late", d(3, 29), athens(3, 27, 14, 1), True),
    ("spring wall-clock 48 h", d(3, 29), athens(3, 27, 15, 0), True),
    ("autumn exactly 48 h", d(10, 31), athens(10, 29, 16, 0), False),
    ("autumn 1 min late", d(10, 31), athens(10, 29, 16, 1), True),
    ("autumn 1 min early", d(10, 31), athens(10, 29, 15, 59), False),
]


@pytest.mark.parametrize(("case", "arrival", "received", "expected"), DST_CASES)
def test_short_notice_across_dst(
    case: str, arrival: date, received: datetime, expected: bool
) -> None:
    for received_at in as_given_and_via_fixed_clock(received):
        result = short_notice(arrival, received_at)
        is_short = result is not None and result.reason == ReasonCode.SHORT_NOTICE
        assert is_short is expected, f"{case}: received {received_at}"


def test_short_notice_cases_without_dst() -> None:
    received = datetime(2027, 6, 1, 9, 0, tzinfo=UTC)  # 12:00 Athens (UTC+3)
    assert short_notice(d(6, 10), received) is None
    assert short_notice(d(6, 2), received) is not None  # 27 h ahead
    assert short_notice(d(6, 1), received) is not None  # same day
    assert short_notice(d(5, 30), received) is not None  # already past


def test_short_notice_rejects_naive_times() -> None:
    naive = datetime(2027, 6, 1, 9, 0)  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="timezone-aware"):
        short_notice(d(6, 10), naive)


def test_naive_wall_clock_subtraction_would_be_wrong() -> None:
    # Documents the trap: same tzinfo -> Python subtracts wall-clock times.
    arrival = athens(10, 31, 15, 0)
    received = athens(10, 29, 16, 0)
    assert arrival - received == timedelta(hours=47)  # wall clock: looks short
    assert arrival.astimezone(UTC) - received.astimezone(UTC) == timedelta(hours=48)


# --- GROUP (D31) ---


@pytest.mark.parametrize(("rooms", "is_group"), [(1, False), (2, False), (3, True), (5, True)])
def test_group_from_three_rooms(rooms: int, is_group: bool) -> None:
    result = group(rooms)
    assert (result is not None and result.reason == ReasonCode.GROUP) is is_group
