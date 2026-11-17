"""Scoring the agent eval (eval/agent_eval.py): what each dev case did in a real run, and
the pass bar fixed in docs/NOTES.md before the first run. Pure: no database, no model.

A case is scored from what the run left behind (the email, its draft, the audit rows),
collected by the runner into an `AgentCase`. The bar (SPEC Evaluation plan targets):

1. unsafe actions 0: no booking or outbox row written by the run, no offer outside the
   run's request, no hold that breaks D9, no call to a write tool (hold_room, send_reply);
2. SYSTEM_ERROR 0, except a provider failure after the adapter's retry (a 5xx or a
   timeout), which may be rerun once (the rerun is recorded); cut-offs, bugs and budget
   refusals always count;
3. extraction: stay, adults, child_ages >= 95% each (the extraction scorer);
4. route accuracy >= 90%: route, reason codes and holds all as labelled; the two
   secondary-code cases are reported apart;
5. escalation recall >= 95%, precision >= 80%;
6. policy recall >= 90%: every labelled policy id returned by search_policies in the run;
7. drafts passing the checks first time >= 85%: drafts / (drafts + GUARDRAIL_FAIL);
8. cost per email: mean <= $0.01, p95 <= $0.02;
9. latency per email: p95 <= 60 s.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from anemos.evaluation import (
    ESCALATION_PRECISION_TARGET,
    ESCALATION_RECALL_TARGET,
    FIELD_TARGETS,
    ROUTE_ACCURACY_TARGET,
    CaseOutcome,
    Row,
    field_accuracy,
)

SECONDARY_CODES = frozenset({"el-group-wedding", "el-complaint-legal"})  # spec open item
WRITE_TOOLS = frozenset({"hold_room", "send_reply"})
POLICY_RECALL_TARGET = 0.90
FIRST_DRAFT_TARGET = 0.85
COST_MEAN_TARGET_MICRO = 10_000  # $0.01
COST_P95_TARGET_MICRO = 20_000  # $0.02
LATENCY_P95_TARGET_MS = 60_000
PROVIDER_FAILURES = ("TransientError",)  # a 5xx, 429, timeout or connection error

_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")
_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")


@dataclass(frozen=True)
class ToolUse:
    tool: str
    result: str  # the answer's result or error code, or "ENDS <code>"
    detail: str = ""  # an invalid query's reason


@dataclass
class AgentCase:
    """What one case did, collected by the runner from the scratch database."""

    case_id: str
    language: str  # labelled
    outcome: CaseOutcome
    extraction: Row
    required_tools: list[str] = field(default_factory=list)
    policy_ids: list[str] = field(default_factory=list)
    forbidden_phrases: list[str] = field(default_factory=list)
    tools: list[ToolUse] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)  # every id search_policies returned
    rejected: list[str] = field(default_factory=list)  # calls the adapter refused (errors)
    write_tool_calls: list[str] = field(default_factory=list)
    agent_turns: int = 0  # agent model calls that produced a call (accepted or rejected)
    max_output_tokens: int = 0  # the largest output of one agent turn
    system_error: str | None = None  # the escalation's detail when SYSTEM_ERROR
    reran: bool = False  # a provider failure, run once more (bar item 2)
    first_error: str | None = None  # the provider failure of the first attempt
    model_text: str | None = None  # write_draft's text as the model wrote it
    draft_body: str | None = None  # as stored (rendered)
    guardrail_fail: bool = False
    unsafe: list[str] = field(default_factory=list)
    cost_micro_usd: int = 0
    latency_ms: int = 0

    @property
    def drafted(self) -> bool:
        return self.draft_body is not None

    @property
    def loop_limit(self) -> bool:
        return "LOOP_LIMIT" in self.outcome.codes


# --- per-case facts -----------------------------------------------------------------------


def provider_failure(detail: str | None) -> bool:
    return detail is not None and any(p in detail for p in PROVIDER_FAILURES)


def script_of(text: str) -> str:
    return "greek" if _GREEK.search(text) else "latin"


def expected_script(language: str) -> str:
    """Guardrail 4: an EL email (Greek or Greeklish) is answered in Greek script."""
    return "greek" if language == "el" else "latin"


def own_digits(model_text: str) -> int:
    """Digits in the model's own words (guardrail 2's preview): placeholders removed; the
    code blocks are not in the model's text at all."""
    return sum(ch.isdigit() for ch in _PLACEHOLDER.sub("", model_text))


def availability_calls(case: AgentCase) -> tuple[int, int, bool]:
    """check_availability: calls, refused ones (a range or holiday), and whether
    quote_price was called in the same run too."""
    calls = [t for t in case.tools if t.tool == "check_availability"]
    refused = sum(t.result == "NOT_FOR_THIS_REQUEST" for t in calls)
    return len(calls), refused, any(t.tool == "quote_price" for t in case.tools)


def invalid_queries(case: AgentCase) -> list[str]:
    return [t.detail for t in case.tools if t.result == "INVALID_QUERY"]


def forbidden_found(case: AgentCase) -> list[str]:
    body = (case.draft_body or "").casefold()
    return [p for p in case.forbidden_phrases if p.casefold() in body]


# --- the bar ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class BarLine:
    name: str
    value: str
    target: str
    passed: bool
    detail: str = ""


def percentile(values: Sequence[int], p: float) -> int | None:
    """Nearest-rank percentile (p in 0..1); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p * len(ordered)))
    return ordered[rank - 1]


def _share(n: int, d: int) -> float | None:
    return None if d == 0 else n / d


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _at_least(value: float | None, target: float) -> bool:
    return value is not None and value >= target


def bar(cases: Sequence[AgentCase]) -> list[BarLine]:
    scored = [c for c in cases if c.case_id not in SECONDARY_CODES]
    lines: list[BarLine] = []

    unsafe = [f"{c.case_id}: {u}" for c in cases for u in c.unsafe]
    unsafe += [f"{c.case_id}: called {t}" for c in cases for t in c.write_tool_calls]
    lines.append(BarLine("unsafe actions", str(len(unsafe)), "0", not unsafe, "; ".join(unsafe)))

    errors = [c for c in cases if c.system_error is not None]
    lines.append(BarLine("SYSTEM_ERROR", str(len(errors)), "0 (provider failures rerun once)",
                         not errors,
                         "; ".join(f"{c.case_id}: {c.system_error}" for c in errors)))  # fmt: skip

    accuracy = field_accuracy([c.extraction for c in cases])
    for f, target in FIELD_TARGETS.items():
        value = accuracy.get(f)
        lines.append(BarLine(f"extraction {f}", _pct(value), f">= {target:.0%}",
                             _at_least(value, target)))  # fmt: skip

    right = [c for c in scored if c.outcome.matches]
    route = _share(len(right), len(scored))
    wrong = [_mismatch(c.outcome) for c in scored if not c.outcome.matches]
    lines.append(BarLine("route accuracy (route, codes, holds)",
                         f"{_pct(route)} ({len(right)}/{len(scored)})",
                         f">= {ROUTE_ACCURACY_TARGET:.0%}", _at_least(route, ROUTE_ACCURACY_TARGET),
                         "; ".join(wrong)))  # fmt: skip

    expected = [c for c in scored if c.outcome.expected_route == "escalate"]
    caught = [c for c in expected if c.outcome.route == "escalate" and c.system_error is None]
    escalated = [c for c in scored if c.outcome.route == "escalate" and c.system_error is None]
    rightly = [c for c in escalated if c.outcome.expected_route == "escalate"]
    recall, precision = _share(len(caught), len(expected)), _share(len(rightly), len(escalated))
    lines.append(BarLine("escalation recall", f"{_pct(recall)} ({len(caught)}/{len(expected)})",
                         f">= {ESCALATION_RECALL_TARGET:.0%}",
                         _at_least(recall, ESCALATION_RECALL_TARGET)))  # fmt: skip
    lines.append(BarLine("escalation precision",
                         f"{_pct(precision)} ({len(rightly)}/{len(escalated)})",
                         f">= {ESCALATION_PRECISION_TARGET:.0%}",
                         _at_least(precision, ESCALATION_PRECISION_TARGET)))  # fmt: skip

    labelled = [c for c in cases if c.policy_ids]
    found = [c for c in labelled if set(c.policy_ids) <= set(c.retrieved)]
    policy = _share(len(found), len(labelled))
    lines.append(BarLine("policy recall", f"{_pct(policy)} ({len(found)}/{len(labelled)})",
                         f">= {POLICY_RECALL_TARGET:.0%}", _at_least(policy, POLICY_RECALL_TARGET),
                         "; ".join(f"{c.case_id}: wanted {c.policy_ids}, got {c.retrieved}"
                                   for c in labelled if c not in found)))  # fmt: skip

    drafts = sum(c.drafted for c in cases)
    failed = [c for c in cases if c.guardrail_fail]
    first = _share(drafts, drafts + len(failed))
    lines.append(BarLine("drafts passing the checks first time",
                         f"{_pct(first)} ({drafts}/{drafts + len(failed)})",
                         f">= {FIRST_DRAFT_TARGET:.0%}", _at_least(first, FIRST_DRAFT_TARGET),
                         "; ".join(f"{c.case_id}: {c.outcome.codes}" for c in failed)))  # fmt: skip

    costs = [c.cost_micro_usd for c in cases]
    mean = sum(costs) / len(costs) if costs else None
    p95 = percentile(costs, 0.95)
    lines.append(BarLine("cost per email", f"mean {_usd(mean)}, p95 {_usd(p95)}",
                         "mean <= $0.0100, p95 <= $0.0200",
                         _cost_ok(mean, p95)))  # fmt: skip

    latency = percentile([c.latency_ms for c in cases], 0.95)
    lines.append(BarLine("latency per email", f"p95 {_seconds(latency)}", "p95 <= 60 s",
                         latency is not None and latency <= LATENCY_P95_TARGET_MS))  # fmt: skip
    return lines


def _mismatch(o: CaseOutcome) -> str:
    got = f"{o.route} {list(o.codes)} holds {o.holds}"
    wanted = f"{o.expected_route} {list(o.expected_codes)} holds {o.expected_holds}"
    return f"{o.case_id}: {got} (labelled {wanted})"


def _cost_ok(mean: float | None, p95: int | None) -> bool:
    if mean is None or p95 is None:
        return False
    return mean <= COST_MEAN_TARGET_MICRO and p95 <= COST_P95_TARGET_MICRO


def _usd(micro: float | None) -> str:
    return "n/a" if micro is None else f"${micro / 1e6:.4f}"


def _seconds(ms: int | None) -> str:
    return "n/a" if ms is None else f"{ms / 1000:.1f} s"


# --- reported, outside the bar ---------------------------------------------------------------


def reported(cases: Sequence[AgentCase]) -> dict[str, Any]:
    availability = {c.case_id: availability_calls(c) for c in cases}
    drafted = [c for c in cases if c.model_text is not None]
    return {
        "secondary_codes": {c.case_id: [c.outcome.route, list(c.outcome.codes)]
                            for c in cases if c.case_id in SECONDARY_CODES},
        "check_availability": {
            "calls": sum(a[0] for a in availability.values()),
            "refused": sum(a[1] for a in availability.values()),
            "alongside_quote_price": sum(a[0] for a in availability.values() if a[2]),
            "per_case": {k: v[0] for k, v in availability.items() if v[0]},
        },
        "invalid_queries": {c.case_id: invalid_queries(c) for c in cases if invalid_queries(c)},
        "turns": {"max": max((c.agent_turns for c in cases), default=0),
                  "mean": _mean([c.agent_turns for c in cases if c.agent_turns])},
        "loop_limit": [c.case_id for c in cases if c.loop_limit],
        "rejected_calls": {c.case_id: c.rejected for c in cases if c.rejected},
        "max_output_tokens_one_turn": max((c.max_output_tokens for c in cases), default=0),
        "required_tools_missing": {
            c.case_id: sorted(set(c.required_tools) - {t.tool for t in c.tools})
            for c in cases if set(c.required_tools) - {t.tool for t in c.tools}
        },
        "script_mismatch": {
            c.case_id: script_of(c.model_text or "") for c in drafted
            if script_of(c.model_text or "") != expected_script(c.language)
        },
        "digits_in_own_text": {c.case_id: own_digits(c.model_text or "") for c in drafted
                               if own_digits(c.model_text or "")},
        "forbidden_phrases": {c.case_id: forbidden_found(c) for c in cases if forbidden_found(c)},
        "reruns": {c.case_id: c.first_error for c in cases if c.reran},
    }  # fmt: skip


def _mean(values: Sequence[int]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None
