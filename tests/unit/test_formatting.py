from datetime import UTC, date, datetime

import pytest

from anemos.db.seed import hotel_data
from anemos.reservations.formatting import (
    EN_DASH,
    NBSP,
    count,
    format_date,
    format_hold_until,
    format_money,
    format_party,
    format_stay,
    room_type_name,
)
from anemos.reservations.model import Party, Stay
from anemos.reservations.money import Cents

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("cents", "en", "el"),
    [
        (0, "€0.00", "0,00 €"),
        (150, "€1.50", "1,50 €"),  # Ex 1 city tax
        (7_000, "€70.00", "70,00 €"),
        (8_600, "€86.00", "86,00 €"),  # Ex 5 deposit
        (126_000, "€1,260.00", "1.260,00 €"),  # Ex 6 subtotal
        (123_456_789, "€1,234,567.89", "1.234.567,89 €"),
    ],
)
def test_money(cents: int, en: str, el: str) -> None:
    assert format_money(Cents(cents), "en") == en
    assert format_money(Cents(cents), "el").replace(NBSP, " ") == el


def test_greek_euro_sign_never_breaks_onto_its_own_line() -> None:
    assert format_money(Cents(150), "el") == f"1,50{NBSP}€"


@pytest.mark.parametrize("bad", [-1, 1.5, True])
def test_money_rejects_bad_amounts(bad: object) -> None:
    with pytest.raises(ValueError, match="int cents"):
        format_money(bad, "en")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("day", "en", "el"),
    [
        (date(2027, 6, 11), "Friday 11 June 2027", "Παρασκευή 11 Ιουνίου 2027"),
        (date(2027, 5, 2), "Sunday 2 May 2027", "Κυριακή 2 Μαΐου 2027"),  # Orthodox Easter
        (date(2027, 8, 15), "Sunday 15 August 2027", "Κυριακή 15 Αυγούστου 2027"),
        (date(2027, 10, 31), "Sunday 31 October 2027", "Κυριακή 31 Οκτωβρίου 2027"),
        (date(2027, 4, 15), "Thursday 15 April 2027", "Πέμπτη 15 Απριλίου 2027"),
    ],
)
def test_dates(day: date, en: str, el: str) -> None:
    assert format_date(day, "en") == en
    assert format_date(day, "el") == el


def test_all_weekdays_and_months_are_distinct() -> None:
    week = [date(2027, 6, d) for d in range(7, 14)]  # Monday to Sunday
    months = [date(2027, m, 1) for m in range(1, 13)]
    for lang in ("en", "el"):
        assert len({format_date(d, lang).split()[0] for d in week}) == 7
        assert len({format_date(d, lang).split()[2] for d in months}) == 12


def test_stay() -> None:
    stay = Stay(date(2027, 6, 11), date(2027, 6, 13))
    assert (
        format_stay(stay, "en") == f"Friday 11 June 2027 {EN_DASH} Sunday 13 June 2027 (2 nights)"
    )
    assert format_stay(stay, "el") == (
        f"Παρασκευή 11 Ιουνίου 2027 {EN_DASH} Κυριακή 13 Ιουνίου 2027 (2 νύχτες)"
    )


def test_hold_until_is_hotel_time() -> None:
    # 06:30 UTC on 13 Jun = 09:30 in Athens (UTC+3 in summer).
    at = datetime(2027, 6, 13, 6, 30, tzinfo=UTC)
    assert format_hold_until(at, "en") == "Sunday 13 June 2027 at 09:30 (Greece time)"
    assert format_hold_until(at, "el") == "Κυριακή 13 Ιουνίου 2027, στις 09:30 (ώρα Ελλάδας)"


def test_hold_until_crosses_midnight_in_athens() -> None:
    at = datetime(2027, 6, 13, 22, 15, tzinfo=UTC)  # 01:15 on 14 Jun in Athens
    assert format_hold_until(at, "en") == "Monday 14 June 2027 at 01:15 (Greece time)"


@pytest.mark.parametrize(
    ("n", "noun", "en", "el"),
    [
        (1, "night", "1 night", "1 νύχτα"),
        (4, "night", "4 nights", "4 νύχτες"),
        (1, "room", "1 room", "1 δωμάτιο"),
        (2, "room", "2 rooms", "2 δωμάτια"),
        (1, "adult", "1 adult", "1 ενήλικας"),
        (2, "adult", "2 adults", "2 ενήλικες"),
    ],
)
def test_counts(n: int, noun: str, en: str, el: str) -> None:
    assert count(n, noun, "en") == en
    assert count(n, noun, "el") == el


@pytest.mark.parametrize(
    ("party", "en", "el"),
    [
        (Party.of(2), "2 adults", "2 ενήλικες"),
        (Party.of(1), "1 adult", "1 ενήλικας"),
        (Party.of(2, (8,)), "2 adults and 1 child (age 8)", "2 ενήλικες και 1 παιδί (8 ετών)"),
        (
            Party.of(2, (6, 13)),  # listed oldest first
            "2 adults and 2 children (ages 13 and 6)",
            "2 ενήλικες και 2 παιδιά (13 και 6 ετών)",
        ),
        (
            Party.of(2, (16, 12, 4)),  # Ex 12
            "2 adults and 3 children (ages 16, 12 and 4)",
            "2 ενήλικες και 3 παιδιά (16, 12 και 4 ετών)",
        ),
        (Party.of(2, (1,)), "2 adults and 1 infant (age 1)", "2 ενήλικες και 1 βρέφος (1 έτους)"),
        (
            Party.of(2, (0,)),
            "2 adults and 1 infant (under 1)",
            "2 ενήλικες και 1 βρέφος (κάτω του ενός έτους)",
        ),
        (
            Party.of(2, (1, 8)),  # mixed: adults, then children, then infants
            "2 adults, 1 child (age 8) and 1 infant (age 1)",
            "2 ενήλικες, 1 παιδί (8 ετών) και 1 βρέφος (1 έτους)",
        ),
        (
            Party.of(2, (0, 0)),  # twins under 1
            "2 adults and 2 infants (under 1)",
            "2 ενήλικες και 2 βρέφη (κάτω του ενός έτους)",
        ),
        (
            Party.of(1, (0, 1, 13, 4)),
            "1 adult, 2 children (ages 13 and 4) and 2 infants (age 1 and under 1)",
            "1 ενήλικας, 2 παιδιά (13 και 4 ετών) και 2 βρέφη (1 έτους και κάτω του ενός έτους)",
        ),
    ],
)
def test_party(party: Party, en: str, el: str) -> None:
    assert format_party(party, "en") == en
    assert format_party(party, "el") == el


@pytest.mark.parametrize(
    ("type_id", "en", "el"),
    [
        ("standard", "Standard Double", "Δίκλινο Standard"),
        ("sea_view", "Sea View Double", "Δίκλινο με θέα θάλασσα"),
        ("family_suite", "Family Suite", "Οικογενειακή Σουίτα"),
        ("accessible", "Accessible Double", "Προσβάσιμο Δίκλινο"),
    ],
)
def test_room_type_names(type_id: str, en: str, el: str) -> None:
    room_type = hotel_data().room_types[type_id]
    assert room_type_name(room_type, "en") == en
    assert room_type_name(room_type, "el") == el
