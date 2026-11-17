"""D80 (owner, week 6): code inserts {{REQUEST_SUMMARY}} after the greeting wherever
guardrail 3 requires it; the model's own is kept, never a second one."""

import pytest

from anemos.agent.checks import DraftFacts, check_draft, with_summary
from anemos.guardrails import REQUEST_SUMMARY

pytestmark = pytest.mark.unit

S = REQUEST_SUMMARY
TEXT = "Dear Helen,\n\nThank you for writing.\n\nKind regards,\nHotel Anemos"
WITH = f"Dear Helen,\n\n{S}\n\nThank you for writing.\n\nKind regards,\nHotel Anemos"


def facts(*, relative: bool = False, summarisable: bool = True) -> DraftFacts:
    return DraftFacts(router_route="quote", lang="en", summarisable=summarisable,
                      relative_or_holiday=relative)  # fmt: skip


TABLE = [
    ("quote: inserted after the greeting", TEXT, "quote", facts(), WITH),
    ("decline: inserted", TEXT, "decline", facts(), WITH),
    ("ask_info, relative date: inserted", TEXT, "ask_info", facts(relative=True), WITH),
    ("ask_info, plain dates: not required", TEXT, "ask_info", facts(), TEXT),
    ("policy: not required", TEXT, "policy", facts(), TEXT),
    ("the model wrote it: kept where it is, not twice",
     f"Dear Helen,\n\nThank you.\n\n{S}", "quote", facts(), f"Dear Helen,\n\nThank you.\n\n{S}"),
    ("cannot render: nothing inserted, G3 fails as before", TEXT, "quote",
     facts(summarisable=False), TEXT),
    ("no blank line: inserted first", "Dear Helen, thank you.", "quote", facts(),
     f"{S}\n\nDear Helen, thank you."),
]  # fmt: skip


@pytest.mark.parametrize(("label", "text", "route", "f", "expected"), TABLE,
                         ids=[t[0] for t in TABLE])  # fmt: skip
def test_with_summary(label: str, text: str, route: str, f: DraftFacts, expected: str) -> None:
    assert with_summary(text, route, f) == expected, label


def test_check_draft_checks_and_returns_the_text_with_the_summary() -> None:
    """A decline with no summary from the model passes guardrail 3: code inserted it,
    and the final write renders `Checked.text`."""
    checked = check_draft(TEXT, [], [], facts(), quotes={}, hits={}, outcomes={"NONE_FOUND"},
                          closed_year=None)  # fmt: skip
    assert checked.route == "decline" and checked.passed, checked.failures
    assert checked.text == WITH
