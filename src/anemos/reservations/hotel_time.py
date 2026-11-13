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


def check_in_moment(arrival: date) -> datetime:
    """15:00 hotel time on the arrival date, as UTC."""
    return datetime.combine(arrival, CHECK_IN_TIME, tzinfo=HOTEL_TZ).astimezone(UTC)
