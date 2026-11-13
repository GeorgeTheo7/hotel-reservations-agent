"""Typed errors of the reservation core (spec: "Errors are typed return values").

A function that can fail for a business reason returns `Result | SomeError` instead of
raising, so the caller (and later the agent's tools) must handle the error case;
mypy flags code that uses the result without checking. Exceptions stay for bugs.
More error types are added in the steps that need them (CLOSED, MIN_STAY, ...).
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import ClassVar


@dataclass(frozen=True)
class InvalidDates:
    code: ClassVar[str] = "INVALID_DATES"
    reason: str


@dataclass(frozen=True)
class Closed:
    """Some nights of the stay fall outside every season period (hotel closed)."""

    code: ClassVar[str] = "CLOSED"
    nights: tuple[date, ...]


@dataclass(frozen=True)
class MinStay:
    """The stay is shorter than the minimum for its arrival date."""

    code: ClassVar[str] = "MIN_STAY"
    min_nights: int


@dataclass(frozen=True)
class OverCapacity:
    """The party does not fit the requested rooms (D13 head count vs max occupancy)."""

    code: ClassVar[str] = "OVER_CAPACITY"
    head_count: int
    max_people: int


@dataclass(frozen=True)
class NoRate:
    """No price for these nights: closed, or no rate for the room type and season."""

    code: ClassVar[str] = "NO_RATE"
    nights: tuple[date, ...]


class ReasonCode(StrEnum):
    """Escalation reason codes (spec: Escalation rules). Code and model raise the same set."""

    COMPLAINT = "COMPLAINT"
    DISCOUNT = "DISCOUNT"
    GROUP = "GROUP"
    CHANGE = "CHANGE"
    CANCELLATION = "CANCELLATION"
    THREAD_REPLY = "THREAD_REPLY"
    LANGUAGE = "LANGUAGE"
    SHORT_NOTICE = "SHORT_NOTICE"
    LATE_ARRIVAL = "LATE_ARRIVAL"
    NO_POLICY = "NO_POLICY"
    GUARDRAIL_FAIL = "GUARDRAIL_FAIL"
    LOOP_LIMIT = "LOOP_LIMIT"
    LEGAL = "LEGAL"
    BILLING = "BILLING"
    MEDICAL = "MEDICAL"
    INJECTION = "INJECTION"
    MODEL = "MODEL"
    ACCESSIBILITY = "ACCESSIBILITY"
    UNACCOMPANIED_CHILDREN = "UNACCOMPANIED_CHILDREN"


@dataclass(frozen=True)
class NeedsEscalation:
    """A rule found a case the owner must handle; no draft is written for it."""

    reason: ReasonCode
    detail: str
