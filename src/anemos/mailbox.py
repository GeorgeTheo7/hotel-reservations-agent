"""Mailbox adapter (D4): where guest emails come from. Fake only in v1.

The rest of the system sees only `IncomingEmail`, with the fields a real email has,
so a real mailbox in v2 is a new adapter, not a rewrite. The adapter also normalises
what the database insists on: a lowercase bare address (D48 compares with =) and a
timezone-aware `received_at` (D23).
"""

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Protocol


@dataclass(frozen=True)
class IncomingEmail:
    message_id: str
    from_address: str  # bare, lowercase (normalised by `incoming`)
    subject: str
    raw_body: str
    clean_body: str
    received_at: datetime  # UTC
    in_reply_to: str | None = None
    mail_thread_id: str | None = None


class Mailbox(Protocol):
    def fetch(self) -> Iterable[IncomingEmail]:
        """Emails waiting to be stored. Fetching twice may return the same email
        again: storing is idempotent on message_id (D4)."""
        ...


def incoming(
    *,
    message_id: str,
    from_header: str,
    subject: str,
    body: str,
    received_at: datetime,
    in_reply_to: str | None = None,
    mail_thread_id: str | None = None,
) -> IncomingEmail:
    """Build an IncomingEmail from header-like values, normalised for storage.

    `from_header` may be a display form ("Anna K <Anna@Example.test>"); only the
    address is kept, lowercased. `clean_body` is the body with line endings unified
    and outer whitespace removed: v1 input is already plain text (D5); stripping
    quoted replies and signatures is not done (thread replies escalate anyway).
    """
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError(f"received_at must be timezone-aware, got {received_at!r}")
    _, address = parseaddr(from_header)
    if "@" not in address:
        raise ValueError(f"no email address in From: {from_header!r}")
    return IncomingEmail(
        message_id=message_id.strip(),
        from_address=address.strip().lower(),
        subject=subject.strip(),
        raw_body=body,
        clean_body=body.replace("\r\n", "\n").replace("\r", "\n").strip(),
        received_at=received_at.astimezone(UTC),
        in_reply_to=in_reply_to,
        mail_thread_id=mail_thread_id,
    )


class FakeMailbox:
    """An in-memory mailbox for tests, the evaluation and the replay demo.

    `fetch` returns every email each time, like a mailbox whose messages are never
    marked read, which exercises idempotent storing.
    """

    def __init__(self, emails: Sequence[IncomingEmail] = ()) -> None:
        self._emails = list(emails)

    def deliver(self, email: IncomingEmail) -> None:
        self._emails.append(email)

    def fetch(self) -> Iterator[IncomingEmail]:
        return iter(list(self._emails))
