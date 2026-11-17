"""Guardrails 2-5 (week 6): pure checks on the draft's free text, no database."""

import json
from importlib.resources import files

import pytest

from anemos.guardrails import (
    forbidden_phrases,
    no_forbidden_phrases,
    normalised,
    number_values,
    numbers_from_policies,
    reply_script,
    summary_present,
)

pytestmark = pytest.mark.unit

TRANSFER = "It costs €25.00 per trip per car, for up to 4 passengers with their luggage."
BREAKFAST = "Breakfast is included for all guests and is served every morning from 08:00 to 10:30."
DEPOSIT = "To confirm a booking we ask for a deposit of 30% of the accommodation total."
HOLDS = "A held room is not yet a booking: the booking is confirmed once we have your deposit."
HOURS = "When we hold a room for you, the deposit is due within 48 hours."


# --- guardrail 2: numbers by normalised value (D78) --------------------------------------------


@pytest.mark.parametrize("written", [
    "The transfer costs 25 € per car.",  # Greek order, no decimals
    "Η μεταφορά κοστίζει 25,00 € ανά αυτοκίνητο.",  # decimal comma
    "The transfer costs €25.00 per car.",  # as the chunk writes it
    "The transfer costs €25 per car.",
    "Up to 4 passengers.",
])  # fmt: skip
def test_a_policy_amount_in_any_form_passes(written: str) -> None:
    assert numbers_from_policies(written, [TRANSFER]) is True


@pytest.mark.parametrize("written", ["Breakfast from 08:00 to 10:30.", "Πρωινό 8:00 – 10:30."])
def test_a_policy_time_passes_with_or_without_the_leading_zero(written: str) -> None:
    assert numbers_from_policies(written, [BREAKFAST]) is True


def test_a_time_stays_a_time() -> None:
    """8.00 is the number 8, not the time 08:00: it does not match the breakfast hours."""
    assert numbers_from_policies("Breakfast from 8.00.", [BREAKFAST]) == (
        "numbers outside the code-rendered blocks and the returned policies: ['8']"
    )


@pytest.mark.parametrize("written", ["a deposit of 30%", "προκαταβολή 30 %", "30 percent"])
def test_a_percentage_matches_its_value(written: str) -> None:
    assert numbers_from_policies(written, [DEPOSIT]) is True


def test_a_greek_reply_quotes_an_english_chunk() -> None:
    assert numbers_from_policies("Η προκαταβολή οφείλεται σε 48 ώρες.", [HOURS]) is True


@pytest.mark.parametrize("written, stray", [
    ("The transfer costs 26 €.", "['26']"),  # not the policy's amount
    ("Your stay is 3 nights.", "['3']"),  # no policy says 3
    ("Arrive on 12/06.", "['12', '6']"),  # a date the model wrote
])  # fmt: skip
def test_a_number_no_returned_policy_has_fails(written: str, stray: str) -> None:
    result = numbers_from_policies(written, [TRANSFER])
    assert result == f"numbers outside the code-rendered blocks and the returned policies: {stray}"


def test_without_a_returned_policy_any_number_fails() -> None:
    assert numbers_from_policies("Breakfast from 08:00.", []) is not True


def test_placeholders_are_not_free_text() -> None:
    assert numbers_from_policies("{{REQUEST_SUMMARY}}\n\n{{QUOTE_BLOCK}}", []) is True


def test_number_values_normalise_thousands_and_decimals() -> None:
    assert number_values("1.500 or 1,500 or 1500") == {"1500"}
    assert number_values("€1.50 or 1,5") == {"1.5"}
    assert number_values("12.25") == {"12.25"}


# --- guardrail 3: the request summary (D80) ---------------------------------------------------


@pytest.mark.parametrize("route", ["quote", "decline"])
def test_quote_and_decline_need_the_summary(route: str) -> None:
    assert summary_present("Dear guest", route, relative_or_holiday=False) == (
        f"{{{{REQUEST_SUMMARY}}}} missing: required for the {route} route"
    )
    assert summary_present("{{REQUEST_SUMMARY}}", route, relative_or_holiday=False) is True


def test_ask_info_needs_it_only_for_a_relative_or_holiday_date() -> None:
    assert summary_present("How many?", "ask_info", relative_or_holiday=False) is True
    assert summary_present("How many?", "ask_info", relative_or_holiday=True) == (
        "{{REQUEST_SUMMARY}} missing: required for a relative or holiday date"
    )
    assert summary_present("{{REQUEST_SUMMARY}}", "ask_info", relative_or_holiday=True) is True


def test_a_policy_answer_never_needs_it() -> None:
    assert summary_present("Pets are welcome.", "policy", relative_or_holiday=True) is True


# --- guardrail 4: the reply's script (D79) ----------------------------------------------------


def test_an_english_reply_has_no_greek_letter() -> None:
    assert reply_script("Dear guest, see you at Hotel Anemos.", "en") is True
    assert (
        reply_script("Dear guest, ευχαριστούμε.", "en") == "English reply with 12 Greek letter(s)"
    )


def test_a_greek_reply_may_keep_names_in_latin_letters() -> None:
    text = "Αγαπητέ επισκέπτη, το Wi-Fi είναι δωρεάν στο Hotel Anemos. Με εκτίμηση"
    assert reply_script(text, "el") is True


def test_a_greeklish_reply_to_a_greek_email_fails() -> None:
    result = reply_script("Agapite episkepti, to proino einai dorean.", "el")
    assert isinstance(result, str) and result.startswith("Greek reply with 0 of")


def test_no_letters_and_placeholders_pass() -> None:
    assert reply_script("{{REQUEST_SUMMARY}}\n\n{{QUOTE_BLOCK}}", "el") is True
    assert reply_script("{{REQUEST_SUMMARY}}", "en") is True


# --- guardrail 5: forbidden phrases -----------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Your booking is confirmed.",
    "Your BOOKING   is\nconfirmed.",  # case and spaces
    "We can offer a free upgrade.",
    "The price is guaranteed.",
    "We can give you a 10% discount.",
    "Discounts are possible.",
])  # fmt: skip
def test_english_forbidden_phrases_fail(text: str) -> None:
    assert no_forbidden_phrases(text, []) is not True


@pytest.mark.parametrize("text", [
    "Η κράτησή σας είναι επιβεβαιωμένη.",  # feminine
    "Το δωμάτιο είναι επιβεβαιωμένο.",  # neuter
    "Οι τιμές είναι εγγυημένες.",  # plural
    "Η τιμή είναι εγγυημενη.",  # no accent
    "ΕΓΓΥΗΜΈΝΗ ΤΙΜΗ",  # capitals
    "Σας κάνουμε έκπτωση.",
    "Δεν υπάρχουν εκπτώσεις.",  # plural (and an honest negative)
    "Η κράτηση επιβεβαιώθηκε.",
])  # fmt: skip
def test_greek_forbidden_stems_catch_every_form(text: str) -> None:
    assert no_forbidden_phrases(text, []) is not True


def test_policy_wording_is_allowed() -> None:
    """The holds chunk itself says "the booking is confirmed once we have your deposit"."""
    text = "A held room is not yet a booking: the booking is confirmed once we have your deposit."
    assert no_forbidden_phrases(text, [HOLDS]) is True
    assert no_forbidden_phrases(text, []) is not True


def test_an_english_chunk_does_not_excuse_a_greek_phrase() -> None:
    assert no_forbidden_phrases("Η κράτηση είναι επιβεβαιωμένη.", [HOLDS]) is not True


def test_an_honest_negative_is_blocked_too() -> None:
    """Known limit (NOTES): phrase matching cannot tell a negation; step 11 measures it."""
    assert no_forbidden_phrases("The room is not guaranteed until the deposit.", []) is not True


def test_the_list_has_no_true_policy_phrases() -> None:
    """Free cancellation until the deadline is real policy (owner, week 6)."""
    data = json.loads(files("anemos").joinpath("templates", "forbidden_phrases.json")
                      .read_text("utf-8"))  # fmt: skip
    every = {normalised(p) for p in data["en"] + data["el"]}
    for removed in ("fully refundable", "πλήρης επιστροφή", "η ακύρωση είναι δωρεάν"):
        assert normalised(removed) not in every
    assert set(forbidden_phrases()) == every
