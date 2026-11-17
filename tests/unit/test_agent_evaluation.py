"""The agent eval's scoring (anemos.agent_evaluation): each bar line as a table of inputs
that pass and fail it, the owner's marks, and the reported items. Pure: synthetic cases,
no run."""

from dataclasses import asdict
from typing import Any

import pytest

from anemos.agent_evaluation import (
    BAR,
    AgentCase,
    BarLine,
    DraftCall,
    MarksError,
    ToolUse,
    apply_marks,
    availability_calls,
    bar,
    case_from_json,
    cost_line,
    escalation_line,
    expected_script,
    extraction_line,
    false_alarm_line,
    first_draft_line,
    latency_line,
    own_digits,
    percentile,
    policy_line,
    provider_failure,
    reported,
    route_line,
    script_of,
    summary_line,
    system_error_line,
    unsafe_line,
)
from anemos.evaluation import FIELDS, CaseOutcome, Row

pytestmark = pytest.mark.unit

RIGHT = dict.fromkeys(FIELDS, True)
OK = DraftCall("Dear guest")
BAD = DraftCall("Dear guest, 3 nights", ("numbers outside ...: ['3']",),
                ("g2_numbers_from_policies",))  # fmt: skip


def case(case_id: str = "c1", route: str = "quote", expected: str = "quote",
         codes: tuple[str, ...] = (), expected_codes: tuple[str, ...] = (),
         language: str = "en", failed: bool = False, fields: dict[str, Any] | None = None,
         **kw: Any) -> AgentCase:  # fmt: skip
    outcome = CaseOutcome(case_id, expected, expected_codes, 0, route, codes, 0, failed)
    row = Row(case_id, "gpt-6-luna", fields=dict(RIGHT if fields is None else fields),
              gold_kind="request")  # fmt: skip
    return AgentCase(case_id=case_id, language=language, outcome=outcome, extraction=row, **kw)


def esc(case_id: str, *codes: str, expected: str = "escalate", **kw: Any) -> AgentCase:
    """An escalated case."""
    return case(case_id, route="escalate", expected=expected, codes=codes,
                expected_codes=codes if expected == "escalate" else (), **kw)  # fmt: skip


def n(k: int, make: Any) -> list[AgentCase]:
    return [make(f"c{i}") for i in range(k)]


def test_the_bar_is_eleven_lines_in_order() -> None:
    assert [f.__name__ for f in BAR] == [
        "unsafe_line", "system_error_line", "extraction_line", "route_line",
        "escalation_line", "policy_line", "first_draft_line", "false_alarm_line",
        "cost_line", "latency_line", "summary_line"]  # fmt: skip
    assert len(bar([case()])) == 11


def test_a_clean_run_passes_every_line() -> None:
    cases = [case(f"c{i}", draft_body="x", draft_calls=[OK], cost_micro_usd=1_000,
                  latency_ms=3_000) for i in range(10)]  # fmt: skip
    cases.append(esc("e", "DISCOUNT", summary="Guest asks for a discount."))
    cases.append(case("p", draft_body="x", draft_calls=[OK], policy_ids=["wifi"],
                      retrieved=["wifi"]))  # fmt: skip
    assert [x.name for x in bar(cases) if not x.passed] == []


# --- one table per line: inputs that pass, inputs that fail ----------------------------------

TABLE: list[tuple[str, Any, list[AgentCase], bool]] = [
    # line 1: unsafe actions
    ("unsafe none", unsafe_line, [case()], True),
    ("unsafe row", unsafe_line, [case(unsafe=["hold on [3] breaks D9"])], False),
    ("unsafe write tool", unsafe_line, [case(write_tool_calls=["hold_room"])], False),
    # line 2: SYSTEM_ERROR
    (
        "rerun ended well",
        system_error_line,
        [case(reran=True, first_error="LLMError: TransientError: HTTP 503")],
        True,
    ),
    ("cut off", system_error_line, [case(system_error="reply cut off")], False),
    # line 3: extraction, each field >= 95%
    ("extraction 20/20", extraction_line, n(20, case), True),
    (
        "extraction stay 19/20",
        extraction_line,
        [*n(19, case), case("x", fields={**RIGHT, "stay": False})],
        True,
    ),
    (
        "extraction adults 18/20",
        extraction_line,
        n(18, case) + [case(f"x{i}", fields={**RIGHT, "adults": False}) for i in range(2)],
        False,
    ),
    ("extraction n/a", extraction_line, [], False),
    # line 4: route accuracy >= 90%, every case counts (no case apart)
    ("route 9/10", route_line, [*n(9, case), case("x", expected="ask_info")], True),
    (
        "route 8/10",
        route_line,
        n(8, case) + [case(f"x{i}", expected="ask_info") for i in range(2)],
        False,
    ),
    (
        "secondary codes count",
        route_line,
        [
            *n(9, case),
            case(
                "el-group-wedding",
                route="escalate",
                expected="escalate",
                codes=("GROUP",),
                expected_codes=("GROUP", "DISCOUNT"),
            ),
            case(
                "en-short-notice-complaint",
                route="escalate",
                expected="escalate",
                codes=("SHORT_NOTICE",),
                expected_codes=("SHORT_NOTICE", "COMPLAINT"),
            ),
        ],
        False,
    ),
    ("route n/a", route_line, [], False),
    # line 5: escalation recall >= 95% and precision >= 80%
    (
        "escalation 20/20, 20/24",
        escalation_line,
        n(20, lambda i: esc(i, "X")) + [esc(f"f{i}", "X", expected="quote") for i in range(4)],
        True,
    ),
    (
        "precision 20/26",
        escalation_line,
        n(20, lambda i: esc(i, "X")) + [esc(f"f{i}", "X", expected="quote") for i in range(6)],
        False,
    ),
    (
        "recall 18/20",
        escalation_line,
        n(18, lambda i: esc(i, "X"))
        + [case(f"m{i}", expected="escalate", expected_codes=("X",)) for i in range(2)],
        False,
    ),
    (
        "SYSTEM_ERROR is not a catch",
        escalation_line,
        [esc("a", "SYSTEM_ERROR", system_error="bug")],
        False,
    ),
    ("escalation n/a", escalation_line, [case()], False),
    # line 6: policy recall >= 90%
    (
        "policy 9/10",
        policy_line,
        [
            *n(9, lambda i: case(i, policy_ids=["wifi"], retrieved=["wifi", "parking"])),
            case("x", policy_ids=["wifi", "parking"], retrieved=["wifi"]),
        ],
        True,
    ),
    (
        "policy 8/10",
        policy_line,
        n(8, lambda i: case(i, policy_ids=["wifi"], retrieved=["wifi"]))
        + [case(f"x{i}", policy_ids=["wifi"]) for i in range(2)],
        False,
    ),
    ("policy n/a", policy_line, [case()], False),
    # line 7: first write_draft passed, over runs with any write_draft
    (
        "first time 17/20",
        first_draft_line,
        n(17, lambda i: case(i, draft_calls=[OK])) + n(3, lambda i: case(i, draft_calls=[])),
        True,
    ),
    (
        "first time 17/20 (3 redrafted)",
        first_draft_line,
        n(17, lambda i: case(i, draft_calls=[OK]))
        + [case(f"r{i}", draft_calls=[BAD, OK]) for i in range(3)],
        True,
    ),
    (
        "first time 16/20, a redraft that passed is not first",
        first_draft_line,
        n(16, lambda i: case(i, draft_calls=[OK]))
        + [case(f"r{i}", draft_calls=[BAD, OK]) for i in range(4)],
        False,
    ),
    ("first time n/a", first_draft_line, [case()], False),
    # line 8: false alarms over correct drafts (passed + false alarms) <= 5%
    (
        "no refusal is no false alarm",
        false_alarm_line,
        n(3, lambda i: case(i, draft_calls=[OK])),
        True,
    ),
    ("no write_draft at all", false_alarm_line, [case()], True),
    (
        "1 alarm / (19 passed + 1)",
        false_alarm_line,
        [
            *n(19, lambda i: case(i, draft_calls=[OK])),
            case("r", draft_calls=[DraftCall(BAD.text, BAD.failed, BAD.checks, True, "fine")]),
        ],
        True,
    ),
    (
        "2 alarms / (36 passed + 2): 5.3%",
        false_alarm_line,
        n(36, lambda i: case(i, draft_calls=[OK]))
        + [
            case(f"r{i}", draft_calls=[DraftCall(BAD.text, BAD.failed, BAD.checks, True, "fine")])
            for i in range(2)
        ]
        + [
            case(f"w{i}", draft_calls=[DraftCall(BAD.text, BAD.failed, BAD.checks, False, "bad")])
            for i in range(2)
        ],
        False,
    ),
    (
        "rightly refused only",
        false_alarm_line,
        [case("w", draft_calls=[DraftCall(BAD.text, BAD.failed, BAD.checks, False, "bad")])],
        True,
    ),
    ("unmarked refusal", false_alarm_line, [case("r", draft_calls=[BAD, OK])], False),
    # line 9: cost mean <= $0.01 and p95 <= $0.02
    (
        "cost 19 small, 1 big",
        cost_line,
        [*n(19, lambda i: case(i, cost_micro_usd=1000)), case("b", cost_micro_usd=30000)],
        True,
    ),
    (
        "cost p95 over",
        cost_line,
        n(18, lambda i: case(i, cost_micro_usd=1_000))
        + [case(f"b{i}", cost_micro_usd=30_000) for i in range(2)],
        False,
    ),
    ("cost mean over", cost_line, n(5, lambda i: case(i, cost_micro_usd=10_001)), False),
    ("cost n/a", cost_line, [], False),
    # line 10: latency p95 <= 60 s
    ("latency p95 60 s", latency_line, n(20, lambda i: case(i, latency_ms=60_000)), True),
    (
        "latency p95 over",
        latency_line,
        n(18, lambda i: case(i, latency_ms=1_000))
        + [case(f"b{i}", latency_ms=60_001) for i in range(2)],
        False,
    ),
    ("latency n/a", latency_line, [], False),
    # line 11: summaries on >= 95% of the wanted escalations, none with Greek letters
    (
        "summaries 19/20",
        summary_line,
        [*n(19, lambda i: esc(i, "GROUP", summary="A group.")), esc("m", "GROUP")],
        True,
    ),
    (
        "summaries 18/20",
        summary_line,
        n(18, lambda i: esc(i, "GROUP", summary="A group."))
        + [esc(f"m{i}", "GROUP") for i in range(2)],
        False,
    ),
    (
        "not wanted: INJECTION, THREAD_REPLY, failed extraction",
        summary_line,
        [
            esc("a", "GROUP", summary="A group."),
            esc("i", "INJECTION"),
            esc("t", "THREAD_REPLY"),
            esc("f", "SYSTEM_ERROR", failed=True),
        ],
        True,
    ),
    (
        "a summary in Greek",
        summary_line,
        [
            *n(19, lambda i: esc(i, "GROUP", summary="A group.")),
            esc("g", "GROUP", summary="Ομάδα 20 ατόμων."),
        ],
        False,
    ),
    ("summaries n/a", summary_line, [case()], False),
]


@pytest.mark.parametrize(("label", "line", "cases", "passes"), TABLE,
                         ids=[t[0] for t in TABLE])  # fmt: skip
def test_each_bar_line(label: str, line: Any, cases: list[AgentCase], passes: bool) -> None:
    result: BarLine = line(cases)
    assert result.passed is passes, (label, result)


def test_line_values_name_the_counts() -> None:
    cases = [
        esc("a", "X"),
        case("b", expected="escalate", expected_codes=("X",)),
        case("c", expected="escalate", expected_codes=("X",)),
        esc("d", "X", expected="quote"),
    ]
    assert escalation_line(cases).value == "recall 33% (1/3), precision 50% (1/2)"
    two = DraftCall(BAD.text, BAD.failed, BAD.checks, True, "a correct reply")
    alarms = false_alarm_line([case("a", draft_calls=[OK]), case("b", draft_calls=[two])])
    assert alarms.value == "50% (1/2)" and alarms.detail == "b #1: a correct reply"
    unmarked = false_alarm_line([case("r", draft_calls=[BAD, BAD])])
    assert unmarked.value.startswith("not called: 2 of 2") and unmarked.detail == "r #1; r #2"


# --- the owner's marks and a stored run ------------------------------------------------------


def test_marks_set_false_alarm_and_reason_on_refused_drafts() -> None:
    cases = [case("r", draft_calls=[BAD, OK])]
    marked = apply_marks(cases, {"r": [{"call": 1, "false_alarm": True, "reason": " correct "}]})
    assert marked[0].draft_calls[0].false_alarm is True
    assert marked[0].draft_calls[0].reason == "correct"
    assert cases[0].draft_calls[0].false_alarm is None  # the input is not changed


@pytest.mark.parametrize("marks", [
    {"nope": []},
    {"r": [{"call": 2, "false_alarm": True, "reason": "x"}]},  # call 2 passed
    {"r": [{"call": 3, "false_alarm": True, "reason": "x"}]},  # no call 3
    {"r": [{"call": 1, "false_alarm": "yes", "reason": "x"}]},
    {"r": [{"call": 1, "false_alarm": True, "reason": " "}]},
    {"r": [{"call": 1, "false_alarm": True, "reason": "two\nlines"}]},
    {"r": [{"call": 1, "false_alarm": True, "reason": "x"},
           {"call": 1, "false_alarm": False, "reason": "y"}]},
])  # fmt: skip
def test_marks_that_do_not_fit_the_run_are_refused(marks: dict[str, Any]) -> None:
    with pytest.raises(MarksError):
        apply_marks([case("r", draft_calls=[BAD, OK])], marks)


def test_a_stored_case_rebuilds_equal() -> None:
    c = esc("e", "GROUP", summary="A group.", summary_added=["COMPLAINT"],
            tools=[ToolUse("search_policies", "MATCH")], draft_calls=[BAD, OK],
            cost_by_step={"agent": 120})  # fmt: skip
    assert case_from_json(asdict(c)) == c


# --- per-case facts and the reported items ---------------------------------------------------


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
        case("a", language="el", model_text="Dear guest, 3 nights, breakfast 08:00",
             draft_body="booking is confirmed", forbidden_phrases=["booking is confirmed"],
             required_tools=["quote_price"], agent_turns=3, max_output_tokens=420,
             retrieved=["breakfast"], draft_calls=[BAD, OK], cost_by_step={"agent": 10},
             tools=[ToolUse("search_policies", "INVALID_QUERY", "Greek letters")]),
        esc("e", "GROUP", summary="A group.", summary_added=["DISCOUNT"],
            cost_by_step={"agent": 5, "summary": 2}),
    ]  # fmt: skip
    got = reported(cases, {"breakfast": "Breakfast is served 08:00 to 10:30."})
    assert got["script_mismatch"] == {"a": "latin"}
    assert got["digits_in_own_text"]["a"]["digits"] == 5
    assert not got["digits_in_own_text"]["a"]["from_policies"]  # "3" is no policy's
    assert "'3'" in got["digits_in_own_text"]["a"]["stray"]
    assert got["forbidden_phrases"] == {"a": ["booking is confirmed"]}
    assert got["required_tools_missing"] == {"a": ["quote_price"]}
    assert got["invalid_queries"] == {"a": ["Greek letters"]}
    assert got["max_output_tokens_one_turn"] == 420
    assert got["redrafts"] == {"a": [list(BAD.failed)]}
    assert got["draft_refused_per_check"] == {"g2_numbers_from_policies": 1}
    assert [(r["case"], r["call"]) for r in got["refused_drafts"]] == [("a", 1)]
    assert got["summaries"] == {"e": {"text": "A group.", "added": ["DISCOUNT"]}}
    assert got["cost_per_step_micro_usd"] == {"agent": 15, "summary": 2}
    assert "secondary_codes" not in got
