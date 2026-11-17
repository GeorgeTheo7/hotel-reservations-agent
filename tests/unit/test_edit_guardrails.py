"""Guardrails 2-5 on an owner edit (D77): the word diff against version 1, and the four
checks on the changed words. Pure, no database."""

import pytest

from anemos.guardrails import EditChanges, edit_changes, edit_checks
from anemos.reservations.quote_template import summary_line

pytestmark = pytest.mark.unit

BLOCK = "Sea View Double, 3 nights\nTotal €390.00\nDeposit €117.00"
SUMMARY = "Your request: Sea View Double, 1 room, Tuesday 1 June 2027 – Friday 4 June 2027."
V1 = f"Dear guest,\n\n{SUMMARY}\n\n{BLOCK}\n\nKind regards,\nHotel Anemos"
TRANSFER = "It costs €25.00 per trip per car, for up to 4 passengers with their luggage."
V1_EL = "Αγαπητέ επισκέπτη,\n\nΣας ευχαριστούμε για το μήνυμά σας.\n\nΜε εκτίμηση,\nHotel Anemos"


def checks(edited: str, original: str = V1, language: str = "en",
           policies: tuple[str, ...] = ()) -> dict[str, bool | str]:  # fmt: skip
    return edit_checks(original, edited, blocks=[BLOCK], summary=summary_line(original),
                       policy_texts=list(policies), language=language)  # fmt: skip


def failed(results: dict[str, bool | str]) -> set[str]:
    return {k for k, r in results.items() if r is not True}


# --- the word diff ------------------------------------------------------------------------------


def test_only_inserted_or_replaced_words_count_and_runs_stay_apart() -> None:
    edited = V1.replace("Dear guest,", "Dear Maria, free").replace("Kind", "upgrade kind")
    assert edit_changes(V1, edited, [BLOCK]) == EditChanges(("Maria, free", "upgrade kind"), ())


def test_a_change_inside_a_quote_block_is_marked_as_such() -> None:
    edited = V1.replace("€390.00", "€350.00")
    assert edit_changes(V1, edited, [BLOCK]) == EditChanges((), ("€350.00",))


def test_deleted_words_and_whitespace_are_not_changes() -> None:
    assert edit_changes(V1, "  ".join(V1.split()), [BLOCK]) == EditChanges((), ())
    assert edit_changes(V1, V1.replace("Kind regards,\n", ""), [BLOCK]) == EditChanges((), ())


def test_summary_line_is_found_in_either_language() -> None:
    assert summary_line(V1) == SUMMARY
    assert summary_line("Γεια σας\nΤο αίτημά σας: 1 δωμάτιο.\nΤέλος") == "Το αίτημά σας: 1 δωμάτιο."
    assert summary_line("No summary here.") is None


# --- the four checks ----------------------------------------------------------------------------


def test_a_friendlier_edit_has_no_warning() -> None:
    assert failed(checks(V1.replace("Dear guest,", "Dear Maria,"))) == set()


def test_g2_a_new_number_warns_a_policy_or_version_1_number_does_not() -> None:
    assert failed(checks(V1.replace("Kind", "The transfer is €25. Kind"),
                         policies=(TRANSFER,))) == set()  # fmt: skip
    assert failed(checks(V1.replace("Kind", "Total again: €390. Kind"))) == set()  # from v1
    assert failed(checks(V1.replace("Kind", "Only 2 rooms left. Kind"))) == {
        "g2_numbers_from_policies"
    }


def test_g2_leaves_a_changed_price_to_the_price_check() -> None:
    assert failed(checks(V1.replace("€390.00", "€350.00"))) == set()


def test_g3_the_summary_line_deleted_or_changed_warns() -> None:
    assert failed(checks(V1.replace(SUMMARY + "\n", ""))) == {"g3_summary_present"}
    assert failed(checks(V1.replace("Friday 4 June", "Saturday 5 June"))) >= {"g3_summary_present"}


def test_g3_is_skipped_when_version_1_had_no_summary() -> None:
    assert failed(checks("Dear guest, see you.", original="Dear guest, see us.")) == set()


def test_g4_an_english_sentence_in_a_greek_reply_warns() -> None:
    edited = V1_EL.replace("Με εκτίμηση", "Thank you very much for your kind message. Με εκτίμηση")
    assert failed(checks(edited, original=V1_EL, language="el")) == {"g4_reply_script"}
    greek = V1_EL.replace("Με εκτίμηση", "Σας περιμένουμε. Με εκτίμηση")
    assert failed(checks(greek, original=V1_EL, language="el")) == set()


def test_g5_a_forbidden_phrase_warns_even_inside_a_block() -> None:
    assert failed(checks(V1.replace("Kind", "Your room is guaranteed. Kind"))) == {
        "g5_no_forbidden_phrases"
    }
    assert failed(checks(V1.replace("Total €390.00", "Total €390.00 guaranteed"))) == {
        "g5_no_forbidden_phrases"
    }


def test_g5_two_separate_edits_do_not_join_into_a_phrase() -> None:
    edited = V1.replace("Dear guest,", "Dear guest, free").replace("Kind", "upgrade Kind")
    assert failed(checks(edited)) == set()
