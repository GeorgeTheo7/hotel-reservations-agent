"""The agent eval's scoring (anemos.agent_evaluation): each bar line, and the reported
items outside it. Pure: synthetic cases, no run."""

from typing import Any

import pytest

from anemos.agent_evaluation import (
    AgentCase,
    ToolUse,
    availability_calls,
    bar,
    expected_script,
    own_digits,
    percentile,
    provider_failure,
    reported,
    script_of,
)
from anemos.evaluation import FIELDS, CaseOutcome, Row

pytestmark = pytest.mark.unit

RIGHT = dict.fromkeys(FIELDS, True)


def case(case_id: str = "c1", route: str = "quote", expected: str = "quote",
         codes: tuple[str, ...] = (), expected_codes: tuple[str, ...] = (),
         language: str = "en", **fields: Any) -> AgentCase:  # fmt: skip
    outcome = CaseOutcome(case_id, expected, expected_codes, 0, route, codes, 0, False)
    row = Row(case_id, "gpt-6-luna", fields=dict(RIGHT), gold_kind="request")
    return AgentCase(case_id=case_id, language=language, outcome=outcome, extraction=row,
                     **fields)  # fmt: skip


def line(cases: list[AgentCase], name: str) -> Any:
    return next(x for x in bar(cases) if x.name.startswith(name))


def test_a_clean_run_passes_every_line() -> None:
    cases = [case(f"c{i}", draft_body="Dear guest", cost_micro_usd=1_000, latency_ms=3_000)
             for i in range(10)]  # fmt: skip
    cases.append(case("e", route="escalate", expected="escalate", codes=("DISCOUNT",),
                      expected_codes=("DISCOUNT",)))  # fmt: skip
    cases.append(case("p", draft_body="x", policy_ids=["wifi"], retrieved=["wifi"]))
    assert [x.name for x in bar(cases) if not x.passed] == []


def test_a_metric_with_nothing_to_measure_fails() -> None:
    """No labelled escalation or policy case: recall is n/a, which never passes."""
    assert line([case()], "escalation recall").value.startswith("n/a")
    assert not line([case()], "policy recall").passed


def test_any_unsafe_action_or_write_tool_call_fails_the_bar() -> None:
    assert not line([case(unsafe=["hold on [3] breaks D9"])], "unsafe").passed
    assert not line([case(write_tool_calls=["hold_room"])], "unsafe").passed


def test_system_error_counts_unless_a_provider_failure_was_rerun() -> None:
    """A rerun case that ended well has no system_error; one that failed again counts."""
    rerun = case(reran=True, first_error="LLMError: agent: TransientError: HTTP 503")
    assert line([rerun], "SYSTEM_ERROR").passed
    assert not line([case(system_error="reply cut off at the output limit")],
                    "SYSTEM_ERROR").passed  # fmt: skip


def test_route_accuracy_needs_route_codes_and_holds_and_leaves_out_secondary_codes() -> None:
    cases = [case("c1"), case("c2", route="escalate", expected="escalate", codes=("MODEL",),
                              expected_codes=("DISCOUNT",)),
             case("el-group-wedding", route="escalate", expected="escalate",
                  codes=("GROUP",), expected_codes=("GROUP", "DISCOUNT"))]  # fmt: skip
    route = line(cases, "route accuracy")
    assert route.value == "50% (1/2)" and not route.passed and "c2" in route.detail


def test_escalation_recall_and_precision_differ() -> None:
    """Inputs where the two metrics differ (NOTES: a metric test needs that)."""
    cases = [case("a", route="escalate", expected="escalate", codes=("X",), expected_codes=("X",)),
             case("b", route="quote", expected="escalate", expected_codes=("X",)),
             case("c", route="quote", expected="escalate", expected_codes=("X",)),
             case("d", route="escalate", expected="quote", codes=("X",))]  # fmt: skip
    assert line(cases, "escalation recall").value == "33% (1/3)"
    assert line(cases, "escalation precision").value == "50% (1/2)"


def test_policy_recall_needs_every_labelled_id() -> None:
    cases = [case("a", policy_ids=["wifi", "parking"], retrieved=["wifi"]),
             case("b", policy_ids=["pets-assistance-dogs"], retrieved=["pets-assistance-dogs"]),
             case("c")]  # fmt: skip
    assert line(cases, "policy recall").value == "50% (1/2)"


def test_first_draft_share_counts_guardrail_failures() -> None:
    cases = [case("a", draft_body="x"), case("b", guardrail_fail=True, route="escalate",
                                             codes=("GUARDRAIL_FAIL",))]  # fmt: skip
    assert line(cases, "drafts passing").value == "50% (1/2)"


def test_cost_and_latency_use_mean_and_p95() -> None:
    cases = [case(f"c{i}", cost_micro_usd=1_000, latency_ms=1_000) for i in range(19)]
    cases.append(case("big", cost_micro_usd=30_000, latency_ms=70_000))
    assert line(cases, "cost").passed  # mean 2,450 µ$; p95 is the 19th of 20: 1,000
    cases.append(case("big2", cost_micro_usd=30_000, latency_ms=70_000))
    assert not line(cases, "cost").passed and not line(cases, "latency").passed


def test_percentile_is_nearest_rank() -> None:
    assert percentile([5, 1, 3, 2, 4], 0.95) == 5 and percentile([1, 2], 0.5) == 1
    assert percentile([], 0.95) is None


def test_provider_failures_are_only_transient_errors() -> None:
    assert provider_failure("LLMError: agent: TransientError: HTTP 503: busy")
    assert not provider_failure("reply cut off at the output limit")
    assert not provider_failure("BudgetExceeded: cap reached") and not provider_failure(None)


def test_script_and_own_digits_preview_guardrails_4_and_2() -> None:
    assert (script_of("Αγαπητέ Γιώργο"), script_of("Kalimera")) == ("greek", "latin")
    assert expected_script("el") == "greek" and expected_script("en") == "latin"
    assert own_digits("Dear guest, {{QUOTE_BLOCK}} see you on 12 June") == 2


def test_check_availability_counts() -> None:
    c = case(tools=[ToolUse("check_availability", "AVAILABLE"),
                    ToolUse("check_availability", "NOT_FOR_THIS_REQUEST"),
                    ToolUse("quote_price", "QUOTED")])  # fmt: skip
    assert availability_calls(c) == (2, 1, True)


def test_reported_items() -> None:
    cases = [
        case("a", language="el", model_text="Dear guest, 3 nights",
             draft_body="booking is confirmed", forbidden_phrases=["booking is confirmed"],
             required_tools=["quote_price"], agent_turns=3, max_output_tokens=420,
             tools=[ToolUse("search_policies", "INVALID_QUERY", "Greek letters")]),
    ]  # fmt: skip
    got = reported(cases)
    assert got["script_mismatch"] == {"a": "latin"}
    assert got["digits_in_own_text"] == {"a": 1}
    assert got["forbidden_phrases"] == {"a": ["booking is confirmed"]}
    assert got["required_tools_missing"] == {"a": ["quote_price"]}
    assert got["invalid_queries"] == {"a": ["Greek letters"]}
    assert got["max_output_tokens_one_turn"] == 420
