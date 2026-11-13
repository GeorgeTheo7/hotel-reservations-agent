"""Domain types: seasons, room types, guests, parties and stays. No database.

All types are frozen dataclasses (immutable records). Their constructors check their
own invariants, so a `Stay` or `Child` that exists is always valid: code that receives
one does not need to check it again ("parse, don't validate").
"""

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Literal

from anemos.reservations.errors import InvalidDates


class Season(StrEnum):
    """Values match `seasons.id` in the database."""

    LOW = "low"
    MID = "mid"
    HIGH = "high"


class ExtraBedBand(StrEnum):
    """Extra-bed price band by age. Values match `extra_bed_rates.band` in the database;
    the age bounds live in `occupancy.extra_bed_band`."""

    CHILD_2_11 = "child_2_11"
    AGE_12_PLUS = "age_12_plus"


# Values match the room_types_extra_beds_valid constraint in the database.
ExtraBeds = Literal["cot_only", "extra_bed", "sofa_beds"]


@dataclass(frozen=True)
class RoomType:
    id: str
    name: str
    standard_occupancy: int
    max_occupancy: int
    extra_beds: ExtraBeds
    # Accessible room: offered without a stated need only if nothing else fits.
    offer_only_if_needed: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.standard_occupancy <= self.max_occupancy:
            raise ValueError(
                f"{self.id}: need 1 <= standard ({self.standard_occupancy})"
                f" <= max ({self.max_occupancy})"
            )


@dataclass(frozen=True)
class Adult:
    """An adult guest. Age is not asked; for extra beds an adult counts as 12+."""


MAX_CHILD_AGE = 17


@dataclass(frozen=True)
class Child:
    """A guest under 18 with a known age. Under 2 = infant in a cot (D13)."""

    age: int

    def __post_init__(self) -> None:
        if type(self.age) is not int or not 0 <= self.age <= MAX_CHILD_AGE:
            raise ValueError(f"child age must be an int 0-{MAX_CHILD_AGE}, got {self.age!r}")


type Guest = Adult | Child


@dataclass(frozen=True)
class Party:
    """Everyone in the request, across all rooms."""

    guests: tuple[Guest, ...]

    def __post_init__(self) -> None:
        if not self.guests:
            raise ValueError("a party needs at least one guest")

    @classmethod
    def of(cls, adults: int, child_ages: tuple[int, ...] = ()) -> "Party":
        """Party.of(2, (13, 6)) = 2 adults + children aged 13 and 6."""
        return cls((*(Adult() for _ in range(adults)), *(Child(age) for age in child_ages)))


@dataclass(frozen=True)
class Stay:
    """Nights from check_in up to, not including, check_out: half-open [check_in, check_out).

    Matches the database `daterange` and makes stays easy to compare: a stay ending on
    the 5th and one starting on the 5th do not overlap (check-out morning, check-in
    afternoon).
    """

    check_in: date
    check_out: date

    def __post_init__(self) -> None:
        # datetime is a subclass of date; a datetime here would be a bug (D23: stay
        # dates are hotel calendar dates, not timestamps).
        for name, value in (("check_in", self.check_in), ("check_out", self.check_out)):
            if type(value) is not date:
                raise TypeError(f"{name} must be a date, got {type(value).__name__}")
        if self.check_out <= self.check_in:
            raise ValueError(f"check-out {self.check_out} is not after check-in {self.check_in}")

    @property
    def night_count(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def nights(self) -> tuple[date, ...]:
        """The date of each night slept, e.g. 12-16 Jul -> 12, 13, 14, 15 Jul."""
        return tuple(self.check_in + timedelta(days=i) for i in range(self.night_count))


def stay_or_error(check_in: date, check_out: date) -> Stay | InvalidDates:
    """Build a Stay from untrusted dates, returning INVALID_DATES instead of raising."""
    if check_out == check_in:
        return InvalidDates("check-out is the same day as check-in (0 nights)")
    if check_out < check_in:
        return InvalidDates("check-out is before check-in")
    return Stay(check_in, check_out)
