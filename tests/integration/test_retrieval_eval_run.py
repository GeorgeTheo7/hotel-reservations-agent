"""eval/retrieval_eval.py end to end with fakes: the hashing embedder, a scripted
baseline, the committed test database (seeded, so the packaged chunks are loaded).
No network: what is checked is the run's plumbing and its record, not retrieval quality
(the fake's vectors are not comparable with the packaged ones)."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from anemos.clock import FixedClock
from anemos.llm.adapter import Prompt, Settings
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, HashingEmbedClient, ScriptedClient, Step
from anemos.policies import load_policies
from anemos.policies.embeddings import EMBEDDING_DIMENSIONS, policies_sha256
from anemos.policies.search import selector_prompt
from anemos.prompts import POLICY_SELECT_V1

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
EVAL = REPO / "eval"
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("retrieval_eval_cli", EVAL / "retrieval_eval.py")
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def breakfast_or_nothing(prompt: Prompt) -> Answer:
    question = prompt.input.rsplit("<question>", 1)[1]
    return Answer({"policy_ids": ["breakfast"] if "breakfast" in question.lower() else []})


def run(module: ModuleType, committed_db: CommittedDb, tmp_path: Path, *argv: str,
        dirty: bool = False, step: Step = breakfast_or_nothing,
        ) -> tuple[int, ScriptedClient, HashingEmbedClient]:  # fmt: skip
    client = ScriptedClient.of([step] * 500)
    embedder = HashingEmbedClient(EMBEDDING_DIMENSIONS)
    clock = FixedClock(NOW)
    code = module.main(
        [*argv, "--results-dir", str(tmp_path)], client=client, embed_client=embedder,
        clock=clock, budget=Budget(clock, 1_000_000, tmp_path / "ledger.jsonl"),
        conninfo=committed_db.conninfo, git=lambda repo: ("c" * 40, dirty),
    )  # fmt: skip
    return code, client, embedder


def test_a_dev_run_records_everything_the_result_needs(
    module: ModuleType, committed_db: CommittedDb, tmp_path: Path
) -> None:
    code, client, embedder = run(module, committed_db, tmp_path, "--split", "dev")
    assert code == 0
    data = json.loads((tmp_path / "20261116T090000Z-retrieval-dev-gpt-6-luna.json").read_text(
        encoding="utf-8"))  # fmt: skip
    dev = [q for q in data["queries"]]
    assert len(dev) == 48 and len(client.sent) == 48  # --repeat defaults to 1
    assert len(embedder.calls) == 2 and embedder.calls[0] == embedder.calls[1]  # twice

    run_ = data["run"]
    assert (run_["split"], run_["commit"], run_["dirty"]) == ("dev", "c" * 40, False)
    assert run_["started_at"] == NOW.isoformat()
    assert run_["policies_sha256"] == policies_sha256()
    assert run_["embedding_model"] == "text-embedding-3-small"
    assert (run_["selector_model"], run_["selector_prompt_version"]) == (
        "gpt-6-luna", POLICY_SELECT_V1.version)  # fmt: skip
    assert run_["selector_settings"] == {"max_output_tokens": 100, "reasoning_effort": "none"}
    assert run_["selector_response_models"] == ["gpt-6-luna"] and run_["repeat"] == 1
    assert run_["threshold_choice"]["threshold"] == run_["search_settings"]["threshold"]
    assert len(run_["queries_sha256"]) == 64 and run_["embeddings_generated"]["commit"]
    assert run_["cost_micro_usd"] > 0

    summary = data["summary"]
    assert set(summary["modes"]) == {"vector", "full_text", "hybrid", "selector"}
    assert len(summary["modes"]["selector"]) == 1  # one score per repeat
    assert {"written_by=mentor", "written_by=claude", "source=email"} <= set(summary["groups"])
    assert summary["largest_best_similarity_change"] == 0.0  # the fake is deterministic
    assert module.third_decimal(0.0004) == "the threshold's third decimal is stable"
    assert module.third_decimal(0.0005) == "the threshold's third decimal is noise"
    assert summary["hybrid_vs_vector"]["verdict"] in {"better", "worse", "same"}
    row = next(r for r in data["queries"] if r["id"] == "q-e-breakfast")
    assert row["selector"] == [{"top": ["breakfast"], "no_match": False, "right": True,
                                "returned": ["breakfast"]}]  # fmt: skip


def test_the_selector_is_the_production_call(
    module: ModuleType, committed_db: CommittedDb, tmp_path: Path
) -> None:
    _, client, _ = run(module, committed_db, tmp_path, "--split", "dev")
    sent = client.sent[0]
    # the first dev query in the file, sent exactly as search_policies sends it
    assert sent.prompt == selector_prompt(
        "Can the remaining balance be paid in cash when we arrive?"
    )
    assert sent.settings == Settings("gpt-6-luna", 100, "none")
    assert all(f"[{c.policy_id}] {c.title}" in sent.prompt.input for c in load_policies())


def test_each_repeat_is_a_new_call_scored_on_its_own(
    module: ModuleType, committed_db: CommittedDb, tmp_path: Path
) -> None:
    """The fake answers breakfast for q-e-breakfast on calls 1 and 3 of that query, and
    nothing on call 2: repeat 2 misses it, and the query is inconsistent."""
    calls: dict[str, int] = {}

    def flaky(prompt: Prompt) -> Answer:
        question = prompt.input.rsplit("<question>", 1)[1]
        calls[question] = calls.get(question, 0) + 1
        answer = breakfast_or_nothing(prompt)
        if "until what time" in question and calls[question] == 2:
            return Answer({"policy_ids": []})
        return answer

    code, client, embedder = run(module, committed_db, tmp_path, "--split", "dev",
                                 "--repeat", "3", "--max-usd", "1", step=flaky)  # fmt: skip
    data = json.loads(next(tmp_path.glob("*-retrieval-dev-*.json")).read_text(encoding="utf-8"))
    assert code == 0 and len(client.sent) == 3 * 48 and len(embedder.calls) == 2
    row = next(r for r in data["queries"] if r["id"] == "q-e-breakfast")
    assert [a["right"] for a in row["selector"]] == [True, False, True]
    assert row["selector_consistent"] is False
    summary = data["summary"]
    assert summary["selector_inconsistent"] == ["q-e-breakfast"]
    assert summary["selector_consistency"] == 47 / 48
    hits = [s["hits"] for s in summary["modes"]["selector"]]
    assert hits[0] - hits[1] == 1 and hits[0] == hits[2]
    lines = [b["line"] for b in summary["selector_bar"]]
    assert lines[-1] == "same answer on every repeat" and len(lines) == 7
    assert summary["selector_bar_passed"] is False  # the fake finds one chunk at most


def test_a_dirty_tree_is_refused_before_any_call(
    module: ModuleType, committed_db: CommittedDb, tmp_path: Path
) -> None:
    code, client, embedder = run(module, committed_db, tmp_path, "--split", "dev", dirty=True)
    assert code == 2 and client.sent == [] and embedder.calls == []
    code, _, _ = run(module, committed_db, tmp_path, "--split", "dev", "--allow-dirty", dirty=True)
    data = json.loads(next(tmp_path.glob("*-retrieval-dev-*.json")).read_text(encoding="utf-8"))
    assert code == 0 and data["run"]["dirty"] is True


def test_over_the_cost_limit_nothing_is_sent(
    module: ModuleType, committed_db: CommittedDb, tmp_path: Path
) -> None:
    code, client, embedder = run(module, committed_db, tmp_path, "--split", "dev", "--max-usd", "0")
    assert code == 3 and client.sent == [] and embedder.calls == []


@pytest.mark.parametrize(
    "argv",
    [["--split", "held_out"], ["--split", "dev", "--threshold", "0.3"]],
    ids=["held-out-without", "dev-with"],
)
def test_only_held_out_takes_a_threshold(
    module: ModuleType, committed_db: CommittedDb, tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(SystemExit):
        run(module, committed_db, tmp_path, *argv)


def test_a_held_out_run_uses_the_given_threshold(
    module: ModuleType, committed_db: CommittedDb, tmp_path: Path
) -> None:
    code, client, _ = run(module, committed_db, tmp_path, "--split", "held_out",
                          "--threshold", "0.25")  # fmt: skip
    data = json.loads(next(tmp_path.glob("*-retrieval-held_out-*.json")).read_text(
        encoding="utf-8"))  # fmt: skip
    assert code == 0 and len(client.sent) == 20
    assert data["run"]["search_settings"]["threshold"] == 0.25
    assert data["run"]["threshold_choice"] is None


def test_the_result_is_written_before_the_report_is_printed(
    module: ModuleType, committed_db: CommittedDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The calls are paid for before the report: a report that cannot be printed (a
    console code page without a character) must not lose the result file."""

    def unprintable(data: object) -> str:
        raise UnicodeEncodeError("cp1253", "≥", 0, 1, "character maps to <undefined>")

    monkeypatch.setattr(module, "report", unprintable)
    with pytest.raises(UnicodeEncodeError):
        run(module, committed_db, tmp_path, "--split", "dev")
    assert len(list(tmp_path.glob("*-retrieval-dev-*.json"))) == 1
