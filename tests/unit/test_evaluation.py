"""The evaluation support and the smoke runner, with the scripted client (no key)."""

import importlib.util
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from anemos.clock import FixedClock
from anemos.email_cases import EmailCase
from anemos.evaluation import (
    FIELDS,
    SetError,
    git_state,
    load_set,
    report,
    run_cases,
    score,
    summarise,
)
from anemos.extraction import ExtractionOutput
from anemos.llm.adapter import LLM, PermanentError, Prompt, Settings, TransientError
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, ScriptedClient, Step
from anemos.llm.pricing import Usage
from anemos.prompts import EXTRACTION_V1

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
EVAL = REPO / "eval"
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
USAGE = Usage(input_tokens=1_600, cached_input_tokens=0, output_tokens=220, reasoning_tokens=40)


def smoke() -> list[EmailCase]:
    return load_set("smoke", EVAL)


def gold(case: EmailCase) -> ExtractionOutput:
    out = case.gold_output()
    assert out is not None
    return out


def answers_by_email(cases: list[EmailCase]) -> Step:
    """One step that answers whichever case's email is in the prompt with its gold."""

    def step(prompt: Prompt) -> Answer:
        [case] = [c for c in cases if c.email.body.strip() in prompt.input]
        return Answer(gold(case), USAGE, response_model="gpt-6-luna-2026-10-01")

    return step


def llm(steps: list[Step], tmp: Path, cap: int = 1_000_000) -> LLM:
    clock = FixedClock(NOW)
    budget = Budget(clock, cap, tmp / "spend.jsonl")
    return LLM(ScriptedClient.of(steps), Settings("gpt-6-luna", 100), budget, None, clock)


# --- the smoke set ----------------------------------------------------------------------


def test_the_smoke_set_is_five_greek_dev_cases_with_greeklish() -> None:
    cases = smoke()
    assert len(cases) == 5
    assert {c.split for c in cases} == {"dev"}
    assert {c.labels.language for c in cases} == {"el"}
    assert sum(c.labels.script.value == "latin" for c in cases) >= 1  # Greeklish


def copy_eval(tmp: Path, ids: list[str]) -> Path:
    (tmp / "emails").mkdir(parents=True)
    (tmp / "sets").mkdir()
    for case_id in ids:
        shutil.copy(EVAL / "emails" / f"{case_id}.yaml", tmp / "emails")
    (tmp / "sets" / "s.txt").write_text("\n".join(ids) + "\n", encoding="utf-8")
    return tmp


def test_a_set_with_a_held_out_case_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SetError, match="held-out"):
        load_set("s", copy_eval(tmp_path, ["el-medical-insulin"]))


def test_a_set_with_a_thread_reply_is_refused(tmp_path: Path) -> None:
    """(en-thread-reply is also held out; allowed here to reach the reply check.)"""
    with pytest.raises(SetError, match="thread replies"):
        load_set("s", copy_eval(tmp_path, ["en-thread-reply"]), allow_held_out=True)


def test_an_unknown_id_in_a_set_is_refused(tmp_path: Path) -> None:
    eval_dir = copy_eval(tmp_path, ["el-pets-policy"])
    (eval_dir / "sets" / "s.txt").write_text("el-pets-policy\nno-such-case\n", encoding="utf-8")
    with pytest.raises(SetError, match="no-such-case"):
        load_set("s", eval_dir)


# --- scoring --------------------------------------------------------------------------


def test_the_gold_answer_scores_every_field() -> None:
    for case in smoke():
        assert score(gold(case), gold(case), case.email.received_at) == dict.fromkeys(FIELDS, True)


def test_a_wrong_field_is_scored_wrong_and_only_that_field() -> None:
    case = next(c for c in smoke() if c.id == "el-ask-info-child-ages")
    right = gold(case)
    guessed = right.model_copy(update={"child_ages": [8, 6]})  # invented the missing age
    result = score(right, guessed, case.email.received_at)
    assert [f for f in FIELDS if not result[f]] == ["child_ages"]


def test_child_ages_in_another_order_are_right() -> None:
    case = next(c for c in smoke() if c.id == "el-greeklish-family-ages")
    right = gold(case)
    assert len(right.child_ages) == 2
    swapped = right.model_copy(update={"child_ages": list(reversed(right.child_ages))})
    assert score(right, swapped, case.email.received_at)["child_ages"] is True


def test_dates_written_differently_but_meaning_the_same_stay() -> None:
    """date_spec scores what the model wrote; stay scores what it means."""
    case = next(c for c in smoke() if c.id == "el-greeklish-quote-june")
    right = gold(case)
    assert right.date_spec is not None and right.date_spec.kind == "exact"
    nights = right.date_spec.model_copy(update={"check_out": None, "nights": 4})
    result = score(right, right.model_copy(update={"date_spec": nights}), case.email.received_at)
    assert (result["date_spec"], result["stay"]) == (False, True)


# --- running the cases ----------------------------------------------------------------


def test_each_call_records_tokens_cost_latency_and_fields(tmp_path: Path) -> None:
    cases = smoke()
    rows = run_cases(llm([answers_by_email(cases)] * 10, tmp_path), cases, EXTRACTION_V1,
                     step="eval-smoke", repeat=2)  # fmt: skip
    assert [r.case_id for r in rows] == [c.id for c in cases for _ in range(2)]
    first = rows[0]
    assert (first.input_tokens, first.output_tokens, first.reasoning_tokens) == (1_600, 220, 40)
    assert first.response_model == "gpt-6-luna-2026-10-01"
    assert first.cost_micro_usd is not None and first.cost_micro_usd > 0
    assert first.fields == dict.fromkeys(FIELDS, True)
    summary = summarise(rows)
    assert (summary.calls, summary.failed) == (10, 0)
    assert summary.accuracy == dict.fromkeys(FIELDS, 1.0)


def test_the_calls_use_the_prompts_settings(tmp_path: Path) -> None:
    cases = smoke()[:1]
    client = ScriptedClient.of([answers_by_email(cases)])
    clock = FixedClock(NOW)
    run_cases(LLM(client, Settings("gpt-6-luna", 100), Budget(clock, 10**6, tmp_path / "s.jsonl"),
                  None, clock), cases, EXTRACTION_V1, step="eval-smoke")  # fmt: skip
    [sent] = client.sent
    assert (sent.settings.max_output_tokens, sent.settings.reasoning_effort) == (600, "none")


def test_a_failed_call_is_a_row_and_the_run_goes_on(tmp_path: Path) -> None:
    cases = smoke()[:2]
    steps: list[Step] = [TransientError("busy"), TransientError("busy"), answers_by_email(cases)]
    rows = run_cases(llm(steps, tmp_path), cases, EXTRACTION_V1, step="eval-smoke")
    assert rows[0].fields is None and "TransientError" in (rows[0].error or "")
    assert rows[1].fields == dict.fromkeys(FIELDS, True)
    assert summarise(rows).failed == 1


def test_an_exhausted_budget_ends_the_run(tmp_path: Path) -> None:
    rows = run_cases(llm([], tmp_path, cap=0), smoke(), EXTRACTION_V1, step="eval-smoke")
    assert len(rows) == 1 and (rows[0].error or "").startswith("BudgetExceeded")


# --- the working tree ---------------------------------------------------------------


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                   cwd=repo, check=True, capture_output=True)  # fmt: skip


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    return repo


def test_git_state_sees_changes_but_not_results(clean_repo: Path) -> None:
    assert git_state(clean_repo)[1] is False
    (clean_repo / "eval" / "results").mkdir(parents=True)
    (clean_repo / "eval" / "results" / "r.json").write_text("{}", encoding="utf-8")
    assert git_state(clean_repo)[1] is False  # a result file does not dirty the tree
    (clean_repo / "a.txt").write_text("changed\n", encoding="utf-8")
    assert git_state(clean_repo)[1] is True


# --- the smoke command ----------------------------------------------------------------


def smoke_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_cli", EVAL / "smoke.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_smoke_command_prints_and_records_every_call(
    clean_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = smoke()
    clock = FixedClock(NOW)
    code = smoke_module().main(
        ["--model", "gpt-6-luna", "--repeat", "3", "--results-dir", str(tmp_path / "results")],
        client=ScriptedClient.of([answers_by_email(cases)] * 15), clock=clock,
        budget=Budget(clock, 10**6, tmp_path / "spend.jsonl"), repo=clean_repo,
    )  # fmt: skip
    assert code == 0
    printed = capsys.readouterr().out
    assert "el-greeklish-quote-june" in printed and "accuracy: kind 100%" in printed
    [path] = (tmp_path / "results").glob("*.json")
    assert path.name == "20261116T090000Z-smoke-gpt-6-luna.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    run = data["run"]
    assert (run["model"], run["prompt_version"], run["dirty"], run["repeat"]) == (
        "gpt-6-luna", EXTRACTION_V1.version, False, 3,
    )  # fmt: skip
    assert run["settings"]["max_output_tokens"] == 600 and run["commit"]
    assert run["started_at"] == NOW.isoformat()
    assert len(data["calls"]) == 15
    call = data["calls"][0]
    assert {k for k, v in call.items() if v is None} == {"error"}
    assert data["summary"]["accuracy"]["child_ages"] == 1.0
    assert data["summary"]["consistent_cases"] == 5
    assert "consistent cases: 5/5" in printed


def test_the_smoke_command_refuses_a_dirty_tree_unless_allowed(
    clean_repo: Path, tmp_path: Path
) -> None:
    (clean_repo / "a.txt").write_text("edited\n", encoding="utf-8")
    module = smoke_module()
    clock = FixedClock(NOW)
    common: dict[str, Any] = {"clock": clock, "repo": clean_repo,
                              "budget": Budget(clock, 10**6, tmp_path / "spend.jsonl")}  # fmt: skip
    args = ["--model", "gpt-6-luna", "--results-dir", str(tmp_path / "results")]
    assert module.main(args, client=ScriptedClient.of([]), **common) == 2
    assert not (tmp_path / "results").exists()
    client = ScriptedClient.of([answers_by_email(smoke())] * 5)
    assert module.main([*args, "--allow-dirty"], client=client, **common) == 0
    [path] = (tmp_path / "results").glob("*.json")
    assert json.loads(path.read_text(encoding="utf-8"))["run"]["dirty"] is True


# --- repeats: does the model answer the same email the same way? ----------------------


def test_repeats_that_differ_or_fail_are_reported_per_case(tmp_path: Path) -> None:
    """Three repeats per case: one case answers a different child_ages on its second
    repeat, another has a failed call; the other three are the same each time."""
    cases = smoke()
    steps: list[Step] = []
    for case in cases:
        for repeat in range(3):
            right = gold(case)
            if case.id == "el-ask-info-child-ages" and repeat == 1:
                steps.append(Answer(right.model_copy(update={"child_ages": [8, 6]}), USAGE))
            elif case.id == "el-injection-greek" and repeat == 2:
                steps += [TransientError("busy"), TransientError("busy")]  # call + retry
            else:
                steps.append(Answer(right, USAGE))
    rows = run_cases(llm(steps, tmp_path), cases, EXTRACTION_V1, step="eval-smoke", repeat=3)
    summary = summarise(rows)

    assert summary.consistency == {
        "el-greeklish-quote-june": [], "el-greeklish-family-ages": [],
        "el-ask-info-child-ages": ["child_ages"], "el-approximate-late-june": [],
        "el-injection-greek": ["failed call"],
    }  # fmt: skip
    assert summary.consistent_cases == 3
    printed = report(rows, summary)
    assert "el-ask-info-child-ages           differs in child_ages" in printed
    assert "el-injection-greek               differs in failed call" in printed
    assert "el-greeklish-quote-june          same" in printed
    assert printed.endswith("consistent cases: 3/5")


# --- fail fast: a refused request is not repeated --------------------------------------


def test_a_refused_request_stops_the_run_at_once(tmp_path: Path) -> None:
    """A 400 for the schema or a setting would be the same for every call: one row,
    the error printed, no further calls."""
    refused = PermanentError("HTTP 400: 'minimal' is not supported", request_refused=True)
    client = ScriptedClient.of([refused])
    clock = FixedClock(NOW)
    agent = LLM(client, Settings("gpt-6-luna", 100), Budget(clock, 10**6, tmp_path / "s.jsonl"),
                None, clock)  # fmt: skip
    rows = run_cases(agent, smoke(), EXTRACTION_V1, step="eval-smoke", repeat=3)
    assert len(rows) == 1 and len(client.sent) == 1
    assert rows[0].stops_run and "not supported" in (rows[0].error or "")
    printed = report(rows, summarise(rows))
    assert "STOPPED: the API refused the request" in printed
    assert "not supported" in printed


def test_a_bad_reply_does_not_stop_the_run(tmp_path: Path) -> None:
    """An unparsable or cut-off reply is about the model's output, not the request:
    the run goes on to the other cases."""
    cases = smoke()[:2]
    steps: list[Step] = [PermanentError("unparsable reply: ...", USAGE), answers_by_email(cases)]
    rows = run_cases(llm(steps, tmp_path), cases, EXTRACTION_V1, step="eval-smoke")
    assert [r.stops_run for r in rows] == [False, False]
    assert rows[1].fields == dict.fromkeys(FIELDS, True)
