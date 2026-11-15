"""Evaluation support: case sets, per-field scoring against the gold labels, running
cases through an `LLM`, and the results file. No model is called here: the runners
under eval/ pass in a real `LLM`, the tests a scripted one.

Scoring compares what the model returned with the case's gold extraction, field by
field. Dates are scored twice: `date_spec` (what the model wrote down) and `stay`
(both specs resolved by code at the email's received_at: what matters for the reply).
`rooms` is compared after the D5 default (not stated = one room, `rooms_or_default`):
null and 1 give the same draft, so they count as the same answer, in the score and in
the repeat check. (Changed after the first v2 smoke run, 2026-11-16; see D2.)

A results file records the model, the call settings, the prompt version, the commit
(and whether the working tree was dirty: then the run is not comparable), the time,
every call (tokens, reasoning tokens, cost, latency, per-field results, output) and a
summary.
"""

import json
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from anemos.email_cases import EmailCase, load_cases
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import build_prompt
from anemos.llm.adapter import LLM, ClientError, LLMError, max_input_tokens
from anemos.llm.budget import BudgetExceeded
from anemos.llm.pricing import max_cost
from anemos.prompts import PromptSpec
from anemos.reservations.datespec import resolve
from anemos.reservations.model import rooms_or_default

FIELDS = ("kind", "language", "script", "flags", "date_spec", "stay", "adults", "children",
          "child_ages", "rooms", "room_type", "arrival_time")  # fmt: skip


class SetError(ValueError):
    """A case set that cannot be run (unknown id, held-out case, thread reply)."""


def load_set(name: str, eval_dir: Path, *, allow_held_out: bool = False) -> list[EmailCase]:
    """The cases listed in eval/sets/<name>.txt (one id per line, # for comments)."""
    ids = [
        line.split("#", 1)[0].strip()
        for line in (eval_dir / "sets" / f"{name}.txt").read_text(encoding="utf-8").splitlines()
    ]
    ids = [i for i in ids if i]
    cases = {c.id: c for c in load_cases(eval_dir / "emails")}
    unknown = [i for i in ids if i not in cases]
    if unknown:
        raise SetError(f"set {name}: no such case {unknown}")
    chosen = [cases[i] for i in ids]
    held_out = [c.id for c in chosen if c.split == "held_out"]
    if held_out and not allow_held_out:
        raise SetError(f"set {name}: held-out cases are for milestone runs only: {held_out}")
    replies = [c.id for c in chosen if c.labels.kind is None]
    if replies:
        raise SetError(f"set {name}: thread replies are never extracted: {replies}")
    return chosen


def score(gold: ExtractionOutput, got: ExtractionOutput, received_at: datetime) -> dict[str, bool]:
    def resolved(out: ExtractionOutput) -> object:
        spec = out.date_spec_value()
        return None if spec is None else resolve(spec, received_at)

    return {
        "kind": got.kind == gold.kind,
        "language": got.language == gold.language,
        "script": got.script == gold.script,
        "flags": set(got.flags) == set(gold.flags),
        "date_spec": got.date_spec == gold.date_spec,
        "stay": resolved(got) == resolved(gold),
        "adults": got.adults == gold.adults,
        "children": got.children == gold.children,
        "child_ages": sorted(got.child_ages) == sorted(gold.child_ages),
        "rooms": rooms_or_default(got.rooms) == rooms_or_default(gold.rooms),
        "room_type": got.room_type == gold.room_type,
        "arrival_time": got.arrival_time == gold.arrival_time,
    }


@dataclass(frozen=True)
class Row:
    case_id: str
    model: str
    response_model: str | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_micro_usd: int | None = None
    latency_ms: int | None = None
    attempts: int | None = None
    fields: dict[str, bool] | None = None  # None: the call failed
    output: dict[str, Any] | None = None
    error: str | None = None
    stops_run: bool = False  # the API refused the request: the run stopped here


def run_cases(
    llm: LLM, cases: Sequence[EmailCase], spec: PromptSpec, *, step: str, repeat: int = 1
) -> list[Row]:
    """Each case `repeat` times. A failed call is a row with its error. Two failures end
    the run (the rows so far are kept): an exhausted budget, and a request the API
    refused (4xx: a bad schema or setting), which every later call would repeat."""
    settings = replace(llm.settings, max_output_tokens=spec.max_output_tokens,
                       reasoning_effort=spec.reasoning_effort)  # fmt: skip
    rows: list[Row] = []
    for case in cases:
        gold = case.gold_output()
        assert gold is not None, case.id
        for _ in range(repeat):
            prompt = build_prompt(case.email.subject, case.email.body.strip(), spec)
            try:
                result = llm.structured(prompt, ExtractionOutput, step=step,
                                        prompt_version=spec.version, settings=settings)  # fmt: skip
            except BudgetExceeded as exc:
                rows.append(Row(case.id, settings.model, error=f"BudgetExceeded: {exc}"))
                return rows
            except LLMError as exc:
                refused = isinstance(exc.__cause__, ClientError) and exc.__cause__.request_refused
                rows.append(Row(case.id, settings.model, error=str(exc), stops_run=refused))
                if refused:  # the same request would be refused again: stop, don't repeat it
                    return rows
                continue
            u = result.usage
            rows.append(Row(
                case.id, settings.model, response_model=result.response_model,
                input_tokens=u.input_tokens,
                cached_input_tokens=u.cached_input_tokens, output_tokens=u.output_tokens,
                reasoning_tokens=u.reasoning_tokens, cost_micro_usd=result.cost_micro_usd,
                latency_ms=result.latency_ms, attempts=result.attempts,
                fields=score(gold, result.output, case.email.received_at),
                output=result.output.model_dump(mode="json"),
            ))  # fmt: skip
    return rows


@dataclass(frozen=True)
class Summary:
    calls: int
    failed: int
    cost_micro_usd: int
    mean_latency_ms: int | None
    accuracy: dict[str, float] = field(default_factory=dict)  # field -> share right
    # case -> what differed between its repeats ([] = every repeat succeeded and
    # returned the same parsed output); in run order
    consistency: dict[str, list[str]] = field(default_factory=dict)

    @property
    def consistent_cases(self) -> int:
        return sum(not differed for differed in self.consistency.values())


# Fields compared after the default the code applies, as in `score`.
_AS_USED: dict[str, Any] = {"rooms": rooms_or_default}


def _as_used(key: str, value: Any) -> Any:
    convert = _AS_USED.get(key)
    return value if convert is None else convert(value)


def consistency(rows: Sequence[Row]) -> dict[str, list[str]]:
    """Per case: the output fields whose values differed between its repeats, plus
    "failed call" if a repeat failed. A model that answers the same email differently
    from one call to the next is a risk even when one of its answers is right. Values
    are compared as the code uses them (`rooms` after the D5 default)."""
    by_case: dict[str, list[Row]] = {}
    for row in rows:
        by_case.setdefault(row.case_id, []).append(row)
    result: dict[str, list[str]] = {}
    for case_id, repeats in by_case.items():
        outputs = [r.output for r in repeats if r.output is not None]
        keys = sorted({k for out in outputs for k in out})
        differed = [k for k in keys if len({repr(_as_used(k, out.get(k))) for out in outputs}) > 1]
        if len(outputs) < len(repeats):
            differed.append("failed call")
        result[case_id] = differed
    return result


def summarise(rows: Sequence[Row]) -> Summary:
    scored = [r for r in rows if r.fields is not None]
    latencies = [r.latency_ms for r in scored if r.latency_ms is not None]
    return Summary(
        calls=len(rows),
        failed=len(rows) - len(scored),
        cost_micro_usd=sum(r.cost_micro_usd or 0 for r in rows),
        mean_latency_ms=round(sum(latencies) / len(latencies)) if latencies else None,
        accuracy={
            f: sum(bool(r.fields and r.fields[f]) for r in scored) / len(scored) for f in FIELDS
        }
        if scored
        else {},
        consistency=consistency(rows),
    )


def report(rows: Sequence[Row], summary: Summary) -> str:
    """A plain-text table: one line per call, the fields it got wrong, then a summary."""
    lines = [f"{'case':32} {'in':>6} {'out':>5} {'reas':>5} {'µ$':>6} {'ms':>6}  wrong fields"]
    for r in rows:
        if r.fields is None:
            lines.append(f"{r.case_id:32} FAILED: {r.error}")
            continue
        wrong = ", ".join(f for f in FIELDS if not r.fields[f]) or "-"
        numbers = (
            f"{r.input_tokens:>6} {r.output_tokens:>5} {r.reasoning_tokens:>5} "
            f"{r.cost_micro_usd:>6} {r.latency_ms:>6}"
        )
        lines.append(f"{r.case_id:32} {numbers}  {wrong}")
    if rows and rows[-1].stops_run:
        lines.append("STOPPED: the API refused the request; fix the request or setting, rerun")
    lines.append("")
    lines.append(f"calls {summary.calls}, failed {summary.failed}, cost "
                 f"{summary.cost_micro_usd} µ$ (${summary.cost_micro_usd / 1_000_000:.4f}), "
                 f"mean latency {summary.mean_latency_ms} ms")  # fmt: skip
    if summary.accuracy:
        lines.append("accuracy: " + ", ".join(f"{f} {a:.0%}" for f, a in summary.accuracy.items()))
    lines.append("repeats:")
    for case_id, differed in summary.consistency.items():
        verdict = f"differs in {', '.join(differed)}" if differed else "same"
        lines.append(f"  {case_id:32} {verdict}")
    lines.append(f"consistent cases: {summary.consistent_cases}/{len(summary.consistency)}")
    return "\n".join(lines)


def git_state(repo: Path) -> tuple[str, bool]:
    """(commit, dirty). eval/results/ is left out: writing a result must not make the
    next run look dirty."""
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                            text=True, check=True).stdout.strip()  # fmt: skip
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", ".", ":(exclude)eval/results"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return commit, bool(status.strip())


@dataclass(frozen=True)
class RunInfo:
    set_name: str
    model: str
    settings: dict[str, Any]
    prompt_version: str
    commit: str
    dirty: bool  # True: run with --allow-dirty; not comparable with other runs
    started_at: datetime
    repeat: int


def write_results(results_dir: Path, info: RunInfo, rows: Sequence[Row], summary: Summary) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = info.started_at.strftime("%Y%m%dT%H%M%SZ")
    path = results_dir / f"{stamp}-{info.set_name}-{info.model}.json"
    data = {"run": asdict(info) | {"started_at": info.started_at.isoformat()},
            "summary": asdict(summary) | {"consistent_cases": summary.consistent_cases},
            "calls": [asdict(r) for r in rows]}  # fmt: skip
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def rescore(data: dict[str, Any], cases: Sequence[EmailCase]) -> tuple[list[Row], Summary]:
    """A results file's calls scored again with today's scorer, from the outputs it
    recorded: no model call. Failed calls stay failed."""
    gold = {case.id: case for case in cases}
    rows = []
    for call in data["calls"]:
        row = Row(**call)
        if row.output is not None:
            case = gold[row.case_id]
            expected = case.gold_output()
            assert expected is not None, case.id
            got = ExtractionOutput.model_validate(row.output)
            row = replace(row, fields=score(expected, got, case.email.received_at))
        rows.append(row)
    return rows, summarise(rows)


# --- the extraction eval: fields and routing over the dev split ---------------------------

# Cases that need model judgement the agent loop (week 5) will bring; the router alone
# cannot reach their labels. Scored for fields, reported apart for routing.
NOT_BUILT_YET = frozenset({
    "en-discount-as-agreed", "el-group-wedding", "el-complaint-legal", "el-cancellation",
    "el-change-dates", "el-pets-policy", "el-greeklish-transfer-policy",
})  # fmt: skip

# SPEC Evaluation plan targets. Dates are scored as `stay` (resolved by code).
FIELD_TARGETS: dict[str, float] = {"stay": 0.95, "adults": 0.95, "child_ages": 0.95}
ROUTE_ACCURACY_TARGET = 0.90
ESCALATION_RECALL_TARGET = 0.95
ESCALATION_PRECISION_TARGET = 0.80


def dev_cases(eval_dir: Path, ids: Sequence[str] = ()) -> list[EmailCase]:
    """The dev split (sorted by id), or the listed dev cases. Held-out cases are refused:
    they are for milestone runs only."""
    cases = {c.id: c for c in load_cases(eval_dir / "emails")}
    unknown = [i for i in ids if i not in cases]
    if unknown:
        raise SetError(f"no such case {unknown}")
    chosen = [cases[i] for i in ids] if ids else [c for c in cases.values() if c.split == "dev"]
    held_out = [c.id for c in chosen if c.split != "dev"]
    if held_out:
        raise SetError(f"not dev cases (held-out cases are for milestone runs): {held_out}")
    return chosen


def estimate_max_cost(cases: Sequence[EmailCase], spec: PromptSpec, model: str) -> int:
    """The most the cases can cost, in micro-dollars: per case the adapter's own
    reservation (every input token uncached, the whole output limit used)."""
    total = 0
    for case in cases:
        prompt = build_prompt(case.email.subject, case.email.body.strip(), spec)
        total += max_cost(model, max_input_tokens=max_input_tokens(prompt, ExtractionOutput),
                          max_output_tokens=spec.max_output_tokens)  # fmt: skip
    return total


def field_accuracy(rows: Sequence[Row]) -> dict[str, float]:
    """Share right per field over *every* case: a failed call is wrong on every field."""
    if not rows:
        return {}
    return {f: sum(bool(r.fields and r.fields[f]) for r in rows) / len(rows) for f in FIELDS}


@dataclass(frozen=True)
class CaseOutcome:
    """How one case ended in the run, against its labels. route: the draft's route, or
    "escalate" / "set_aside" when there is no draft."""

    case_id: str
    expected_route: str
    expected_codes: tuple[str, ...]
    expected_holds: int
    route: str
    codes: tuple[str, ...]
    holds: int
    failed: bool  # the model call failed (the run escalated SYSTEM_ERROR)

    @property
    def matches(self) -> bool:
        return not self.failed and (self.route, sorted(self.codes), self.holds) == (
            self.expected_route, sorted(self.expected_codes), self.expected_holds,
        )  # fmt: skip


@dataclass(frozen=True)
class RoutingScore:
    cases: int  # scored: not-built-yet cases left out
    route_correct: int
    escalations_expected: int
    escalations_caught: int  # labelled escalate, escalated by a run whose call succeeded
    escalated: int  # escalated by a run whose call succeeded
    escalated_rightly: int  # ... and labelled escalate
    codes_exact: int  # among escalations caught: the same reason codes
    not_built_yet: list[str]
    failed: list[str]

    @property
    def route_accuracy(self) -> float | None:
        return self.route_correct / self.cases if self.cases else None

    @property
    def recall(self) -> float | None:
        return (
            self.escalations_caught / self.escalations_expected
            if self.escalations_expected
            else None
        )

    @property
    def precision(self) -> float | None:
        return self.escalated_rightly / self.escalated if self.escalated else None


def score_routing(outcomes: Sequence[CaseOutcome]) -> RoutingScore:
    """Route accuracy and escalation recall/precision per email, not-built-yet cases left
    out. A failed call is never a correct escalation, though the run escalates it
    (SYSTEM_ERROR): it is wrong for route accuracy, a miss for recall when the case is
    labelled escalate, and left out of precision (it is not a routing decision)."""
    scored = [o for o in outcomes if o.case_id not in NOT_BUILT_YET]
    ok = [o for o in scored if not o.failed]
    expected = [o for o in scored if o.expected_route == "escalate"]
    caught = [o for o in expected if not o.failed and o.route == "escalate"]
    escalated = [o for o in ok if o.route == "escalate"]
    return RoutingScore(
        cases=len(scored),
        route_correct=sum(o.route == o.expected_route for o in ok),
        escalations_expected=len(expected),
        escalations_caught=len(caught),
        escalated=len(escalated),
        escalated_rightly=sum(o.expected_route == "escalate" for o in escalated),
        codes_exact=sum(sorted(o.codes) == sorted(o.expected_codes) for o in caught),
        not_built_yet=sorted(o.case_id for o in outcomes if o.case_id in NOT_BUILT_YET),
        failed=sorted(o.case_id for o in outcomes if o.failed),
    )


def meets(value: float | None, target: float) -> bool:
    return value is not None and value >= target
