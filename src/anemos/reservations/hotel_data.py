"""HotelData: a read-only snapshot of the hotel's rules (room types, calendar, rates).

The pure pricing/rules code takes this as an argument instead of querying the
database, so it can be tested without Postgres. Week 2 step 10 adds the loader that
builds it from the database; unit tests use `anemos.db.seed.hotel_data()`.
Mappings are wrapped in MappingProxyType: a read-only view, so the snapshot cannot be
changed after it is built.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from types import MappingProxyType

from anemos.reservations.model import RoomType, Season
from anemos.reservations.money import Cents


@dataclass(frozen=True)
class SeasonPeriod:
    """Nights [first_night, end) belong to `season`. A night in no period = closed."""

    season: Season
    first_night: date
    end: date

    def __post_init__(self) -> None:
        _check_range(self.first_night, self.end)

    def contains(self, night: date) -> bool:
        return self.first_night <= night < self.end


@dataclass(frozen=True)
class MinStayRule:
    """Arrivals in [first_arrival, end) must stay at least `min_nights`."""

    first_arrival: date
    end: date
    min_nights: int

    def __post_init__(self) -> None:
        _check_range(self.first_arrival, self.end)
        if self.min_nights < 1:
            raise ValueError(f"min_nights must be >= 1, got {self.min_nights}")


@dataclass(frozen=True)
class HotelData:
    room_types: Mapping[str, RoomType]
    season_periods: tuple[SeasonPeriod, ...]
    city_tax_cents: Mapping[Season, Cents]  # per room-night
    rates: Mapping[tuple[str, Season], Cents]  # (room type id, season) -> per room-night
    min_stay_rules: tuple[MinStayRule, ...]

    def __post_init__(self) -> None:
        # Same rules as the database constraints, so a hand-built snapshot is as safe.
        _check_no_overlap([(p.first_night, p.end) for p in self.season_periods], "season")
        _check_no_overlap([(r.first_arrival, r.end) for r in self.min_stay_rules], "min-stay")
        for type_id, room_type in self.room_types.items():
            if type_id != room_type.id:
                raise ValueError(f"room type key {type_id!r} != id {room_type.id!r}")
        for (type_id, _), cents in self.rates.items():
            if type_id not in self.room_types:
                raise ValueError(f"rate for unknown room type {type_id!r}")
            _check_cents(cents, minimum=1)
        for cents in self.city_tax_cents.values():
            _check_cents(cents, minimum=0)
        # Freeze the mappings (frozen=True only stops reassigning the fields).
        for name in ("room_types", "city_tax_cents", "rates"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        ordered = tuple(sorted(self.season_periods, key=lambda p: p.first_night))
        object.__setattr__(self, "season_periods", ordered)

    def rate(self, room_type_id: str, season: Season) -> Cents | None:
        """None = no rate for this type and season (NO_RATE, week 3)."""
        return self.rates.get((room_type_id, season))


def _check_range(start: date, end: date) -> None:
    if end <= start:
        raise ValueError(f"empty range: {start} to {end}")


def _check_no_overlap(ranges: list[tuple[date, date]], what: str) -> None:
    ordered = sorted(ranges)
    for (_, previous_end), (start, _) in pairwise(ordered):
        if start < previous_end:
            raise ValueError(f"overlapping {what} ranges at {start}")


def _check_cents(amount: int, *, minimum: int) -> None:
    if type(amount) is not int or amount < minimum:
        raise ValueError(f"amount must be int cents >= {minimum}, got {amount!r}")
