"""Extraction (spec Escalation rules, D5, D6): what the model reads out of a guest email.

The model writes down what the guest said; code does everything else (D6): years,
weekends, windows and stays are resolved later by `reservations.datespec`. The model
is never given today's date or the email's received_at.

`ExtractionOutput` is the structured-output schema sent to the model. OpenAI's strict
mode needs every field present, so a value the email does not give is null (or an
empty list), never left out. The test-email labels (`email_cases.Labels.kind/
language/script` + `email_cases.Extraction`) hold the same fields; a test keeps the two
in step.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from anemos.reservations.datespec import (
    ApproximateDates,
    DateSpec,
    ExactDates,
    HolidayDates,
    PartialDate,
    RelativeDates,
)


class ExtractionFlag(StrEnum):
    LEGAL_THREAT = "legal_threat"
    BILLING_REQUEST = "billing_request"
    MEDICAL_NEED = "medical_need"
    ACCESSIBILITY_NEED = "accessibility_need"
    INJECTION_SUSPECTED = "injection_suspected"


class Script(StrEnum):
    """The letters the guest wrote in. `latin` with language `el` is Greeklish (D5)."""

    GREEK = "greek"
    LATIN = "latin"


# The seeded room type ids (seed.ROOM_TYPES; a test checks they agree).
type RoomTypeId = Literal["standard", "sea_view", "family_suite", "accessible"]
# request: asks for a stay. question: a policy question only. other: anything else a
# guest writes (complaint, cancellation, change, invoice). spam: set aside.
type Kind = Literal["request", "question", "other", "spam"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutDate(_Out):
    day: Annotated[int, Field(ge=1, le=31)]
    month: Annotated[int, Field(ge=1, le=12)]
    year: int | None
    ambiguous_order: bool

    def build(self) -> PartialDate:
        return PartialDate(self.day, self.month, self.year, self.ambiguous_order)


class OutExact(_Out):
    kind: Literal["exact"]
    check_in: OutDate
    check_out: OutDate | None
    nights: int | None

    def build(self) -> ExactDates:
        out = self.check_out.build() if self.check_out else None
        return ExactDates(self.check_in.build(), out, self.nights)


class OutRelative(_Out):
    kind: Literal["relative"]
    phrase: Literal["this_weekend", "next_weekend"]
    nights: int | None

    def build(self) -> RelativeDates:
        return RelativeDates(self.phrase, self.nights)


class OutHoliday(_Out):
    kind: Literal["holiday"]
    holiday: str
    tradition: Literal["orthodox", "western", "unspecified"]
    year: int | None
    nights: int | None

    def build(self) -> HolidayDates:
        return HolidayDates(self.holiday, self.tradition, self.year, self.nights)


class OutApproximate(_Out):
    kind: Literal["approximate"]
    part: Literal["early", "mid", "late", "whole_month"]
    month: Annotated[int, Field(ge=1, le=12)]
    year: int | None
    nights: int | None

    def build(self) -> ApproximateDates:
        return ApproximateDates(self.part, self.month, self.year, self.nights)


# A plain union, not Field(discriminator=...): pydantic writes a discriminated union as
# `oneOf`, which OpenAI's strict mode refuses; a plain union is `anyOf`. Each kind has
# its own fixed `kind` value, so exactly one variant matches anyway.
type OutDateSpec = OutExact | OutRelative | OutHoliday | OutApproximate


class ExtractionOutput(_Out):
    """The model's answer for one email. Every field is required; unknown = null."""

    kind: Kind
    language: Annotated[str, Field(pattern=r"^[a-z]{2}$")]
    script: Script
    flags: list[ExtractionFlag]
    date_spec: OutDateSpec | None
    adults: Annotated[int, Field(ge=0)] | None
    children: Annotated[int, Field(ge=0)] | None
    child_ages: list[Annotated[int, Field(ge=0, le=17)]]
    rooms: Annotated[int, Field(ge=1)] | None
    room_type: RoomTypeId | None
    arrival_time: Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")] | None

    @model_validator(mode="after")
    def check(self) -> Self:
        if len(self.child_ages) > (self.children or 0):
            raise ValueError("more child_ages than children")
        if self.date_spec is not None:
            self.date_spec.build()  # PartialDate refuses impossible values
        return self

    def date_spec_value(self) -> DateSpec | None:
        return None if self.date_spec is None else self.date_spec.build()
