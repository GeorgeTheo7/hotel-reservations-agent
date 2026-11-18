"""D82's settings and helpers, no database: the password from the environment, the
constant-time comparison, the token hash, the hidden repr."""

import hashlib

import pytest

from anemos.api.auth import (
    COOKIE_SECURE_ENV,
    PASSWORD_ENV,
    OwnerAuth,
    auth_from_env,
    password_matches,
    token_hash,
)

pytestmark = pytest.mark.unit

GOOD = "twelve-chars"  # exactly 12


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.delenv(COOKIE_SECURE_ENV, raising=False)
    return monkeypatch


def test_a_password_of_twelve_characters_is_enough(env: pytest.MonkeyPatch) -> None:
    env.setenv(PASSWORD_ENV, GOOD)
    assert auth_from_env() == OwnerAuth(password=GOOD, cookie_secure=True)


@pytest.mark.parametrize("value", [None, "", "   ", GOOD[:-1]])
def test_no_password_or_a_short_one_stops_the_app(
    env: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is not None:
        env.setenv(PASSWORD_ENV, value)
    with pytest.raises(ValueError, match=PASSWORD_ENV):
        auth_from_env()


@pytest.mark.parametrize(("raw", "secure"), [("false", False), ("FALSE", False), ("true", True)])
def test_the_secure_flag_can_be_turned_off_for_development(
    env: pytest.MonkeyPatch, raw: str, secure: bool
) -> None:
    env.setenv(PASSWORD_ENV, GOOD)
    env.setenv(COOKIE_SECURE_ENV, raw)
    assert auth_from_env().cookie_secure is secure


@pytest.mark.parametrize("raw", ["no", "0", ""])
def test_an_unclear_secure_flag_is_refused_not_guessed(env: pytest.MonkeyPatch, raw: str) -> None:
    env.setenv(PASSWORD_ENV, GOOD)
    env.setenv(COOKIE_SECURE_ENV, raw)
    with pytest.raises(ValueError, match=COOKIE_SECURE_ENV):
        auth_from_env()


def test_the_password_never_appears_in_repr() -> None:
    auth = OwnerAuth(password="do-not-print-me")
    assert "do-not-print-me" not in repr(auth)
    assert "do-not-print-me" not in str(auth)


@pytest.mark.parametrize(
    ("given", "matches"),
    [(GOOD, True), (GOOD + " ", False), (GOOD.upper(), False), ("", False), (GOOD[:-1], False)],
)
def test_password_matches_exactly(given: str, matches: bool) -> None:
    assert password_matches(given, GOOD) is matches


def test_the_token_hash_is_sha256_of_the_token() -> None:
    assert token_hash("abc") == hashlib.sha256(b"abc").digest()
    assert len(token_hash("abc")) == 32  # the table's CHECK
