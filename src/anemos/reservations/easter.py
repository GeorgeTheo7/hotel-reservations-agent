"""Easter dates, integer arithmetic only (D6, D28).

Orthodox Easter: Meeus' Julian algorithm gives the date in the Julian calendar; adding
13 days converts it to our (Gregorian) calendar. The gap is 13 days only from
1 Mar 1900 to 28 Feb 2100, so years outside 1900-2099 are refused rather than wrong.

Western Easter: the "anonymous Gregorian" algorithm (Meeus/Jones/Butcher), valid for
every Gregorian year. Needed for the "Catholic/Western Easter" trap: 28 Mar 2027 is
before opening, so the reply is CLOSED.
"""

from datetime import date, timedelta

JULIAN_TO_GREGORIAN_DAYS = 13
ORTHODOX_YEARS = range(1900, 2100)
FIRST_GREGORIAN_YEAR = 1583


def orthodox_easter(year: int) -> date:
    if year not in ORTHODOX_YEARS:
        raise ValueError(f"orthodox_easter supports 1900-2099 (13-day calendar gap), got {year}")
    a = year % 4
    b = year % 7
    c = year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day_index = divmod(d + e + 114, 31)
    julian = date(year, month, day_index + 1)  # read as a Julian-calendar date
    return julian + timedelta(days=JULIAN_TO_GREGORIAN_DAYS)


def western_easter(year: int) -> date:
    if year < FIRST_GREGORIAN_YEAR:
        raise ValueError(f"western_easter needs a Gregorian year (>= 1583), got {year}")
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day_index = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day_index + 1)
