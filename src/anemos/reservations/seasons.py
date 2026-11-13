"""Season calendar: which season a night belongs to, or whether the hotel is closed.

`season_periods` is the hotel's only calendar: a night in no period is closed.
"""

from datetime import date

from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import Season


def season_of(hotel: HotelData, night: date) -> Season | None:
    """The season of the night of `night`, or None if the hotel is closed that night."""
    for period in hotel.season_periods:
        if period.contains(night):
            return period.season
    return None


def is_open(hotel: HotelData, night: date) -> bool:
    return season_of(hotel, night) is not None
