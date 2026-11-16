"""Policy chunks (D15) and D39: the quote templates agree with the policies.

D39 compares facts, not wording: each policy fact the templates state maps to a chunk
and to values or keywords that must appear in the chunk and in the templates of both
languages. A reworded sentence passes; a different number (a 25% deposit) fails.
The chunks' own numbers are checked against the database in
tests/integration/test_policy_facts.py.
"""

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import pytest

from anemos.approval import HOLD_FOR
from anemos.email_cases import load_cases
from anemos.policies import (
    POLICY_ID,
    PolicyFormatError,
    load_policies,
    parse_policies,
    policy,
)
from anemos.reservations.formatting import Lang
from anemos.reservations.money import DEPOSIT_PERCENT

pytestmark = pytest.mark.unit

EMAILS = Path(__file__).resolve().parents[2] / "eval" / "emails"
GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")


# --- format ------------------------------------------------------------------------------


def test_the_packaged_policies_parse_into_about_25_chunks() -> None:
    chunks = load_policies()
    assert 20 <= len(chunks) <= 30
    ids = [c.policy_id for c in chunks]
    titles = [c.title for c in chunks]
    assert len(set(ids)) == len(ids) and len(set(titles)) == len(titles)
    assert all(POLICY_ID.fullmatch(i) for i in ids)


def test_chunks_are_english_only() -> None:
    """The model writes queries in English (D15); Greek text would split the index."""
    greek = [c.policy_id for c in load_policies() if GREEK.search(c.title + c.text)]
    assert greek == []


def test_no_room_rates_in_the_policies() -> None:
    """Room prices come only from quote_price (docs/NOTES.md); a rate in a chunk would
    let a draft quote it as policy text (guardrail 2 allows chunk substrings)."""
    text = " ".join(c.text for c in load_policies()).lower()
    assert "per room-night" not in text and "room rate" not in text
    euros = set(re.findall(r"€(\d+(?:\.\d+)?)", text))
    assert euros == {"1.50", "4.00", "25.00", "20.00", "35.00"}  # city tax, transfer, extra beds


def test_every_policy_id_in_the_eval_labels_is_a_chunk() -> None:
    ids = {c.policy_id for c in load_policies()}
    labelled = {(case.id, p) for case in load_cases(EMAILS) for p in case.labels.policy_ids}
    assert labelled, "no case names a policy yet"
    assert {(case_id, p) for case_id, p in labelled if p not in ids} == set()


def test_the_three_policy_cases_name_their_chunk() -> None:
    cases = {c.id: c for c in load_cases(EMAILS)}
    assert cases["el-pets-policy"].labels.policy_ids == ["pets-assistance-dogs"]
    assert cases["el-greeklish-transfer-policy"].labels.policy_ids == ["transfer"]
    assert cases["en-quote-plus-breakfast"].labels.policy_ids == ["breakfast"]


def test_policy_looks_up_by_id() -> None:
    assert policy("breakfast").title == "Breakfast"
    with pytest.raises(KeyError):
        policy("swimming-pool")


GOOD = "<!-- intro -->\n\n## Breakfast\n<!-- policy_id: breakfast -->\nIncluded.\n"


def test_parse_reads_title_id_and_text() -> None:
    (chunk,) = parse_policies(GOOD)
    assert (chunk.policy_id, chunk.title, chunk.text) == ("breakfast", "Breakfast", "Included.")


@pytest.mark.parametrize(
    ("markdown", "message"),
    [
        (GOOD + GOOD.split("\n\n", 1)[1], "duplicate"),
        ("## Breakfast\nIncluded.\n", "policy_id"),
        ("## Breakfast\n<!-- policy_id: Break_fast -->\nIncluded.\n", "lowercase"),
        ("## Breakfast\n<!-- policy_id: breakfast -->\n\n", "empty"),
        ("Intro text\n" + GOOD, "before the first"),
        ("## B\n<!-- policy_id: b -->\nText <!-- note --> more.\n", "comment"),
    ],
)
def test_parse_refuses_a_broken_format(markdown: str, message: str) -> None:
    with pytest.raises(PolicyFormatError, match=message):
        parse_policies(markdown)


# --- D39: template facts agree with the policy chunks -----------------------------------


@dataclass(frozen=True)
class TemplateFact:
    """One policy fact the quote templates state. Every term must appear in the chunk
    and in the templates (case-insensitive; a number must not be part of a longer
    number). Words differ by language, values do not."""

    name: str
    policy_id: str
    chunk: tuple[str, ...]
    en: tuple[str, ...]
    el: tuple[str, ...]


DEPOSIT = f"{DEPOSIT_PERCENT}%"
HOLD_HOURS = str(int(HOLD_FOR.total_seconds() // 3600))

TEMPLATE_FACTS = (
    TemplateFact("breakfast included", "breakfast",
                 chunk=("breakfast", "included"), en=("breakfast", "included"),
                 el=("πρωινό", "περιλαμβάνεται")),
    TemplateFact("first night charged after free cancellation", "cancellation",
                 chunk=("first night", "free"), en=("first night", "free"),
                 el=("πρώτη νύχτα", "δωρεάν")),
    TemplateFact("deposit percentage", "deposit",
                 chunk=(DEPOSIT, "deposit"), en=(DEPOSIT, "deposit"), el=(DEPOSIT, "προκαταβολή")),
    TemplateFact("deposit deadline for a held quote", "deposit",
                 chunk=(HOLD_HOURS, "hours"), en=(HOLD_HOURS, "hours"), el=(HOLD_HOURS, "ωρών")),
)  # fmt: skip


def template_text(lang: Lang) -> str:
    """The quote template and every phrase it can use, as one text."""
    folder = files("anemos.reservations").joinpath("templates")
    quote = folder.joinpath(f"quote_{lang}.txt").read_text(encoding="utf-8")
    phrases = json.loads(folder.joinpath(f"phrases_{lang}.json").read_text(encoding="utf-8"))
    values = [v for v in phrases.values() if isinstance(v, str)]
    return "\n".join([quote, *values])


def contains(text: str, term: str) -> bool:
    if term[0].isdigit():
        return re.search(rf"(?<![\d.,]){re.escape(term)}(?![\d])", text) is not None
    return term.casefold() in text.casefold()


@pytest.mark.parametrize("fact", TEMPLATE_FACTS, ids=lambda f: f.name)
def test_template_fact_is_in_its_policy_chunk(fact: TemplateFact) -> None:
    text = policy(fact.policy_id).text
    assert [t for t in fact.chunk if not contains(text, t)] == []


@pytest.mark.parametrize("lang", ["en", "el"])
@pytest.mark.parametrize("fact", TEMPLATE_FACTS, ids=lambda f: f.name)
def test_template_fact_is_in_the_templates(fact: TemplateFact, lang: Lang) -> None:
    text = template_text(lang)
    terms = fact.en if lang == "en" else fact.el
    assert [t for t in terms if not contains(text, t)] == []


# Every value of these kinds in the templates must be the policy value: a template with
# two deposit sentences must not pass with one of them changed to 25%.
TEMPLATE_VALUES: dict[Lang, list[tuple[str, str]]] = {
    "en": [(r"(\d+)\s*%", str(DEPOSIT_PERCENT)), (r"(\d+)\s+hours", HOLD_HOURS)],
    "el": [(r"(\d+)\s*%", str(DEPOSIT_PERCENT)), (r"(\d+)\s+ωρ", HOLD_HOURS)],
}


@pytest.mark.parametrize("lang", ["en", "el"])
def test_every_percentage_and_hour_count_in_the_templates_is_the_policy_value(
    lang: Lang,
) -> None:
    text = template_text(lang)
    for pattern, value in TEMPLATE_VALUES[lang]:
        found = re.findall(pattern, text)
        assert found and set(found) == {value}, (pattern, found)


def test_a_number_inside_a_longer_number_does_not_count() -> None:
    assert contains("30% deposit", "30%")
    assert not contains("130% deposit", "30%") and not contains("0.30% deposit", "30%")
    assert contains("within 48 hours", "48") and not contains("within 480 hours", "48")
