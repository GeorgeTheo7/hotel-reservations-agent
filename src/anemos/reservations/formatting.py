"""Guest-facing text for amounts, dates, parties, nights and rooms, in EN and EL.

Own tables instead of the `locale` module: locale depends on what the operating
system has installed (Windows and Linux differ) and is global state. Greek dates use
the month in the genitive ("11 Ιουνίου"), as Greeks write them. Integer-only (D14).
"""

from datetime import date, datetime
from typing import Literal

from anemos.reservations.hotel_time import HOTEL_TZ, require_aware
from anemos.reservations.model import Adult, Child, Party, RoomType, Stay
from anemos.reservations.money import Cents
from anemos.reservations.occupancy import is_infant

type Lang = Literal["en", "el"]

NBSP = "\N{NO-BREAK SPACE}"  # keeps amounts like "1.260,00 EUR" on one line
EN_DASH = "\N{EN DASH}"  # between check-in and check-out

WEEKDAYS: dict[Lang, tuple[str, ...]] = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "el": ("Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"),
}
MONTHS: dict[Lang, tuple[str, ...]] = {
    "en": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    # Genitive case, as used in a date: "11 Ιουνίου 2027".
    "el": (
        "Ιανουαρίου", "Φεβρουαρίου", "Μαρτίου", "Απριλίου", "Μαΐου", "Ιουνίου",
        "Ιουλίου", "Αυγούστου", "Σεπτεμβρίου", "Οκτωβρίου", "Νοεμβρίου", "Δεκεμβρίου",
    ),
}  # fmt: skip

# (singular, plural) for counted nouns.
NOUNS: dict[Lang, dict[str, tuple[str, str]]] = {
    "en": {
        "adult": ("adult", "adults"),
        "child": ("child", "children"),
        "infant": ("infant", "infants"),
        "night": ("night", "nights"),
        "room": ("room", "rooms"),
    },
    "el": {
        "adult": ("ενήλικας", "ενήλικες"),
        "child": ("παιδί", "παιδιά"),
        "infant": ("βρέφος", "βρέφη"),
        "night": ("νύχτα", "νύχτες"),
        "room": ("δωμάτιο", "δωμάτια"),
    },
}
AND: dict[Lang, str] = {"en": "and", "el": "και"}


def format_money(cents: Cents, lang: Lang) -> str:
    """15_000 -> "€150.00" / "150,00 €"; 126_000 -> "€1,260.00" / "1.260,00 €"."""
    if type(cents) is not int or cents < 0:
        raise ValueError(f"amount must be non-negative int cents, got {cents!r}")
    euros, rest = divmod(cents, 100)
    grouped = f"{euros:,}"  # thousands with ',' (integer formatting, no float)
    if lang == "en":
        return f"€{grouped}.{rest:02d}"
    return f"{grouped.replace(',', '.')},{rest:02d}{NBSP}€"


def format_date(day: date, lang: Lang) -> str:
    """Full date with weekday (guardrail 3): "Friday 11 June 2027" /
    "Παρασκευή 11 Ιουνίου 2027"."""
    weekday = WEEKDAYS[lang][day.weekday()]
    month = MONTHS[lang][day.month - 1]
    return f"{weekday} {day.day} {month} {day.year}"


def format_stay(stay: Stay, lang: Lang) -> str:
    """Check-in to check-out, both in full, plus the night count."""
    nights = count(stay.night_count, "night", lang)
    check_in, check_out = format_date(stay.check_in, lang), format_date(stay.check_out, lang)
    return f"{check_in} {EN_DASH} {check_out} ({nights})"


def format_hold_until(at: datetime, lang: Lang) -> str:
    """Hold expiry in hotel time, with the zone named:
    "Sunday 13 June 2027 at 09:30 (Greece time)" /
    "Κυριακή 13 Ιουνίου 2027, στις 09:30 (ώρα Ελλάδας)"."""
    require_aware(at, "at")
    local = at.astimezone(HOTEL_TZ)
    clock = f"{local.hour:02d}:{local.minute:02d}"
    day = format_date(local.date(), lang)
    if lang == "en":
        return f"{day} at {clock} (Greece time)"
    return f"{day}, στις {clock} (ώρα Ελλάδας)"


def room_type_name(room_type: RoomType, lang: Lang) -> str:
    """Name from the database: "Sea View Double" / "Δίκλινο με θέα θάλασσα"."""
    return room_type.name if lang == "en" else room_type.name_el


def count(n: int, noun: str, lang: Lang) -> str:
    """count(1, "night", "en") -> "1 night"; count(3, "night", "el") -> "3 νύχτες"."""
    singular, plural = NOUNS[lang][noun]
    return f"{n} {singular if n == 1 else plural}"


def format_party(party: Party, lang: Lang) -> str:
    """Adults, then children (2-17, oldest first), then infants (0-1, D13):
    "2 adults, 1 child (age 8) and 1 infant (age 1)" /
    "2 ενήλικες, 1 παιδί (8 ετών) και 1 βρέφος (1 έτους)".
    The free cot is mentioned by the quote block, not here.
    """
    adults = sum(1 for g in party.guests if isinstance(g, Adult))
    children = sorted(
        (g.age for g in party.guests if isinstance(g, Child) and not is_infant(g)), reverse=True
    )
    infants = sorted(
        (g.age for g in party.guests if isinstance(g, Child) and is_infant(g)), reverse=True
    )
    parts = []
    if adults:
        parts.append(count(adults, "adult", lang))
    if children:
        parts.append(f"{count(len(children), 'child', lang)} ({_child_ages(children, lang)})")
    if infants:
        parts.append(f"{count(len(infants), 'infant', lang)} ({_infant_ages(infants, lang)})")
    return _join(parts, lang)


def _child_ages(ages: list[int], lang: Lang) -> str:
    """Ages 2-17: "age 8" / "ages 13 and 6"; Greek "8 ετών" / "13 και 6 ετών"."""
    listed = _join([str(a) for a in ages], lang)
    if lang == "en":
        return f"{'age' if len(ages) == 1 else 'ages'} {listed}"
    return f"{listed} ετών"


INFANT_AGE_TEXT: dict[Lang, dict[int, str]] = {
    "en": {1: "age 1", 0: "under 1"},
    "el": {1: "1 έτους", 0: "κάτω του ενός έτους"},
}


def _infant_ages(ages: list[int], lang: Lang) -> str:
    """Infants are 0 or 1, so each distinct age is named once: twins under 1 ->
    "under 1"; one of each -> "age 1 and under 1"."""
    distinct = sorted(set(ages), reverse=True)
    return _join([INFANT_AGE_TEXT[lang][a] for a in distinct], lang)


def _join(items: list[str], lang: Lang) -> str:
    """["16", "12", "4"] -> "16, 12 and 4" / "16, 12 και 4"."""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {AND[lang]} {items[-1]}"
