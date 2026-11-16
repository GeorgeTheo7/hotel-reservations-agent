"""The numbers in the policy chunks agree with the database and the code (D15, D39).

policies.md is static text. Each chunk with numbers has checks built from HotelData
(loaded from the seeded test database) or from code constants. Two directions:
- every check must match its chunk (the chunk states the right value);
- every number in a chunk must come from one of its checks (a stray or stale number
  fails, even one nobody thought to check).
A few facts exist only in SPEC (Domain data, house facts), with no row or constant to
compare against; they are listed in SPEC_ONLY by name.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, time, timedelta

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.approval import HOLD_FOR
from anemos.policies import load_policies, policy
from anemos.reservations.formatting import MONTHS, format_money
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.hotel_time import CHECK_IN_TIME
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.model import MAX_CHILD_AGE, ExtraBedBand, Season
from anemos.reservations.money import DEPOSIT_PERCENT, Cents
from anemos.reservations.occupancy import INFANT_AGE_LIMIT, OLDER_BAND_FROM_AGE
from anemos.reservations.stay_rules import (
    FREE_CANCELLATION_DAYS,
    FREE_CANCELLATION_DAYS_HIGH,
    GROUP_FROM_ROOMS,
    LAST_CHECK_IN,
)

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

NUMBER = re.compile(r"\d+(?:[.:]\d+)*")


@dataclass(frozen=True)
class Check:
    """A regex the chunk must match, and the numbers it accounts for."""

    pattern: str
    numbers: frozenset[str]


def lit(text: str) -> Check:
    """The exact text; a value and the words around it are checked together, so a value
    cannot pass by sitting next to the wrong season or band."""
    return Check(re.escape(text), frozenset(NUMBER.findall(text)))


# Facts with no database row or code constant: SPEC Domain data (policies, house facts).
SPEC_ONLY: dict[str, list[Check]] = {
    "check-in-check-out": [lit("by 11:00")],  # check-out time
    "breakfast": [lit("from 08:00 to 10:30")],
    "transfer": [lit("€25.00 per trip per car"), lit("up to 4 passengers")],
    "parking": [lit("100 m from the hotel")],
}

ROOM_CHUNKS = {
    "room-standard-double": "standard",
    "room-sea-view-double": "sea_view",
    "room-family-suite": "family_suite",
    "room-accessible-double": "accessible",
}


def hhmm(t: time) -> str:
    return f"{t:%H:%M}"


def day_month(d: date) -> str:
    return f"{d.day} {MONTHS['en'][d.month - 1]}"


def ordinal(n: int) -> str:
    """3 -> '3rd', 4 -> '4th' (room places are small numbers)."""
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n, "th")
    return f"{n}{suffix}"


def expected(hotel: HotelData) -> dict[str, list[Check]]:
    hours = int(HOLD_FOR.total_seconds() // 3600)
    checks: dict[str, list[Check]] = {
        "check-in-check-out": [lit(f"from {hhmm(CHECK_IN_TIME)} to {hhmm(LAST_CHECK_IN)}")],
        "late-arrival": [lit(f"until {hhmm(LAST_CHECK_IN)}"),
                         lit(f"later than {hhmm(LAST_CHECK_IN)}")],
        "cancellation": [lit(f"up to {FREE_CANCELLATION_DAYS} days before your arrival"),
                         lit(f"up to {FREE_CANCELLATION_DAYS_HIGH} days before arrival "
                             "for arrivals in high season"),
                         lit("the first night is charged")],
        "deposit": [lit(f"{DEPOSIT_PERCENT}% of the accommodation total"),
                    lit(f"within {hours} hours")],
        "holds-and-confirmation": [lit(f"for {hours} hours")],
        "cots-and-infants": [lit(f"children under {INFANT_AGE_LIMIT}"),
                             lit(f"child under {INFANT_AGE_LIMIT}")],
        "children": [lit(f"Children under {MAX_CHILD_AGE + 1}")],
        "groups": [lit(f"{GROUP_FROM_ROOMS} or more rooms")],
        "city-tax": city_tax(hotel),
        "extra-beds": extra_beds(hotel),
        "season-and-opening": seasons(hotel),
        "minimum-stay": minimum_stay(hotel),
        **{chunk: room(hotel, type_id) for chunk, type_id in ROOM_CHUNKS.items()},
    }  # fmt: skip
    for chunk, extra in SPEC_ONLY.items():
        checks[chunk] = checks.get(chunk, []) + extra
    return checks


def city_tax(hotel: HotelData) -> list[Check]:
    """Seasons with the same amount are named together: "€4.00 … in mid and high season"."""
    by_amount: dict[Cents, list[Season]] = {}
    for season in Season:
        by_amount.setdefault(hotel.city_tax_cents[season], []).append(season)
    return [
        lit(f"{format_money(cents, 'en')} per room per night in {' and '.join(seasons)} season")
        for cents, seasons in by_amount.items()
    ]


def extra_beds(hotel: HotelData) -> list[Check]:
    standard = {t.standard_occupancy for t in hotel.room_types.values()}
    assert len(standard) == 1, "the chunk says 'the first N guests' for every room type"
    child = format_money(hotel.extra_bed_rates[ExtraBedBand.CHILD_2_11], "en")
    older = format_money(hotel.extra_bed_rates[ExtraBedBand.AGE_12_PLUS], "en")
    return [
        lit(f"{child} per night for a child aged {INFANT_AGE_LIMIT} to {OLDER_BAND_FROM_AGE - 1}"),
        lit(f"{older} per night for a guest aged {OLDER_BAND_FROM_AGE} and over"),
        lit(f"The first {standard.pop()} guests"),
    ]


def room(hotel: HotelData, type_id: str) -> list[Check]:
    t = hotel.room_types[type_id]
    sleeps = Check(rf"sleeps? up to {t.max_occupancy} guests", frozenset({str(t.max_occupancy)}))
    checks = [lit(t.name), sleeps]
    extra = t.max_occupancy - t.standard_occupancy
    if t.extra_beds == "extra_bed":
        checks.append(lit(f"{t.standard_occupancy} in the double bed and {extra} in an extra bed"))
    elif t.extra_beds == "sofa_beds":
        places = " and ".join(ordinal(n) for n in range(t.standard_occupancy + 1,
                                                        t.max_occupancy + 1))  # fmt: skip
        checks += [lit(f"{t.standard_occupancy} in the double bed"), lit(f"the {places} guest")]
    return checks


def ranges_by_season(hotel: HotelData) -> dict[Season, list[tuple[date, date]]]:
    """Each season's periods as (first night, last night), in calendar order."""
    out: dict[Season, list[tuple[date, date]]] = {}
    for p in hotel.season_periods:
        out.setdefault(p.season, []).append((p.first_night, p.end - timedelta(days=1)))
    return out


def seasons(hotel: HotelData) -> list[Check]:
    first = hotel.season_periods[0].first_night
    last = hotel.season_periods[-1].end - timedelta(days=1)
    checks = [lit(f"open from {day_month(first)} to {day_month(last)}")]
    for season, ranges in ranges_by_season(hotel).items():
        spans = " and ".join(f"{day_month(a)} to {day_month(b)}" for a, b in ranges)
        checks.append(lit(f"{season.value.capitalize()} season is {spans}"))
    return checks


def minimum_stay(hotel: HotelData) -> list[Check]:
    """A rule that starts or ends with a season period gives that season's minimum; a
    rule strictly inside one period (10-20 Aug) is named by its own dates."""
    starts = {p.first_night: p.season for p in hotel.season_periods}
    ends = {p.end: p.season for p in hotel.season_periods}
    per_season: dict[Season, set[int]] = {}
    checks = []
    for rule in hotel.min_stay_rules:
        season = starts.get(rule.first_arrival) or ends.get(rule.end)
        if season is None:
            last = rule.end - timedelta(days=1)
            assert rule.first_arrival.month == last.month, "the chunk names one month"
            checks.append(lit(f"from {rule.first_arrival.day} to {day_month(last)} "
                              f"the minimum stay is {rule.min_nights} nights"))  # fmt: skip
        else:
            per_season.setdefault(season, set()).add(rule.min_nights)
    for season, nights in per_season.items():
        assert len(nights) == 1, f"{season}: one minimum per season outside special dates"
        n = nights.pop()
        checks.append(lit(f"{n} night{'s' if n > 1 else ''} in {season.value} season"))
    return checks


@pytest.fixture
def hotel(db: Conn) -> HotelData:
    return load_hotel_data(db)


def test_every_check_names_a_chunk(hotel: HotelData) -> None:
    ids = {c.policy_id for c in load_policies()}
    assert set(expected(hotel)) - ids == set()


def failing(text: str, checks: Iterable[Check]) -> list[str]:
    return [c.pattern for c in checks if re.search(c.pattern, text) is None]


def test_each_chunk_states_the_values_from_the_database_and_code(hotel: HotelData) -> None:
    failures = {
        chunk: missing
        for chunk, checks in expected(hotel).items()
        if (missing := failing(policy(chunk).text, checks))
    }
    assert failures == {}


def test_every_number_in_a_chunk_comes_from_a_check(hotel: HotelData) -> None:
    checks = expected(hotel)
    stray = {}
    for chunk in load_policies():
        allowed = set().union(*(c.numbers for c in checks.get(chunk.policy_id, [])))
        extra = set(NUMBER.findall(chunk.text)) - allowed
        if extra:
            stray[chunk.policy_id] = sorted(extra)
    assert stray == {}
