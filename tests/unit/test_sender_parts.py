"""Sender pieces with no database: backoff, Message-IDs, the scripted fake transport."""

from datetime import timedelta

import pytest

from anemos.sender import (
    FakeTransport,
    OutgoingMessage,
    TransportError,
    backoff,
    message_id_for,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("attempts", "minutes"), [(1, 1), (2, 2), (3, 4), (4, 8), (6, 32), (7, 60), (20, 60)]
)
def test_backoff_doubles_and_caps_at_an_hour(attempts: int, minutes: int) -> None:
    assert backoff(attempts) == timedelta(minutes=minutes)


def test_backoff_needs_a_failed_attempt() -> None:
    with pytest.raises(ValueError, match="attempts must be >= 1"):
        backoff(0)


def test_message_id_is_stable_and_never_a_real_domain() -> None:
    """D66: the same Message-ID on every attempt; .invalid is reserved (RFC 2606)."""
    assert message_id_for(42) == message_id_for(42) == "<outbox-42@anemos.invalid>"


def message(n: int) -> OutgoingMessage:
    return OutgoingMessage(message_id_for(n), "g@example.test", "Re: s", "b", "<g@x>", "<g@x>")


def test_fake_transport_fails_n_times_then_succeeds() -> None:
    transport = FakeTransport(fail_first=2)
    for _ in range(2):
        with pytest.raises(TransportError):
            transport.send(message(1))
    transport.send(message(1))
    assert transport.sent == [message(1)]


def test_fake_transport_can_always_fail_one_message() -> None:
    transport = FakeTransport(always_fail=frozenset({message_id_for(1)}))
    with pytest.raises(TransportError):
        transport.send(message(1))
    transport.send(message(2))
    assert transport.sent == [message(2)]


def test_fake_transport_raises_the_scripted_error_type() -> None:
    transport = FakeTransport(fail_first=1, error=ValueError("not a transport error"))
    with pytest.raises(ValueError, match="not a transport error"):
        transport.send(message(1))
