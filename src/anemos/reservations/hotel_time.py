"""Hotel time (D23): timestamps are UTC; calendar rules use Europe/Athens.

Gotcha this module exists for: subtracting two aware datetimes that share the *same*
tzinfo object gives wall-clock time, ignoring a DST change in between. Converting both
to UTC first gives real elapsed time.
"""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

HOTEL_TZ = ZoneInfo("Europe/Athens")  # needs the tzdata package on Windows
CHECK_IN_TIME = time(15, 0)


def require_aware(at: datetime, name: str) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got {at!r}")


def hotel_date(at: datetime) -> date:
    """The calendar date in the hotel (Europe/Athens) at instant `at`.

    31 Dec 22:30 UTC is already 1 Jan in Athens (UTC+2 in winter).
    """
    require_aware(at, "at")
    return at.astimezone(HOTEL_TZ).date()


def check_in_moment(arrival: date) -> datetime:
    """15:00 hotel time on the arrival date, as UTC."""
    return datetime.combine(arrival, CHECK_IN_TIME, tzinfo=HOTEL_TZ).astimezone(UTC)
