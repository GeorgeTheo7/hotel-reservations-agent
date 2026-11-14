"""The mailbox adapter: normalising header values and the fake mailbox (D4)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from anemos.mailbox import FakeMailbox, incoming

pytestmark = pytest.mark.unit

AT = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)


def make(**overrides: object) -> object:
    values: dict[str, object] = {
        "message_id": "<a@example.test>",
        "from_header": "guest@example.test",
        "subject": "Room in July",
        "body": "Hello",
        "received_at": AT,
    } | overrides
    return incoming(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "header",
    ["Anna K <Anna.K@Example.TEST>", "anna.k@example.test", "  ANNA.K@example.test  ",
     '"K, Anna" <anna.k@EXAMPLE.test>'],
)  # fmt: skip
def test_from_header_becomes_a_lowercase_bare_address(header: str) -> None:
    email = incoming(
        message_id="<a@example.test>", from_header=header, subject="s", body="b",
        received_at=AT,
    )  # fmt: skip
    assert email.from_address == "anna.k@example.test"


@pytest.mark.parametrize("header", ["", "Anna K", "<>"])
def test_from_header_without_an_address_is_refused(header: str) -> None:
    with pytest.raises(ValueError, match="no email address"):
        make(from_header=header)


def test_clean_body_unifies_line_endings_and_trims() -> None:
    email = incoming(
        message_id="<a@example.test>", from_header="g@example.test", subject=" Hi ",
        body="\r\n  Hello,\r\nroom for two?\r\n\r\n", received_at=AT,
    )  # fmt: skip
    assert email.clean_body == "Hello,\nroom for two?"
    assert email.raw_body == "\r\n  Hello,\r\nroom for two?\r\n\r\n"  # kept as received
    assert email.subject == "Hi"


def test_received_at_is_stored_in_utc() -> None:
    athens_winter = timezone(timedelta(hours=2))
    email = incoming(
        message_id="<a@example.test>", from_header="g@example.test", subject="s", body="b",
        received_at=datetime(2026, 11, 13, 9, 0, tzinfo=athens_winter),
    )  # fmt: skip
    assert email.received_at == AT
    assert email.received_at.tzinfo is UTC


def test_naive_received_at_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make(received_at=datetime(2026, 11, 13, 7, 0))  # noqa: DTZ001 (the point of the test)


def test_fake_mailbox_returns_everything_every_time() -> None:
    """Like a mailbox that never marks messages read: storing must be idempotent."""
    first = incoming(
        message_id="<1@example.test>", from_header="g@example.test", subject="s", body="b",
        received_at=AT,
    )  # fmt: skip
    mailbox = FakeMailbox([first])
    assert list(mailbox.fetch()) == [first]
    second = incoming(
        message_id="<2@example.test>", from_header="g@example.test", subject="s", body="b",
        received_at=AT,
    )  # fmt: skip
    mailbox.deliver(second)
    assert list(mailbox.fetch()) == [first, second]
    assert list(mailbox.fetch()) == [first, second]
