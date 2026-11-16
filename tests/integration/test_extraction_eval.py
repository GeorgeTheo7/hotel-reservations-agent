"""The extraction eval end to end with a scripted client (no network, no cost): each case
on its own in the database, the real run, field and routing scores, the report and the
results file. One case gets a wrong answer, one loses an escalation, one call fails."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from anemos.clock import FixedClock
from anemos.email_cases import load_cases
from anemos.extraction import ExtractionOutput
from anemos.llm.adapter import PermanentError
from anemos.prompts import CURRENT_EXTRACTION
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, ScriptedClient, Step

if TYPE_CHECKING:  # tests/ is not a package: the fixtures' classes are for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
EVAL = REPO / "eval"
CASES = {c.id: c for c in load_cases(EVAL / "emails")}
NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)


def eval_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("extraction_eval", EVAL / "extraction_eval.py")
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
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "a"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def gold(case_id: str) -> ExtractionOutput:
    out = CASES[case_id].gold_output()
    assert out is not None
    return out


def test_the_eval_scores_fields_and_routing_case_by_case(
    committed_db: CommittedDb, clean_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = [
        "en-fully-booked-decline",  # its own world: every Standard room taken -> decline
        "en-late-arrival",  # the answer drops the arrival time -> quote: an escalation missed
        "el-change-dates",  # not built yet: reported apart for routing
        "en-quote-sea-view-june",  # wrong adults: a field miss, the route still right
        "en-invoice-vat",  # the call fails: escalated SYSTEM_ERROR, not a correct escalation
    ]
    steps: list[Step] = [
        Answer(gold("en-fully-booked-decline")),
        Answer(gold("en-late-arrival").model_copy(update={"arrival_time": None})),
        Answer(gold("el-change-dates")),
        Answer(gold("en-quote-sea-view-june").model_copy(update={"adults": 3})),
        PermanentError("the model refused"),
    ]
    clock = FixedClock(NOW)
    args = ["--model", "gpt-6-luna", "--results-dir", str(tmp_path / "results")]
    for case_id in ids:
        args += ["--case", case_id]
    code = eval_module().main(
        args, client=ScriptedClient.of(steps), clock=clock,
        budget=Budget(clock, 10**6, tmp_path / "spend.jsonl"), repo=clean_repo,
        conninfo=committed_db.conninfo,
    )  # fmt: skip
    assert code == 0
    printed = capsys.readouterr().out
    assert "field adults: 2 -> 3" in printed
    assert "en-late-arrival" in printed and "escalate ['LATE_ARRIVAL'] holds 0 -> quote" in printed
    assert "FAILED CALL" in printed and "en-invoice-vat: " in printed

    [path] = (tmp_path / "results").glob("*-extraction-dev-gpt-6-luna.json")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    run, summary = data["run"], data["summary"]
    version = run["prompt_version"]
    assert (version.startswith(f"{CURRENT_EXTRACTION.name}@"), run["dirty"]) == (True, False)
    # Effective: the request fields count only for the 3 requests (el-change-dates and
    # en-invoice-vat are kind other); raw: all 5, the failed call wrong on every field.
    assert summary["field_accuracy"]["adults"] == 2 / 3
    assert summary["field_accuracy_raw"]["adults"] == 3 / 5
    assert summary["field_accuracy"]["arrival_time"] == 2 / 3
    assert summary["field_accuracy_raw"]["arrival_time"] == 3 / 5
    assert summary["field_accuracy"]["kind"] == 4 / 5  # kind applies to every case
    routing = summary["routing"]
    assert routing["cases"] == 4 and routing["not_built_yet"] == ["el-change-dates"]
    assert routing["route_correct"] == 2  # decline, quote; late arrival and the failure wrong
    assert (routing["escalations_caught"], routing["escalations_expected"]) == (0, 2)
    assert (routing["escalated"], routing["precision"]) == (0, None)
    assert routing["failed"] == ["en-invoice-vat"]
    by_id = {c["case_id"]: c for c in data["cases"]}
    assert by_id["en-fully-booked-decline"]["outcome"]["route"] == "decline"
    assert by_id["en-quote-sea-view-june"]["outcome"]["matches"] is True
    assert by_id["en-invoice-vat"]["error"] and by_id["en-invoice-vat"]["fields"] is None

    # Re-scoring the file reproduces the scores without a call; the original stays.
    before = path.read_text(encoding="utf-8")
    assert eval_module().main(["--rescore", str(path)], repo=clean_repo) == 0
    rescored = json.loads(path.with_name(path.stem + ".rescored.json").read_text("utf-8"))
    assert rescored["summary"]["field_accuracy"] == summary["field_accuracy"]
    assert rescored["summary"]["routing"] == summary["routing"]
    assert path.read_text(encoding="utf-8") == before


def test_the_result_is_written_before_the_report_is_printed(
    committed_db: CommittedDb, clean_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The calls are paid for before the report: a report that cannot be printed (a
    console code page without a character) must not lose the result file."""
    module = eval_module()

    def unprintable(*args: object) -> str:
        raise UnicodeEncodeError("cp1253", "x", 0, 1, "character maps to <undefined>")

    monkeypatch.setattr(module, "report", unprintable)
    clock = FixedClock(NOW)
    with pytest.raises(UnicodeEncodeError):
        module.main(
            ["--model", "gpt-6-luna", "--results-dir", str(tmp_path / "results"),
             "--case", "en-quote-sea-view-june"],
            client=ScriptedClient.of([Answer(gold("en-quote-sea-view-june"))]), clock=clock,
            budget=Budget(clock, 10**6, tmp_path / "spend.jsonl"), repo=clean_repo,
            conninfo=committed_db.conninfo,
        )  # fmt: skip
    assert len(list((tmp_path / "results").glob("*-extraction-dev-gpt-6-luna.json"))) == 1
