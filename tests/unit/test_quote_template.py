"""Quote block and request summary.

Golden files (tests/unit/golden/) hold the expected rendering, reviewed by eye once;
the tests compare exactly. To change wording on purpose: change the template, look at
the new output, then update the golden file in the same commit.
"""

import json
import re
from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path

import pytest

from anemos.db.seed import hotel_data
from anemos.reservations.datespec import Window
from anemos.reservations.formatting import Lang
from anemos.reservations.model import Party, RoomRequest, Stay
from anemos.reservations.pricing import Quote, price_stay
from anemos.reservations.quote_template import (
    MinStayChange,
    OfferChange,
    OpenPartChange,
    RoomTypeChange,
    render_hold_until,
    render_quote_block,
    render_request_summary,
)

pytestmark = pytest.mark.unit

HOTEL = hotel_data()
GOLDEN = Path(__file__).parent / "golden"
LANGS: tuple[Lang, ...] = ("en", "el")


def quote(type_id: str, stay: Stay, party: Party, rooms: int = 1) -> Quote:
    result = price_stay(HOTEL, type_id, stay, party, rooms)
    assert isinstance(result, Quote)
    return result


# name: (request, party, offered stay or None, hold, change)
CASES: dict[str, tuple[RoomRequest, Party, Stay | None, bool, OfferChange | None]] = {
    "ex02": (
        RoomRequest(Stay(date(2027, 7, 12), date(2027, 7, 16)), 1, "sea_view"),
        Party.of(2, (8,)),
        None,
        True,
        None,
    ),
    "ex06": (
        RoomRequest(Stay(date(2027, 7, 20), date(2027, 7, 24)), 1, "family_suite"),
        Party.of(2, (13, 6)),
        None,
        True,
        None,
    ),
    "ex11": (
        RoomRequest(Stay(date(2027, 8, 24), date(2027, 8, 27)), 2, "standard"),
        Party.of(4),
        None,
        True,
        None,
    ),
    "ex04": (  # min-stay version, no hold
        RoomRequest(Stay(date(2027, 8, 10), date(2027, 8, 13)), 1, "sea_view"),
        Party.of(2),
        Stay(date(2027, 8, 10), date(2027, 8, 15)),
        False,
        MinStayChange(date(2027, 8, 10), 5),
    ),
}


def rendered(name: str, lang: Lang) -> tuple[str, str, Quote]:
    request, party, offered, hold, change = CASES[name]
    assert request.room_type_id is not None
    q = quote(request.room_type_id, offered or request.stay, party, request.rooms)
    summary = render_request_summary(HOTEL, request, party, lang)
    block = render_quote_block(HOTEL, q, lang, hold=hold, change=change)
    return summary, block, q


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", list(CASES))
def test_matches_golden_file(name: str, lang: Lang) -> None:
    summary, block, _ = rendered(name, lang)
    expected = (GOLDEN / f"quote_{name}_{lang}.txt").read_text(encoding="utf-8")
    assert f"{summary}\n---\n{block}\n" == expected


# --- every amount in the block is one the code computed (seed of guardrail 1) ---

MONEY = {
    "en": re.compile(r"€([\d,]+)\.(\d{2})"),
    "el": re.compile(r"([\d.]+),(\d{2}) €"),
}


def amounts(text: str, lang: Lang) -> list[int]:
    return [
        int(whole.replace(",", "").replace(".", "")) * 100 + int(cents)
        for whole, cents in MONEY[lang].findall(text)
    ]


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", list(CASES))
def test_amounts_parse_back_to_quote_figures(name: str, lang: Lang) -> None:
    _, block, q = rendered(name, lang)
    unit_rates = {line.room_rate_cents for line in q.lines} | set(HOTEL.extra_bed_rates.values())
    line_totals = [
        amounts(line.split(" = ")[1], lang)[0] for line in block.splitlines() if " = " in line
    ]
    totals = {q.accommodation_subtotal_cents, q.city_tax_cents, q.deposit_cents}
    found = amounts(block, lang)
    assert totals <= set(found)  # subtotal, city tax and deposit are all shown
    assert set(found) <= totals | unit_rates | set(line_totals)  # nothing invented
    assert sum(line_totals) == q.accommodation_subtotal_cents  # the lines add up


@pytest.mark.parametrize("lang", LANGS)
def test_hold_placeholder_only_when_held(lang: Lang) -> None:
    _, held, _ = rendered("ex02", lang)
    _, not_held, _ = rendered("ex04", lang)
    assert "{{HOLD_UNTIL}}" in held
    assert "{{HOLD_UNTIL}}" not in not_held


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", list(CASES))
def test_no_template_leftovers(name: str, lang: Lang) -> None:
    summary, block, _ = rendered(name, lang)
    for text in (summary, block):
        assert "$" not in text
        assert "{{" not in text.replace("{{HOLD_UNTIL}}", "")


# --- smaller cases ---


def test_cot_line_for_infants() -> None:
    q = quote("standard", Stay(date(2027, 9, 1), date(2027, 9, 4)), Party.of(2, (1,)))  # Ex 5
    assert "Cot for 1 infant: free of charge." in render_quote_block(HOTEL, q, "en", hold=True)
    assert "Βρεφική κούνια για 1 βρέφος: δωρεάν." in render_quote_block(HOTEL, q, "el", hold=True)


def test_no_cot_line_without_infants() -> None:
    _, block, _ = rendered("ex02", "en")
    assert "Cot" not in block


def test_open_part_note_ex07() -> None:
    q = quote("standard", Stay(date(2027, 10, 30), date(2027, 11, 1)), Party.of(2))
    block = render_quote_block(HOTEL, q, "en", hold=False, change=OpenPartChange())
    assert "closed on some of the nights" in block


def test_room_type_note_ex08() -> None:
    q = quote("family_suite", Stay(date(2027, 6, 5), date(2027, 6, 8)), Party.of(4))
    change = RoomTypeChange("sea_view", 3)
    en = render_quote_block(HOTEL, q, "en", hold=False, change=change)
    el = render_quote_block(HOTEL, q, "el", hold=False, change=change)
    assert (
        "the Sea View Double sleeps at most 3 people, so this quote is for the Family Suite" in en
    )
    assert "«Δίκλινο με θέα θάλασσα» χωρούν έως 3 άτομα" in el
    assert "Extra beds, age 12+: 3 nights × 2 beds × €35.00 = €210.00" in en


def test_greek_weekday_article_for_saturday() -> None:
    q = quote("sea_view", Stay(date(2027, 8, 14), date(2027, 8, 19)), Party.of(2))
    change = MinStayChange(date(2027, 8, 14), 5)  # a Saturday
    assert "για άφιξη το Σάββατο 14 Αυγούστου 2027" in render_quote_block(
        HOTEL, q, "el", hold=False, change=change
    )


def test_summary_for_an_easter_window() -> None:
    window = Window(date(2027, 4, 29), date(2027, 5, 5), 4)
    en = render_request_summary(HOTEL, window, Party.of(2), "en")
    el = render_request_summary(HOTEL, window, Party.of(2), "el")
    assert en == (
        "Your request: 1 room, check-in between Thursday 29 April 2027 and"
        " Wednesday 5 May 2027, 4 nights, 2 adults."
    )
    assert el == (
        "Το αίτημά σας: 1 δωμάτιο, άφιξη από Πέμπτη 29 Απριλίου 2027 έως"
        " Τετάρτη 5 Μαΐου 2027, 4 νύχτες, 2 ενήλικες."
    )


def test_summary_without_room_type() -> None:
    request = RoomRequest(Stay(date(2027, 5, 20), date(2027, 5, 21)))
    assert render_request_summary(HOTEL, request, Party.of(2), "en") == (
        "Your request: 1 room, Thursday 20 May 2027 – Friday 21 May 2027 (1 night), 2 adults."
    )


def test_phrase_files_have_the_same_keys() -> None:
    folder = files("anemos.reservations").joinpath("templates")
    en = json.loads(folder.joinpath("phrases_en.json").read_text(encoding="utf-8"))
    el = json.loads(folder.joinpath("phrases_el.json").read_text(encoding="utf-8"))
    assert en.keys() == el.keys()


# --- wording rules ---


@pytest.mark.parametrize("lang", LANGS)
def test_no_hold_quote_has_no_48_hour_deadline(lang: Lang) -> None:
    _, not_held, _ = rendered("ex04", lang)
    _, held, _ = rendered("ex02", lang)
    assert "48" not in not_held
    assert "48" in held


def test_deposit_wording_follows_the_hold() -> None:
    _, held, _ = rendered("ex02", "en")
    _, not_held, _ = rendered("ex04", "en")
    assert "Deposit (30% of the accommodation total), due within 48 hours: €198.00" in held
    assert "Deposit to confirm a booking (30% of the accommodation total): €285.00" in not_held


@pytest.mark.parametrize(("lang", "start"), [("en", "Our offer: "), ("el", "Η προσφορά μας: ")])
def test_block_starts_with_our_offer(lang: Lang, start: str) -> None:
    _, block, _ = rendered("ex02", lang)
    assert block.startswith(start)


def test_greek_cancellation_line_has_the_article() -> None:
    _, block, _ = rendered("ex02", "el")  # 28 June 2027 is a Monday: "τη Δευτέρα"
    assert "Δωρεάν ακύρωση έως και τη Δευτέρα 28 Ιουνίου 2027" in block


def test_hold_until_text_for_approval() -> None:
    at = datetime(2027, 6, 13, 6, 30, tzinfo=UTC)  # Sunday 09:30 in Athens
    assert render_hold_until(at, "en") == "Sunday 13 June 2027 at 09:30 (Greece time)"
    assert render_hold_until(at, "el") == ("την Κυριακή 13 Ιουνίου 2027, στις 09:30 (ώρα Ελλάδας)")
    held = "Κρατάμε το δωμάτιο για εσάς έως {{HOLD_UNTIL}}."
    assert held.replace("{{HOLD_UNTIL}}", render_hold_until(at, "el")) == (
        "Κρατάμε το δωμάτιο για εσάς έως την Κυριακή 13 Ιουνίου 2027, στις 09:30 (ώρα Ελλάδας)."
    )
