from datetime import UTC, datetime, timedelta, timezone

import pytest

from anemos.clock import Clock, FixedClock, SystemClock

pytestmark = pytest.mark.unit

# A fixed UTC+3 offset (Greek summer time) without needing a timezone database.
UTC_PLUS_3 = timezone(timedelta(hours=3))


def test_fixed_clock_returns_the_time_it_was_given() -> None:
    at = datetime(2027, 5, 2, 9, 30, tzinfo=UTC)
    assert FixedClock(at).now() == at


def test_fixed_clock_rejects_naive_datetime() -> None:
    naive = datetime(2027, 5, 2, 9, 30)  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(naive)


def test_fixed_clock_converts_other_offsets_to_utc() -> None:
    now = FixedClock(datetime(2027, 5, 2, 12, 0, tzinfo=UTC_PLUS_3)).now()
    assert now == datetime(2027, 5, 2, 9, 0, tzinfo=UTC)
    assert now.tzinfo is UTC


def test_fixed_clock_advance_moves_forward() -> None:
    clock = FixedClock(datetime(2027, 5, 2, 9, 0, tzinfo=UTC))
    clock.advance(timedelta(hours=48))
    assert clock.now() == datetime(2027, 5, 4, 9, 0, tzinfo=UTC)


def test_fixed_clock_does_not_move_on_its_own() -> None:
    clock = FixedClock(datetime(2027, 5, 2, 9, 0, tzinfo=UTC))
    assert clock.now() == clock.now()


def test_fixed_clock_advance_rejects_negative_delta() -> None:
    clock = FixedClock(datetime(2027, 5, 2, 9, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="forward"):
        clock.advance(timedelta(seconds=-1))


def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is UTC


def test_system_clock_moves_forward() -> None:
    clock = SystemClock()
    first = clock.now()
    assert clock.now() >= first


def test_both_clocks_satisfy_the_protocol() -> None:
    # The real check is mypy: these assignments fail type-checking if a class
    # stops matching the Clock protocol.
    clocks: list[Clock] = [SystemClock(), FixedClock(datetime(2027, 5, 2, tzinfo=UTC))]
    assert all(isinstance(c.now(), datetime) for c in clocks)
