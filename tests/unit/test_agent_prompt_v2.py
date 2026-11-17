"""agent_v2 (week 6): what changed from agent_v1, and what its pinned version covers."""

import json
import re
from typing import Any

import pytest

from anemos.agent.loop import DRAFT_REFUSED
from anemos.agent.tools import CHECK_AVAILABILITY
from anemos.guardrails import no_forbidden_phrases
from anemos.llm.openai_client import function_tool
from anemos.prompts import AGENT_V1, AGENT_V2, tool_prompt_hash

pytestmark = pytest.mark.unit

TEXT = " ".join(AGENT_V2.text().split())


def test_agent_v2_pin_covers_its_own_tools() -> None:
    tools: dict[str, Any] = {r: [function_tool(t) for t in ts] for r, ts in AGENT_V2.tools.items()}
    pinned = tool_prompt_hash(AGENT_V2.text(), tools, max_output_tokens=AGENT_V2.max_output_tokens,
                              reasoning_effort=AGENT_V2.reasoning_effort)  # fmt: skip
    assert pinned == AGENT_V2.sha256_8
    assert AGENT_V1.current_hash() == AGENT_V1.sha256_8  # v1 still names what it sent


def test_no_route_offers_check_availability_and_the_prompt_does_not_mention_it() -> None:
    assert all(t.name != CHECK_AVAILABILITY for ts in AGENT_V2.tools.values() for t in ts)
    assert CHECK_AVAILABILITY not in TEXT


def test_the_redraft_instruction_quotes_the_loops_exact_refusal() -> None:
    """The model must recognise the refusal the loop sends (D76), not the escalation code."""
    refusal = json.dumps({"error": DRAFT_REFUSED, "failed": ["..."]})
    assert refusal.replace('"..."', "...") in TEXT
    assert "GUARDRAIL_FAIL" not in TEXT
    assert (
        "call write_draft once more" in TEXT and "A second refused reply goes to the owner" in TEXT
    )


def test_policy_numbers_may_be_copied_exactly_and_no_other_digits() -> None:
    assert "may be copied exactly as the section writes it" in TEXT
    assert "Write no other digits" in TEXT


def test_the_summary_on_ask_info_whenever_the_guest_gave_dates() -> None:
    assert "use it whenever the guest gave dates: it shows only what is already known" in TEXT


def test_the_forbidden_words_are_named_in_english_and_greek() -> None:
    line = TEXT[TEXT.index("Do not use these words at all") : TEXT.index("## When write_draft")]
    for word in ("discount", "guaranteed", "έκπτωση", "εγγυημένο", "εγγυημένη",
                 "επιβεβαιωμένη κράτηση"):  # fmt: skip
        assert f'"{word}' in line, word
    for phrase in re.findall(r'"([^"]+)"', line):  # guardrail 5 refuses each one
        assert no_forbidden_phrases(phrase, []) is not True, phrase


def test_greek_appears_only_in_the_forbidden_words() -> None:
    paragraph = TEXT[
        TEXT.index("Do not use these words at all") : TEXT.index("## When write_draft")
    ]
    greek_lines = [ln for ln in AGENT_V2.text().splitlines() if re.search(r"[Ͱ-Ͽ]", ln)]
    assert greek_lines and all(" ".join(ln.split()) in paragraph for ln in greek_lines)
