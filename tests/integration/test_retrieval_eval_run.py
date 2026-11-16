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
from anemos.llm.adapter import Prompt
from anemos.llm.budget import Budget
from anemos.llm.fake import Answer, HashingEmbedClient, ScriptedClient
from anemos.policies import load_policies
from anemos.policies.embeddings import EMBEDDING_DIMENSIONS, policies_sha256
from anemos.prompts import RETRIEVAL_BASELINE_V1

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
        dirty: bool = False) -> tuple[int, ScriptedClient, HashingEmbedClient]:  # fmt: skip
    client = ScriptedClient.of([breakfast_or_nothing] * 100)
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
    assert len(dev) == 48 and len(client.sent) == 48
    assert len(embedder.calls) == 2 and embedder.calls[0] == embedder.calls[1]  # twice

    run_ = data["run"]
    assert (run_["split"], run_["commit"], run_["dirty"]) == ("dev", "c" * 40, False)
    assert run_["started_at"] == NOW.isoformat()
    assert run_["policies_sha256"] == policies_sha256()
    assert run_["embedding_model"] == "text-embedding-3-small"
    assert (run_["baseline_model"], run_["baseline_prompt_version"]) == (
        "gpt-6-luna", RETRIEVAL_BASELINE_V1.version)  # fmt: skip
    assert run_["baseline_settings"]["reasoning_effort"] == "none"
    assert run_["threshold_choice"]["threshold"] == run_["search_settings"]["threshold"]
    assert len(run_["queries_sha256"]) == 64 and run_["embeddings_generated"]["commit"]
    assert run_["cost_micro_usd"] > 0

    summary = data["summary"]
    assert set(summary["modes"]) == {"vector", "full_text", "hybrid", "all_chunks"}
    assert {"written_by=mentor", "written_by=claude", "source=email"} <= set(summary["groups"])
    assert summary["largest_best_similarity_change"] == 0.0  # the fake is deterministic
    assert module.third_decimal(0.0004) == "the threshold's third decimal is stable"
    assert module.third_decimal(0.0005) == "the threshold's third decimal is noise"
    assert summary["hybrid_vs_vector"]["verdict"] in {"better", "worse", "same"}
    row = next(r for r in data["queries"] if r["id"] == "q-e-breakfast")
    assert row["modes"]["all_chunks"] == {"top": ["breakfast"], "no_match": False, "right": True}


def test_the_baseline_sees_every_chunk_and_the_question(
    module: ModuleType, committed_db: CommittedDb, tmp_path: Path
) -> None:
    _, client, _ = run(module, committed_db, tmp_path, "--split", "dev")
    sent = client.sent[0].prompt.input
    assert all(f"[{c.policy_id}] {c.title}" in sent for c in load_policies())
    assert sent.endswith("</question>")


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
