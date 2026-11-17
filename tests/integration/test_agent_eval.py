"""The agent eval end to end without a model: the gold client answers every call, so the
run, the collection from the scratch database, the bar, the rerun rule and the results
file are tested at no cost."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from anemos.clock import FixedClock
from anemos.email_cases import load_cases
from anemos.gold import GoldClient
from anemos.llm.adapter import (
    ClientError,
    IncompleteReply,
    Settings,
    Tool,
    Transcript,
    TransientError,
    TurnReply,
)
from anemos.llm.budget import Budget
from anemos.llm.pricing import Usage
from anemos.prompts import CURRENT_AGENT, CURRENT_SUMMARY

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
EVAL = REPO / "eval"
CASES = {c.id: c for c in load_cases(EVAL / "emails")}
NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
IDS = ["en-quote-sea-view-june", "el-pets-policy", "en-nomatch-pool-gym", "el-cancellation",
       "en-fully-booked-decline"]  # fmt: skip


def eval_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_eval", EVAL / "agent_eval.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "a"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


@dataclass
class FailingTurns(GoldClient):
    """The gold client, with errors raised on its first agent turns."""

    errors: list[ClientError] = field(default_factory=list)

    def send_turn(
        self, transcript: Transcript, tools: Sequence[Tool], settings: Settings
    ) -> TurnReply:
        if self.errors:
            raise self.errors.pop(0)
        return super().send_turn(transcript, tools, settings)


@dataclass
class OneBadDraft(GoldClient):
    """The gold client, whose first write_draft carries a number no policy has (G2):
    refused once (D76), then the gold draft passes."""

    spoiled: bool = False

    def send_turn(
        self, transcript: Transcript, tools: Sequence[Tool], settings: Settings
    ) -> TurnReply:
        reply = super().send_turn(transcript, tools, settings)
        if reply.call.name != "write_draft" or self.spoiled:
            return reply
        self.spoiled = True
        args = json.loads(reply.call.arguments)
        args["text"] += " We have 99 rooms."
        return replace(reply, call=replace(reply.call, arguments=json.dumps(args)))


def run(committed_db: CommittedDb, clean_repo: Path, tmp_path: Path, client: Any,
        ids: Sequence[str]) -> tuple[int, dict[str, Any], str]:  # fmt: skip
    args = ["--model", "gpt-6-luna", "--results-dir", str(tmp_path / "results")]
    for case_id in ids:
        args += ["--case", case_id]
    clock = FixedClock(NOW)
    ticks = iter(range(10_000))
    code = eval_module().main(
        args, client=client, clock=clock, repo=clean_repo, conninfo=committed_db.conninfo,
        budget=Budget(clock, 10**6, tmp_path / "spend.jsonl"), timer=lambda: next(ticks) * 2.0,
    )  # fmt: skip
    found = list((tmp_path / "results").glob("*-agent-dev-gpt-6-luna.json"))
    data: dict[str, Any] = json.loads(found[0].read_text(encoding="utf-8")) if found else {}
    return code, data, str(found[0]) if found else ""


def test_the_gold_agent_passes_the_bar(committed_db: CommittedDb, clean_repo: Path,
                                       tmp_path: Path) -> None:  # fmt: skip
    code, data, _ = run(committed_db, clean_repo, tmp_path, GoldClient([CASES[i] for i in IDS]),
                        IDS)  # fmt: skip
    assert code == 0
    assert {line["name"]: line["passed"] for line in data["bar"]} == {
        "unsafe actions": True, "SYSTEM_ERROR": True, "extraction": True,
        "route accuracy (route, codes, holds)": True, "escalation": True,
        "policy recall": True, "drafts passing the checks first time": True,
        "guardrail false alarms": True, "cost per email": True, "latency per email": True,
        "escalation summaries": True,
    }  # fmt: skip
    run_info = data["run"]
    assert run_info["prompt_versions"]["agent"] == CURRENT_AGENT.version  # agent_v2 (week 6)
    assert run_info["prompt_versions"]["summary"] == CURRENT_SUMMARY.version
    assert run_info["settings"]["agent_max_output_tokens"] == 2000
    by_id = {c["case_id"]: c for c in data["cases"]}
    assert by_id["el-pets-policy"]["retrieved"] == ["pets-assistance-dogs"]
    assert by_id["en-quote-sea-view-june"]["model_text"].count("{{QUOTE_BLOCK}}") == 1
    assert by_id["en-fully-booked-decline"]["outcome"]["route"] == "decline"
    assert data["reported"]["check_availability"] == {
        "offered": False,
        "calls": 0,
        "refused": 0,
        "per_case": {},
    }  # agent_v2
    assert data["reported"]["script_mismatch"] == {}
    assert by_id["el-cancellation"]["summary"]  # the gold summary, collected
    assert set(by_id["el-cancellation"]["cost_by_step"]) == {"extract", "agent", "summary"}
    assert by_id["el-pets-policy"]["policy_texts"]["pets-assistance-dogs"]
    assert [len(c["draft_calls"]) for c in data["cases"]] == [1, 1, 0, 0, 1]


def test_a_refused_draft_is_listed_and_marked_by_rescore(
    committed_db: CommittedDb, clean_repo: Path, tmp_path: Path
) -> None:
    """One refusal, then the fixed draft: line 7 and line 8 see it; line 8 waits for the
    owner's mark, and --rescore applies it with no model or database call."""
    case_id = "en-quote-sea-view-june"
    code, data, path = run(committed_db, clean_repo, tmp_path, OneBadDraft([CASES[case_id]]),
                           [case_id])  # fmt: skip
    assert code == 0
    [only] = data["cases"]
    assert [d["checks"] for d in only["draft_calls"]] == [["g2_numbers_from_policies"], []]
    [refused] = data["reported"]["refused_drafts"]
    assert (refused["case"], refused["call"]) == (case_id, 1) and "99 rooms" in refused["text"]
    passed = {line["name"]: line["passed"] for line in data["bar"]}
    assert not passed["drafts passing the checks first time"]  # 0/1
    assert not passed["guardrail false alarms"]  # unmarked

    marks = tmp_path / "marks.json"
    marks.write_text(
        json.dumps({case_id: [{"call": 1, "false_alarm": False, "reason": "99 rooms is invented"}]})
    )
    client = GoldClient([])
    assert (
        eval_module().main(
            ["--rescore", path, "--false-alarms", str(marks)],
            client=client,
            repo=clean_repo,
            clock=FixedClock(NOW),
        )
        == 0
    )
    rescored = json.loads(Path(path).with_name(Path(path).stem + "-rescored.json")
                          .read_text(encoding="utf-8"))  # fmt: skip
    line8 = next(x for x in rescored["bar"] if x["name"] == "guardrail false alarms")
    assert line8["passed"] and line8["value"] == "0% (0/1)"
    assert rescored["rescored"]["marks"] == "marks.json" and client.calls == []


def test_marks_that_do_not_fit_are_refused(clean_repo: Path, tmp_path: Path) -> None:
    results = tmp_path / "r.json"
    results.write_text(json.dumps({"cases": []}))
    marks = tmp_path / "marks.json"
    marks.write_text(json.dumps({"nope": []}))
    assert eval_module().main(["--rescore", str(results), "--false-alarms", str(marks)],
                              repo=clean_repo) == 2  # fmt: skip
    assert not (tmp_path / "r-rescored.json").exists()


def test_a_provider_failure_is_rerun_once_and_recorded(
    committed_db: CommittedDb, clean_repo: Path, tmp_path: Path
) -> None:
    """Two 503s on the first turn: the adapter's retry fails too, the run escalates
    SYSTEM_ERROR, the eval runs the case again, and the rerun passes."""
    busy: list[ClientError] = [TransientError("HTTP 503"), TransientError("HTTP 503")]
    client = FailingTurns([CASES["en-quote-sea-view-june"]], errors=busy)
    code, data, _ = run(committed_db, clean_repo, tmp_path, client, ["en-quote-sea-view-june"])
    assert code == 0
    [only] = data["cases"]
    assert (only["reran"], only["system_error"]) == (True, None)
    assert "TransientError" in only["first_error"]
    assert data["reported"]["reruns"] == {"en-quote-sea-view-june": only["first_error"]}


def test_a_cut_off_reply_counts_and_is_not_rerun(
    committed_db: CommittedDb, clean_repo: Path, tmp_path: Path
) -> None:
    cut: list[ClientError] = [IncompleteReply("max_output_tokens", Usage(10, 0, 2000))]
    client = FailingTurns([CASES["en-quote-sea-view-june"]], errors=cut)
    code, data, _ = run(committed_db, clean_repo, tmp_path, client, ["en-quote-sea-view-june"])
    assert code == 0
    [only] = data["cases"]
    assert only["reran"] is False and "cut off" in only["system_error"]
    assert not {line["name"]: line["passed"] for line in data["bar"]}["SYSTEM_ERROR"]


def test_nothing_is_sent_over_the_spend_limit(
    committed_db: CommittedDb, clean_repo: Path, tmp_path: Path
) -> None:
    client = GoldClient([CASES["en-quote-sea-view-june"]])
    args = ["--model", "gpt-6-luna", "--max-usd", "0.001", "--case", "en-quote-sea-view-june",
            "--results-dir", str(tmp_path / "results")]  # fmt: skip
    code = eval_module().main(args, client=client, repo=clean_repo,
                              conninfo=committed_db.conninfo)  # fmt: skip
    assert code == 3 and client.calls == []
