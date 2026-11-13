"""Typed errors of the reservation core (spec: "Errors are typed return values").

A function that can fail for a business reason returns `Result | SomeError` instead of
raising, so the caller (and later the agent's tools) must handle the error case;
mypy flags code that uses the result without checking. Exceptions stay for bugs.
More error types are added in the steps that need them (CLOSED, MIN_STAY, ...).
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class InvalidDates:
    code: ClassVar[str] = "INVALID_DATES"
    reason: str
