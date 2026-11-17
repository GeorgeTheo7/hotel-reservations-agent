"""The router (D70): one test per rule, one per pair of neighbouring rules (which wins),
and the agreement of rules 10-12 with build_offer."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from anemos.db.seed import hotel_data
from anemos.extraction import ExtractionOutput
from anemos.reservations.datespec import Window
from anemos.reservations.errors import InvalidDates, NeedsEscalation, NeedsInfo, ReasonCode
from anemos.reservations.model import Party, RoomRequest, Stay
from anemos.reservations.offer import Offer, build_offer
from anemos.routing import (
    EmailFacts,
    Escalate,
    Resolved,
    Routed,
    SetAside,
    ToAgent,
    candidate_nights,
    route,
)

pytestmark = pytest.mark.unit

HOTEL = hotel_data()
RECEIVED = datetime(2027, 1, 10, 9, 0, tzinfo=UTC)
JUNE = Stay(date(2027, 6, 8), date(2027, 6, 11))  # Mid season, open, min stay 2


def ext(**fields: Any) -> ExtractionOutput:
    base: dict[str, Any] = {
        "kind": "request", "language": "en", "script": "latin", "flags": [],
        "date_spec": None, "adults": 2, "children": None, "child_ages": [], "rooms": 1,
        "room_type": None, "arrival_time": None,
    }  # fmt: skip
    return ExtractionOutput.model_validate(base | fields)


def run(
    extraction: ExtractionOutput | None,
    resolved: Resolved = JUNE,
    *,
    received: datetime = RECEIVED,
    in_reply_to: str | None = None,
) -> Routed:
    return route(EmailFacts(received, in_reply_to), extraction, resolved, HOTEL)


def codes(result: Routed) -> tuple[ReasonCode, ...]:
    assert isinstance(result, Escalate), result
    return result.codes


def agent_route(result: Routed) -> str:
    assert isinstance(result, ToAgent), result
    return result.route


# --- 1. thread reply ---


def test_thread_reply_escalates_without_an_extraction() -> None:
    assert codes(run(None, None, in_reply_to="<x@example.test>")) == (ReasonCode.THREAD_REPLY,)


def test_thread_reply_wins_over_anything_extracted() -> None:
    spam = ext(kind="spam", flags=["injection_suspected"])
    assert codes(run(spam, in_reply_to="<x@example.test>")) == (ReasonCode.THREAD_REPLY,)


def test_no_extraction_is_a_bug_unless_thread_reply() -> None:
    with pytest.raises(ValueError, match="thread reply"):
        run(None)


# --- 2-6. the email as a whole ---


def test_injection_escalates() -> None:
    assert codes(run(ext(flags=["injection_suspected"]))) == (ReasonCode.INJECTION,)


def test_injection_wins_over_spam() -> None:
    result = run(ext(kind="spam", flags=["injection_suspected"]))
    assert codes(result) == (ReasonCode.INJECTION,)


def test_spam_is_set_aside() -> None:
    assert run(ext(kind="spam", adults=None, rooms=None), None) == SetAside()


def test_spam_wins_over_language() -> None:
    assert run(ext(kind="spam", language="de"), None) == SetAside()


@pytest.mark.parametrize("language", ["de", "fr", "it"])
def test_other_languages_escalate(language: str) -> None:
    assert codes(run(ext(language=language))) == (ReasonCode.LANGUAGE,)


def test_language_wins_over_flags() -> None:
    assert codes(run(ext(language="de", flags=["legal_threat"]))) == (ReasonCode.LANGUAGE,)


@pytest.mark.parametrize(
    ("flag", "code"),
    [
        ("legal_threat", ReasonCode.LEGAL),
        ("billing_request", ReasonCode.BILLING),
        ("medical_need", ReasonCode.MEDICAL),
    ],
)
def test_each_flag_escalates_with_its_code(flag: str, code: ReasonCode) -> None:
    assert codes(run(ext(flags=[flag]))) == (code,)


def test_all_flags_go_into_one_escalation_in_a_fixed_order() -> None:
    flags = ["medical_need", "legal_threat", "billing_request"]
    assert codes(run(ext(flags=flags))) == (
        ReasonCode.LEGAL, ReasonCode.BILLING, ReasonCode.MEDICAL,
    )  # fmt: skip


def test_accessibility_flag_alone_does_not_escalate() -> None:
    assert agent_route(run(ext(flags=["accessibility_need"]))) == "quote"


def test_flags_win_over_kind_other() -> None:
    result = run(ext(kind="other", flags=["billing_request"], adults=None, rooms=None), None)
    assert codes(result) == (ReasonCode.BILLING,)


@pytest.mark.parametrize(("kind", "agent_route"), [("question", "policy"), ("other", "other")])
def test_question_and_other_go_to_the_agent(kind: str, agent_route: str) -> None:
    """D70 rule 6: a policy question gets the policy tools; kind other only escalate."""
    assert run(ext(kind=kind, adults=None, rooms=None), None) == ToAgent(agent_route)  # type: ignore[arg-type]


def test_kind_other_wins_over_the_calendar() -> None:
    no_calendar = Stay(date(2028, 7, 10), date(2028, 7, 14))
    assert run(ext(kind="other"), no_calendar) == ToAgent("other")


# --- 7. NO_CALENDAR ---


def test_stay_in_unseeded_year_escalates_no_calendar() -> None:
    result = run(ext(), Stay(date(2028, 7, 10), date(2028, 7, 17)))
    assert codes(result) == (ReasonCode.NO_CALENDAR,)


def test_window_reaching_unseeded_year_escalates_no_calendar() -> None:
    result = run(ext(), Window(date(2027, 12, 29), date(2027, 12, 31), 3))
    assert codes(result) == (ReasonCode.NO_CALENDAR,)


def test_no_calendar_wins_over_closed() -> None:
    # Every 2027 night here is closed, but 2028 has no calendar: "closed" would be a guess.
    result = run(ext(), Stay(date(2027, 12, 30), date(2028, 1, 2)))
    assert codes(result) == (ReasonCode.NO_CALENDAR,)


# --- 8. CLOSED ---


def test_closed_stay_declines() -> None:
    assert agent_route(run(ext(), Stay(date(2027, 11, 5), date(2027, 11, 8)))) == "decline"


def test_partly_closed_stay_quotes_its_open_part() -> None:
    assert agent_route(run(ext(), Stay(date(2027, 4, 12), date(2027, 4, 17)))) == "quote"


def test_closed_window_declines() -> None:
    western_easter_2027 = Window(date(2027, 3, 25), date(2027, 3, 31), 3)
    assert agent_route(run(ext(), western_easter_2027)) == "decline"


def test_partly_closed_window_quotes() -> None:
    # Check-ins 10-14 Apr for 3 nights reach 15-16 Apr, which are open.
    assert agent_route(run(ext(), Window(date(2027, 4, 10), date(2027, 4, 14), 3))) == "quote"


def test_window_nights_run_from_first_check_in_to_last_check_out() -> None:
    nights = candidate_nights(Window(date(2027, 6, 1), date(2027, 6, 3), 2))
    assert nights == tuple(date(2027, 6, 1) + timedelta(days=i) for i in range(4))


def test_closed_wins_over_short_notice() -> None:
    received = datetime(2027, 11, 2, 9, 0, tzinfo=UTC)
    result = run(ext(), Stay(date(2027, 11, 3), date(2027, 11, 5)), received=received)
    assert agent_route(result) == "decline"


# --- 9. SHORT_NOTICE ---

# Check-in 10 Jun 2027 is 15:00 Athens = 12:00 UTC (summer time, UTC+3).
ARRIVAL_10_JUNE = Stay(date(2027, 6, 10), date(2027, 6, 13))
FORTY_EIGHT_HOURS_BEFORE = datetime(2027, 6, 8, 12, 0, tzinfo=UTC)


def test_exactly_48_hours_is_not_short_notice() -> None:
    result = run(ext(), ARRIVAL_10_JUNE, received=FORTY_EIGHT_HOURS_BEFORE)
    assert agent_route(result) == "quote"


def test_one_minute_less_is_short_notice() -> None:
    received = FORTY_EIGHT_HOURS_BEFORE + timedelta(minutes=1)
    assert codes(run(ext(), ARRIVAL_10_JUNE, received=received)) == (ReasonCode.SHORT_NOTICE,)


def test_a_window_is_never_short_notice_here() -> None:
    received = datetime(2027, 6, 20, 9, 0, tzinfo=UTC)
    window = Window(date(2027, 6, 21), date(2027, 6, 30), 3)  # first check-in tomorrow
    assert agent_route(run(ext(), window, received=received)) == "quote"


def test_short_notice_wins_over_group() -> None:
    received = FORTY_EIGHT_HOURS_BEFORE + timedelta(hours=1)
    result = run(ext(rooms=3, adults=6), ARRIVAL_10_JUNE, received=received)
    assert codes(result) == (ReasonCode.SHORT_NOTICE,)


# --- 10. GROUP ---


def test_three_rooms_is_a_group() -> None:
    assert codes(run(ext(rooms=3, adults=6))) == (ReasonCode.GROUP,)


def test_no_plan_in_two_rooms_is_a_group() -> None:
    assert codes(run(ext(adults=9, rooms=None))) == (ReasonCode.GROUP,)


def test_eight_adults_fit_two_suites() -> None:
    assert agent_route(run(ext(adults=8, rooms=None))) == "quote"


def test_group_applies_without_dates() -> None:
    assert codes(run(ext(rooms=5, adults=10), None)) == (ReasonCode.GROUP,)


def test_group_applies_without_children_ages() -> None:
    # 3 rooms is a group whatever the party; the ages would be asked otherwise.
    assert codes(run(ext(rooms=3, adults=3, children=2, child_ages=[]))) == (ReasonCode.GROUP,)


def test_group_wins_over_unaccompanied_children() -> None:
    nine_children = ext(adults=0, children=9, child_ages=[10] * 9, rooms=None)
    assert codes(run(nine_children)) == (ReasonCode.GROUP,)


# --- 11. UNACCOMPANIED_CHILDREN ---


def test_children_only_party_escalates() -> None:
    teens = ext(adults=0, children=2, child_ages=[16, 17])
    assert codes(run(teens)) == (ReasonCode.UNACCOMPANIED_CHILDREN,)


def test_fewer_adults_than_rooms_asked_escalates() -> None:
    result = run(ext(adults=1, children=1, child_ages=[12], rooms=2))
    assert codes(result) == (ReasonCode.UNACCOMPANIED_CHILDREN,)


def test_fewer_adults_than_rooms_of_the_plan_escalates() -> None:
    # 5 guests fit no single room: the plan has 2 rooms, but there is one adult.
    result = run(ext(adults=1, children=4, child_ages=[10, 10, 10, 10], rooms=None))
    assert codes(result) == (ReasonCode.UNACCOMPANIED_CHILDREN,)


def test_unaccompanied_applies_without_children_ages() -> None:
    no_ages = ext(adults=0, children=2, child_ages=[])
    assert codes(run(no_ages)) == (ReasonCode.UNACCOMPANIED_CHILDREN,)


def test_unaccompanied_wins_over_accessibility() -> None:
    result = run(ext(adults=1, rooms=2, flags=["accessibility_need"]))
    assert codes(result) == (ReasonCode.UNACCOMPANIED_CHILDREN,)


# --- 12. ACCESSIBILITY ---


def test_need_and_party_of_three_escalates() -> None:
    result = run(ext(adults=3, flags=["accessibility_need"]))
    assert codes(result) == (ReasonCode.ACCESSIBILITY,)


def test_need_and_party_of_two_quotes() -> None:
    assert agent_route(run(ext(adults=2, flags=["accessibility_need"]))) == "quote"


def test_infant_in_a_cot_does_not_count_for_the_accessible_room() -> None:
    party = ext(adults=2, children=1, child_ages=[0], flags=["accessibility_need"])
    assert agent_route(run(party)) == "quote"


def test_need_and_two_rooms_escalates() -> None:
    result = run(ext(adults=2, rooms=2, flags=["accessibility_need"]))
    assert codes(result) == (ReasonCode.ACCESSIBILITY,)


def test_need_with_a_large_party_is_accessibility_not_group() -> None:
    result = run(ext(adults=9, rooms=None, flags=["accessibility_need"]))
    assert codes(result) == (ReasonCode.ACCESSIBILITY,)


def test_accessibility_wins_over_late_arrival() -> None:
    result = run(ext(adults=3, flags=["accessibility_need"], arrival_time="23:00"))
    assert codes(result) == (ReasonCode.ACCESSIBILITY,)


# --- 13. LATE_ARRIVAL ---


@pytest.mark.parametrize(
    ("arrival_time", "late"),
    [("22:00", False), ("22:01", True), ("00:30", True), ("05:59", True), ("06:00", False)],
)
def test_late_arrival(arrival_time: str, late: bool) -> None:
    result = run(ext(arrival_time=arrival_time))
    if late:
        assert codes(result) == (ReasonCode.LATE_ARRIVAL,)
    else:
        assert agent_route(result) == "quote"


def test_late_arrival_wins_over_ask_info() -> None:
    assert codes(run(ext(arrival_time="23:30"), None)) == (ReasonCode.LATE_ARRIVAL,)


# --- 14. ask_info or quote ---


@pytest.mark.parametrize(
    ("extraction", "resolved", "missing"),
    [
        (ext(), None, "dates of the stay"),
        (ext(), NeedsInfo("length of stay (number of nights)"), "length of stay"),
        (ext(), InvalidDates("31/2/2027 does not exist"), "31/2/2027"),
        (ext(adults=None), JUNE, "number of adults"),
        (ext(children=2, child_ages=[5]), JUNE, "children's ages"),
    ],
)
def test_anything_missing_asks(
    extraction: ExtractionOutput, resolved: Resolved, missing: str
) -> None:
    result = run(extraction, resolved)
    assert isinstance(result, ToAgent) and result.route == "ask_info"
    assert missing in result.detail


def test_missing_dates_are_asked_before_missing_ages() -> None:
    result = run(ext(children=1, child_ages=[]), None)
    assert isinstance(result, ToAgent) and result.detail == "dates of the stay"


@pytest.mark.parametrize(
    "resolved", [JUNE, Window(date(2027, 6, 21), date(2027, 6, 30), 7)], ids=["stay", "window"]
)
def test_complete_request_quotes(resolved: Stay | Window) -> None:
    assert run(ext(), resolved) == ToAgent("quote")


# --- rules 10-12 agree with build_offer ---

PARTIES = [
    (adults, ages)
    for adults in range(4)
    for ages in [(), (0,), (5,), (10, 10), (16, 17), (10, 10, 10), (3, 5, 7, 9, 11)]
    if adults + len(ages) > 0
]


def _offer_code(adults: int, ages: tuple[int, ...], rooms: int, room_type: str | None,
                need: bool) -> ReasonCode | None:  # fmt: skip
    result = build_offer(
        HOTEL, RoomRequest(JUNE, rooms, room_type), Party.of(adults, ages),
        accessibility_need=need,
    )  # fmt: skip
    if isinstance(result, NeedsEscalation):
        return result.reason
    assert isinstance(result, Offer), result
    return None


@pytest.mark.parametrize(("adults", "ages"), PARTIES)
def test_room_rules_agree_with_build_offer(adults: int, ages: tuple[int, ...]) -> None:
    for rooms in (1, 2, 3):
        for room_type in (None, "standard", "sea_view"):
            for need in (False, True):
                flags = ["accessibility_need"] if need else []
                result = run(ext(adults=adults, children=len(ages), child_ages=list(ages),
                                 rooms=rooms, room_type=room_type, flags=flags))  # fmt: skip
                got = result.codes[0] if isinstance(result, Escalate) else None
                expected = _offer_code(adults, ages, rooms, room_type, need)
                assert got == expected, (rooms, room_type, need, result)
