"""Typed errors of the reservation core (spec: "Errors are typed return values").

A function that can fail for a business reason returns `Result | SomeError` instead of
raising, so the caller (and later the agent's tools) must handle the error case;
mypy flags code that uses the result without checking. Exceptions stay for bugs.
More error types are added in the steps that need them (CLOSED, MIN_STAY, ...).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar


@dataclass(frozen=True)
class InvalidDates:
    code: ClassVar[str] = "INVALID_DATES"
    reason: str


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
