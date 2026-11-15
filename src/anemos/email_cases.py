"""Test emails as YAML files, schema version 1 (docs/email-format.md).

One format and one loader for the evaluation set (eval/emails/), the demo inbox
(anemos/demo_emails/), the fake mailbox and the tests. Files are read with
`yaml.safe_load` (never runs code) and validated strictly: an unknown key, a wrong
type or a broken rule refuses the whole file, with the file name in the error.
"""

from collections.abc import Iterable
from datetime import date
from email.utils import parseaddr
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from anemos.db.seed import ROOM_TYPES, ROOMS
from anemos.extraction import ExtractionFlag, Script
from anemos.mailbox import IncomingEmail, incoming
from anemos.reservations.datespec import (
    ApproximateDates,
    DateSpec,
    ExactDates,
    HolidayDates,
    PartialDate,
    RelativeDates,
)
from anemos.reservations.errors import ReasonCode

FICTIONAL_DOMAIN = "example.test"  # RFC 2606: reserved, can never be a real mailbox
MESSAGE_ID_DOMAIN = "cases.example.test"
ROOM_TYPE_IDS = frozenset(row[0] for row in ROOM_TYPES)

# The agent's tools (spec, Tools). hold_room and send_reply run only in the approval
# handler, so no case can require the agent to call them.
type AgentTool = Literal["check_availability", "quote_price", "suggest_alternatives",
                         "search_policies"]  # fmt: skip
type Route = Literal["quote", "ask_info", "policy", "decline", "escalate", "set_aside"]

Count = Annotated[StrictInt, Field(ge=0)]
Text = Annotated[StrictStr, Field(pattern=r"\S")]  # not blank


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- date_spec: the DateSpec kinds of reservations/datespec.py, field for field ----------


class _Partial(_Strict):
    day: StrictInt
    month: StrictInt
    year: StrictInt | None = None
    ambiguous_order: StrictBool = False  # D37

    def build(self) -> PartialDate:
        return PartialDate(self.day, self.month, self.year, self.ambiguous_order)


class _Exact(_Strict):
    kind: Literal["exact"]
    check_in: _Partial
    check_out: _Partial | None = None
    nights: StrictInt | None = None

    def build(self) -> ExactDates:
        out = self.check_out.build() if self.check_out else None
        return ExactDates(self.check_in.build(), out, self.nights)


class _Relative(_Strict):
    kind: Literal["relative"]
    phrase: Literal["this_weekend", "next_weekend"]
    nights: StrictInt | None = None

    def build(self) -> RelativeDates:
        return RelativeDates(self.phrase, self.nights)


class _Holiday(_Strict):
    kind: Literal["holiday"]
    holiday: Text
    tradition: Literal["orthodox", "western", "unspecified"] = "unspecified"
    year: StrictInt | None = None
    nights: StrictInt | None = None

    def build(self) -> HolidayDates:
        return HolidayDates(self.holiday, self.tradition, self.year, self.nights)


class _Approximate(_Strict):
    kind: Literal["approximate"]
    part: Literal["early", "mid", "late", "whole_month"]
    month: StrictInt
    year: StrictInt | None = None
    nights: StrictInt | None = None

    def build(self) -> ApproximateDates:
        return ApproximateDates(self.part, self.month, self.year, self.nights)


type _DateSpecLabel = Annotated[
    _Exact | _Relative | _Holiday | _Approximate, Field(discriminator="kind")
]


# --- labels --------------------------------------------------------------------------


class Extraction(_Strict):
    date_spec: _DateSpecLabel | None = None  # None: the guest gave no dates
    adults: Count | None = None
    children: Count | None = None
    child_ages: list[Annotated[StrictInt, Field(ge=0, le=17)]] = []
    rooms: Annotated[StrictInt, Field(ge=1)] | None = None
    room_type: StrictStr | None = None  # a seeded room type id; None = no type named
    flags: list[ExtractionFlag] = []

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.date_spec is not None:
            self.date_spec.build()  # PartialDate / ApproximateDates refuse impossible values
        if len(self.child_ages) > (self.children or 0):
            raise ValueError("more child_ages than children")
        if self.room_type is not None and self.room_type not in ROOM_TYPE_IDS:
            raise ValueError(f"room_type {self.room_type!r} is not a seeded room type")
        if len(set(self.flags)) != len(self.flags):
            raise ValueError("a flag is listed twice")
        return self

    def date_spec_value(self) -> DateSpec | None:
        return None if self.date_spec is None else self.date_spec.build()


class Labels(_Strict):
    language: Annotated[StrictStr, Field(pattern=r"^[a-z]{2}$")]
    script: Script
    extraction: Extraction | None  # None: spam, or a question with no request in it
    route: Route
    reason_codes: list[ReasonCode] = []
    required_tools: list[AgentTool] = []
    policy_ids: list[Text] = []
    holds: Annotated[StrictInt, Field(ge=0, le=2)] = 0
    forbidden_phrases: list[Text] = []

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.script is Script.GREEK and self.language != "el":
            raise ValueError("script greek needs language el")
        if self.language == "en" and self.script is not Script.LATIN:
            raise ValueError("language en needs script latin")
        if (self.route == "escalate") != bool(self.reason_codes):
            raise ValueError("reason_codes are given exactly when the route is escalate")
        for name, values in (("reason_codes", self.reason_codes),
                             ("required_tools", self.required_tools)):  # fmt: skip
            if len(set(values)) != len(values):
                raise ValueError(f"{name}: a value is listed twice")
        return self


# --- the email and the world it starts from -------------------------------------------


class Email(_Strict):
    from_: Text = Field(alias="from")
    subject: StrictStr  # may be empty
    received_at: AwareDatetime  # pinned; with a timezone
    in_reply_to: Text | None = None
    body: Text

    @model_validator(mode="after")
    def check(self) -> Self:
        _, address = parseaddr(self.from_)
        if address.rpartition("@")[2].lower() != FICTIONAL_DOMAIN:
            raise ValueError(f"from must be an @{FICTIONAL_DOMAIN} address (fictional data)")
        return self


class WorldBooking(_Strict):
    room: StrictInt
    check_in: date
    check_out: date
    status: Literal["confirmed", "hold"]
    expires_at: AwareDatetime | None = None  # holds only

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.room not in ROOMS:
            raise ValueError(f"room {self.room} is not a seeded room")
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if (self.status == "hold") != (self.expires_at is not None):
            raise ValueError("expires_at is given exactly for a hold")
        return self


class World(_Strict):
    """The database state the case starts from. Empty: the seeded hotel, no bookings."""

    bookings: list[WorldBooking] = []


class EmailCase(_Strict):
    schema_version: Literal[1]  # the only version; a new one gets its own models
    id: Annotated[StrictStr, Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]
    split: Literal["dev", "held_out", "demo"]
    tags: list[Text] = []
    email: Email
    world: World = World()
    labels: Labels

    @property
    def message_id(self) -> str:
        return f"<{self.id}@{MESSAGE_ID_DOMAIN}>"

    def incoming(self) -> IncomingEmail:
        """The email as the mailbox adapter delivers it (normalised by `incoming`)."""
        return incoming(
            message_id=self.message_id, from_header=self.email.from_,
            subject=self.email.subject, body=self.email.body,
            received_at=self.email.received_at, in_reply_to=self.email.in_reply_to,
        )  # fmt: skip


# --- loading ---------------------------------------------------------------------------


class CaseError(ValueError):
    """A file that is not a valid schema-1 case; the message names the file."""


def parse_case(text: str, *, name: str) -> EmailCase:
    """Parse one case from YAML text; `name` (the file stem) must equal its id."""
    try:
        case = EmailCase.model_validate(yaml.safe_load(text))
    except (ValidationError, yaml.YAMLError) as exc:
        raise CaseError(f"{name}: {exc}") from exc
    if case.id != name:
        raise CaseError(f"{name}: id {case.id!r} does not match the file name")
    return case


def load_case(path: Path) -> EmailCase:
    return parse_case(path.read_text(encoding="utf-8"), name=path.stem)


def check_unique(cases: Iterable[EmailCase]) -> list[EmailCase]:
    found = list(cases)
    seen: set[str] = set()
    for case in found:
        if case.id in seen:
            raise CaseError(f"{case.id}: id used twice")
        seen.add(case.id)
    return found


def load_cases(directory: Path) -> list[EmailCase]:
    """Every *.yaml file in `directory`, sorted by name."""
    return check_unique(load_case(path) for path in sorted(directory.glob("*.yaml")))
