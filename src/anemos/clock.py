"""Injected clock (D20). The only module allowed to read the system time.

Code that needs "now" takes a `Clock` parameter instead of calling `datetime.now()`,
so tests and evaluations can fix the time. All times are timezone-aware UTC (D23);
conversion to hotel time (Europe/Athens) happens in calendar code, not here.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware, in UTC."""
        ...


class SystemClock:
    """Real time, for the running app."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Test clock: returns a set time and moves only when told to."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        self._now = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("FixedClock only moves forward")
        self._now += delta
