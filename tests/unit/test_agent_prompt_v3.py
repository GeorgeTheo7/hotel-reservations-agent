"""agent_v3 (week 6, owner): code inserts {{REQUEST_SUMMARY}} (D80), so the prompt no
longer names it and forbids dates, nights and guest counts; everything else is v2's."""

import re
from typing import Any

import pytest

from anemos.guardrails import REQUEST_SUMMARY, no_forbidden_phrases
from anemos.llm.openai_client import function_tool
from anemos.prompts import AGENT_V2, AGENT_V3, CURRENT_AGENT, tool_prompt_hash

pytestmark = pytest.mark.unit

TEXT = " ".join(AGENT_V3.text().split())
V2 = " ".join(AGENT_V2.text().split())


def test_agent_v3_is_current_and_its_pin_covers_its_own_tools() -> None:
    assert CURRENT_AGENT is AGENT_V3
    assert AGENT_V3.tools is AGENT_V2.tools  # v2's tools, unchanged
    tools: dict[str, Any] = {r: [function_tool(t) for t in ts] for r, ts in AGENT_V3.tools.items()}
    pinned = tool_prompt_hash(AGENT_V3.text(), tools, max_output_tokens=AGENT_V3.max_output_tokens,
                              reasoning_effort=AGENT_V3.reasoning_effort)  # fmt: skip
    assert pinned == AGENT_V3.sha256_8
    assert AGENT_V2.current_hash() == AGENT_V2.sha256_8  # v2 still names what it sent


def test_the_prompt_never_names_the_summary_placeholder() -> None:
    assert REQUEST_SUMMARY not in TEXT and "REQUEST_SUMMARY" not in TEXT


def test_the_summary_is_added_automatically_and_dates_nights_guests_never_written() -> None:
    assert "The request summary" in TEXT and "is added automatically" in TEXT
    assert "code puts it after your greeting" in TEXT
    assert ("never write a date, a number of nights or a number of guests, in digits or in"
            " words") in TEXT  # fmt: skip
    assert "Write no other digits" in TEXT  # guardrail 2, as in v2


def test_only_the_summary_text_changed_from_v2() -> None:
    """The rest is v2's word for word: the refusal, the escalation codes, the words."""
    parts = [("## When write_draft is refused", "## Handing"),
             ("## Handing the email to the owner", "fits best."),
             ("Do not use these words at all", "## When write_draft"),
             ("{{QUOTE_BLOCK}}: exactly once", "when you offer nothing."),
             ("{{OPEN_SEASON}}: the months", "There are no other placeholders.")]  # fmt: skip
    for start, end in parts:
        assert (
            V2[V2.index(start) : V2.index(end, V2.index(start))]
            == TEXT[TEXT.index(start) : TEXT.index(end, TEXT.index(start))]
        ), start


def test_the_forbidden_words_are_still_refused_by_guardrail_5() -> None:
    line = TEXT[TEXT.index("Do not use these words at all") : TEXT.index("## When write_draft")]
    phrases = re.findall(r'"([^"]+)"', line)
    assert len(phrases) == 9
    assert all(no_forbidden_phrases(p, []) is not True for p in phrases)
