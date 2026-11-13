"""Render {{QUOTE_BLOCK}} and {{REQUEST_SUMMARY}} (D8): every number and date the guest
sees comes from here, never from the model.

All wording lives in templates/: quote_*.txt and summary_*.txt for the layout,
phrases_*.json for the sentences built from parts (string.Template, `$name`
placeholders). The owner can change wording without touching code. Nights with the same
season and rate are grouped into one line; the Quote keeps the nightly lines.
{{HOLD_UNTIL}} is left in place for the approval step to fill (D8).
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from importlib.resources import files
from itertools import groupby
from string import Template

from anemos.reservations.datespec import Window
from anemos.reservations.formatting import (
    Lang,
    count,
    format_date,
    format_hold_until,
    format_money,
    format_party,
    format_stay,
    room_type_name,
)
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.hotel_time import HOTEL_TZ
from anemos.reservations.model import ExtraBedBand, Party, RoomRequest
from anemos.reservations.money import Cents
from anemos.reservations.occupancy import is_infant
from anemos.reservations.pricing import Quote

TIMES = f" {chr(0x00D7)} "  # " x " with the multiplication sign


# --- why the offer differs from the request (rendered as a note in the block) ---


@dataclass(frozen=True)
class MinStayChange:
    arrival: date
    min_nights: int


@dataclass(frozen=True)
class OpenPartChange:
    pass


@dataclass(frozen=True)
class RoomTypeChange:
    requested_type_id: str
    max_people: int


type OfferChange = MinStayChange | OpenPartChange | RoomTypeChange


def render_quote_block(
    hotel: HotelData,
    quote: Quote,
    lang: Lang,
    *,
    hold: bool,
    change: OfferChange | None = None,
) -> str:
    phrases = _phrases(lang)
    room_type = hotel.room_types[quote.room_type_id]
    values = {
        "heading": f"{room_type_name(room_type, lang)}, {count(quote.rooms, 'room', lang)}",
        "dates": format_stay(quote.stay, lang),
        "party": format_party(quote.party, lang),
        "note": "" if change is None else _note(hotel, quote, change, lang),
        "lines": "\n".join([*_room_lines(quote, lang), *_extra_bed_lines(hotel, quote, lang)]),
        "subtotal": format_money(quote.accommodation_subtotal_cents, lang),
        "city_tax": format_money(quote.city_tax_cents, lang),
        # A held quote has a 48-hour deadline; a quote without a hold does not.
        "deposit": _phrase(
            lang,
            "deposit_held" if hold else "deposit_quote",
            amount=format_money(quote.deposit_cents, lang),
        ),
        "cancel_until": _with_article(quote.free_cancellation_until, lang),
        "cot": _cot_line(quote.party, lang),
        "hold": str(phrases["hold_one" if quote.rooms == 1 else "hold_many"])
        if hold
        else str(phrases["no_hold"]),
    }
    return _render_file(f"quote_{lang}.txt", values)


def render_request_summary(
    hotel: HotelData, request: RoomRequest | Window, party: Party, lang: Lang
) -> str:
    """What the guest asked for, with dates resolved in full (guardrail 3)."""
    if isinstance(request, RoomRequest):
        what = count(request.rooms, "room", lang)
        if request.room_type_id is not None:
            what = f"{room_type_name(hotel.room_types[request.room_type_id], lang)}, {what}"
        dates = format_stay(request.stay, lang)
    else:
        what = count(1, "room", lang)
        dates = _phrase(
            lang,
            "window",
            first=format_date(request.first_check_in, lang),
            last=format_date(request.last_check_in, lang),
            nights=count(request.nights, "night", lang),
        )
    values = {"what": what, "dates": dates, "party": format_party(party, lang)}
    return _render_file(f"summary_{lang}.txt", values)


# --- pieces ---


def _room_lines(quote: Quote, lang: Lang) -> list[str]:
    lines = []
    # Consecutive nights with the same season and rate become one line.
    for (season, rate), group in groupby(quote.lines, key=lambda n: (n.season, n.room_rate_cents)):
        nights = list(group)
        factors = [count(len(nights), "night", lang)]
        if quote.rooms > 1:
            factors.append(count(quote.rooms, "room", lang))
        factors.append(format_money(rate, lang))
        amount = Cents(sum(n.room_cents for n in nights))
        lines.append(
            _phrase(
                lang,
                "room_line",
                season=str(_phrases(lang)[f"season_{season.value}"]),
                factors=TIMES.join(factors),
                amount=format_money(amount, lang),
            )
        )
    return lines


def _extra_bed_lines(hotel: HotelData, quote: Quote, lang: Lang) -> list[str]:
    nights = len(quote.lines)
    lines = []
    # Oldest band first, like the party line.
    for band in (ExtraBedBand.AGE_12_PLUS, ExtraBedBand.CHILD_2_11):
        beds = quote.extra_bed_bands.count(band)
        if not beds:
            continue
        rate = hotel.extra_bed_rates[band]
        factors = [count(nights, "night", lang)]
        if beds > 1:
            factors.append(count(beds, "bed", lang))
        factors.append(format_money(rate, lang))
        lines.append(
            _phrase(
                lang,
                "extra_bed_one" if beds == 1 else "extra_bed_many",
                band=str(_phrases(lang)[f"band_{band.value}"]),
                factors=TIMES.join(factors),
                amount=format_money(Cents(nights * beds * rate), lang),
            )
        )
    return lines


def _cot_line(party: Party, lang: Lang) -> str:
    infants = sum(1 for g in party.guests if is_infant(g))
    if not infants:
        return ""
    key = "cot_one" if infants == 1 else "cot_many"
    return _phrase(lang, key, infants=count(infants, "infant", lang))


def _note(hotel: HotelData, quote: Quote, change: OfferChange, lang: Lang) -> str:
    match change:
        case MinStayChange(arrival, min_nights):
            return _phrase(
                lang,
                "note_min_stay",
                arrival=_with_article(arrival, lang),
                nights=count(min_nights, "night", lang),
            )
        case OpenPartChange():
            return _phrase(lang, "note_open_part")
        case RoomTypeChange(requested_type_id, max_people):
            return _phrase(
                lang,
                "note_room_type",
                asked=room_type_name(hotel.room_types[requested_type_id], lang),
                max_people=str(max_people),
                offered=room_type_name(hotel.room_types[quote.room_type_id], lang),
            )


def render_hold_until(at: datetime, lang: Lang) -> str:
    """The text that replaces {{HOLD_UNTIL}} at approval: the hold expiry in Greece
    time, with the Greek article ("την Κυριακή 13 Ιουνίου 2027, στις 09:30 ...")."""
    return f"{_article(at.astimezone(HOTEL_TZ).date(), lang)}{format_hold_until(at, lang)}"


def _with_article(day: date, lang: Lang) -> str:
    """A full date with the Greek article before the weekday; English has none."""
    return f"{_article(day, lang)}{format_date(day, lang)}"


def _article(day: date, lang: Lang) -> str:
    articles = _phrases(lang)["weekday_articles"]
    assert isinstance(articles, list)
    return str(articles[day.weekday()])


# --- template files ---


def _template_text(name: str) -> str:
    return files("anemos.reservations").joinpath("templates", name).read_text(encoding="utf-8")


@cache
def _phrases(lang: Lang) -> dict[str, object]:
    phrases: dict[str, object] = json.loads(_template_text(f"phrases_{lang}.json"))
    return phrases


def _phrase(lang: Lang, key: str, **values: str) -> str:
    return Template(str(_phrases(lang)[key])).substitute(values)


def _render_file(name: str, values: dict[str, str]) -> str:
    rendered = Template(_template_text(name)).substitute(values)
    # Optional parts (note, cot) render empty: drop the blank lines they leave.
    return "\n".join(line for line in rendered.splitlines() if line.strip())
