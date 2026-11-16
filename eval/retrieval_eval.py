"""The retrieval eval (D15): recall@3 and NO_MATCH for vector only, full text only,
hybrid, and the all-chunks-in-prompt baseline, on one split of eval/retrieval/queries.yaml.

    uv run --env-file .env python eval/retrieval_eval.py --split dev
    uv run --env-file .env python eval/retrieval_eval.py --split held_out --threshold 0.xyz

Against a scratch database (`<dev db>_retrieval_test`: migrated and seeded, so the
packaged policy chunks are loaded), never the dev database. The queries are embedded in
one call, twice: the second embedding only measures how much a query's best similarity
moves between two calls. The three search modes come from one `fetch_rows` per query
(the same SQL and the same ranking code as `search_policies`); the baseline is one
`retrieval_baseline_v1` call per query.

dev: the threshold is chosen by the rule in docs/NOTES.md (anemos.retrieval_scoring.
choose_threshold). held_out: the threshold chosen on dev must be given; held-out queries
never choose it.

Before any call the worst-case cost is estimated; over --max-usd (default 0.10) nothing
is sent. Refuses a dirty working tree unless --allow-dirty (then recorded). Writes
eval/results/<time>-retrieval-<split>-<baseline model>.json with the model, settings,
commit, dirty flag, time, policies_sha256 and every query's answers. Never run by
pytest or CI: tests call main() with fakes.
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import anemos.db.scratch as scratch
from anemos.clock import Clock, SystemClock
from anemos.db.connection import connect
from anemos.email_cases import load_cases
from anemos.evaluation import git_state
from anemos.llm.adapter import (
    LLM,
    EmbedClient,
    ModelClient,
    Prompt,
    Settings,
    max_embed_input_tokens,
    max_input_tokens,
)
from anemos.llm.budget import Budget, cap_from_env
from anemos.llm.openai_client import OpenAIClient
from anemos.llm.pricing import EMBEDDING_MODELS, PRICES, max_cost
from anemos.policies import load_policies
from anemos.policies.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    read_embeddings,
)
from anemos.policies.search import Row, SearchSettings, check_table, fetch_rows, packaged_source
from anemos.prompts import RETRIEVAL_BASELINE_V1
from anemos.retrieval_eval import RetrievalQuery, Split, load_queries
from anemos.retrieval_scoring import (
    MODES,
    BaselineAnswer,
    Mode,
    ModeAnswer,
    ThresholdChoice,
    baseline_answer,
    choose_threshold,
    compare,
    correct,
    largest_change,
    score_mode,
    search_answers,
)

REPO = Path(__file__).resolve().parents[1]
EVAL = REPO / "eval"
STEP = "eval-retrieval"
BASELINE_MODEL = "gpt-6-luna"  # D2: the agent's model


@dataclass(frozen=True)
class QueryResult:
    query: RetrievalQuery
    best_similarity: float
    best_similarity_again: float  # from the second embedding of the same query
    answers: dict[Mode, ModeAnswer]
    baseline_returned: list[str]  # the baseline's ids as returned, before the top 3


def policies_input(question: str) -> str:
    """The baseline's input: every chunk with its id and title, then the question."""
    sections = "\n\n".join(f"[{c.policy_id}] {c.title}\n{c.text}" for c in load_policies())
    return f"<policies>\n{sections}\n</policies>\n\n<question>\n{question}\n</question>"


def baseline_prompt(question: str) -> Prompt:
    return Prompt(instructions=RETRIEVAL_BASELINE_V1.text(), input=policies_input(question))


def estimate(queries: Sequence[RetrievalQuery], model: str) -> int:
    """Worst case in µ$: two embedding calls and one baseline call per query."""
    texts = [q.query for q in queries]
    embed = max_cost(EMBEDDING_MODEL, max_input_tokens=max_embed_input_tokens(texts),
                     max_output_tokens=0)  # fmt: skip
    spec = RETRIEVAL_BASELINE_V1
    calls = sum(
        max_cost(model, max_input_tokens=max_input_tokens(baseline_prompt(q.query), spec.schema),
                 max_output_tokens=spec.max_output_tokens)
        for q in queries
    )  # fmt: skip
    return 2 * embed + calls


def main(
    argv: list[str] | None = None,
    *,
    client: ModelClient | None = None,
    embed_client: EmbedClient | None = None,
    clock: Clock | None = None,
    budget: Budget | None = None,
    repo: Path = REPO,
    eval_dir: Path = EVAL,
    conninfo: str | None = None,
    git: Callable[[Path], tuple[str, bool]] = git_state,
) -> int:
    parser = argparse.ArgumentParser(prog="eval/retrieval_eval.py",
                                     description=__doc__.split("\n")[0])  # fmt: skip
    parser.add_argument("--split", choices=["dev", "held_out"], required=True)
    parser.add_argument("--threshold", type=float, help="held_out only: the dev threshold")
    parser.add_argument("--model", default=BASELINE_MODEL,
                        choices=sorted(set(PRICES) - EMBEDDING_MODELS))  # fmt: skip
    parser.add_argument("--max-usd", type=float, default=0.10)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--results-dir", type=Path, default=EVAL / "results")
    args = parser.parse_args(argv)
    split: Split = args.split
    if (split == "held_out") != (args.threshold is not None):
        parser.error("--threshold is given exactly for --split held_out (dev chooses it)")

    commit, dirty = git(repo)
    if dirty and not args.allow_dirty:
        print("the working tree has uncommitted changes: commit them first", file=sys.stderr)
        return 2
    case_splits = {c.id: c.split for c in load_cases(eval_dir / "emails")}
    queries_file = eval_dir / "retrieval" / "queries.yaml"
    queries = [q for q in load_queries(queries_file, case_splits=case_splits) if q.split == split]
    worst = estimate(queries, args.model)
    print(f"{len(queries)} {split} queries, worst-case cost {worst} µ$ (${worst / 1e6:.4f})")
    if worst > args.max_usd * 1_000_000:
        print(f"over --max-usd {args.max_usd}: nothing sent", file=sys.stderr)
        return 3

    clock = clock or SystemClock()
    started = clock.now()
    spec = RETRIEVAL_BASELINE_V1
    settings = Settings(args.model, spec.max_output_tokens, spec.reasoning_effort)
    openai = OpenAIClient()  # no network until a call; replaced by fakes in tests
    llm = LLM(client or openai, settings, budget or Budget(clock, cap_from_env()), None, clock,
              embed_client=embed_client or openai)  # fmt: skip
    source = packaged_source()
    texts = [q.query for q in queries]
    version = f"queries@{sha256(queries_file)[:8]}"
    first = llm.embed(texts, model=EMBEDDING_MODEL, dimensions=EMBEDDING_DIMENSIONS,
                      step=STEP, input_version=version)  # fmt: skip
    again = llm.embed(texts, model=EMBEDDING_MODEL, dimensions=EMBEDDING_DIMENSIONS,
                      step=STEP, input_version=version)  # fmt: skip
    conninfo = conninfo or scratch.prepare("retrieval")
    with connect(conninfo) as conn, conn.transaction():
        check_table(conn, source)
        rows = [fetch_rows(conn, v, q.query) for q, v in zip(queries, first.vectors, strict=True)]
        rows_again = [fetch_rows(conn, v, q.query)
                      for q, v in zip(queries, again.vectors, strict=True)]  # fmt: skip

    best = [max(r.similarity for r in found) for found in rows]
    threshold, choice = choose(queries, best, args.threshold)
    search = SearchSettings(threshold)
    baseline_cost = 0
    response_models: set[str] = set()
    results: list[QueryResult] = []
    for query, found, found_again, top in zip(queries, rows, rows_again, best, strict=True):
        answer = llm.structured(baseline_prompt(query.query), BaselineAnswer, step=STEP,
                                prompt_version=spec.version)  # fmt: skip
        baseline_cost += answer.cost_micro_usd
        response_models.add(answer.response_model)
        answers = search_answers(query.query, found, search, source)
        answers["all_chunks"] = baseline_answer(answer.output, search.top_k)
        results.append(QueryResult(query, top, _best(found_again), answers,
                                   list(answer.output.policy_ids)))  # fmt: skip

    cost = first.cost_micro_usd + again.cost_micro_usd + baseline_cost
    run = {
        "split": split, "commit": commit, "dirty": dirty, "started_at": started.isoformat(),
        "embedding_model": EMBEDDING_MODEL, "embedding_response_model": first.response_model,
        "baseline_model": args.model, "baseline_response_models": sorted(response_models),
        "baseline_settings": asdict(settings), "baseline_prompt_version": spec.version,
        "search_settings": asdict(search), "threshold_choice": None if choice is None
        else asdict(choice), "policies_sha256": source.policies_sha256,
        "embeddings_generated": _generated(), "queries_sha256": sha256(queries_file),
        "estimate_micro_usd": worst, "cost_micro_usd": cost,
    }  # fmt: skip
    data = {"run": run, "summary": summarise(results), "queries": [_row(r) for r in results]}
    print(report(data))
    path = write(args.results_dir, started, split, args.model, data)
    print(f"\nwritten: {path}")
    return 0


def choose(
    queries: Sequence[RetrievalQuery], best: Sequence[float], given: float | None
) -> tuple[float, ThresholdChoice | None]:
    if given is not None:
        return given, None
    pairs = list(zip(queries, best, strict=True))
    choice = choose_threshold([b for q, b in pairs if q.in_scope],
                              [b for q, b in pairs if not q.in_scope])  # fmt: skip
    return choice.threshold, choice


def _best(rows: Sequence[Row]) -> float:
    return max(r.similarity for r in rows)


def _generated() -> dict[str, Any] | None:
    generated = read_embeddings().generated
    return None if generated is None else vars(generated)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scores(results: Sequence[QueryResult]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mode in MODES:
        s = score_mode([(r.query, r.answers[mode]) for r in results])
        out[mode] = asdict(s) | {
            "recall_at_3": s.recall_at_3, "ranked_recall_at_3": s.ranked_recall_at_3,
            "no_match_precision": s.no_match_precision, "no_match_recall": s.no_match_recall,
        }  # fmt: skip
    return out


def summarise(results: Sequence[QueryResult]) -> dict[str, Any]:
    groups: dict[str, list[QueryResult]] = {}
    for r in results:
        groups.setdefault(f"written_by={r.query.written_by}", []).append(r)
        groups.setdefault(f"source={r.query.source}", []).append(r)
    hybrid = [(r.query, r.answers["hybrid"]) for r in results]
    vector = [(r.query, r.answers["vector"]) for r in results]
    threshold_misses = [r.query.id for r in results
                        if not r.query.in_scope and not r.answers["vector"].no_match]  # fmt: skip
    return {
        "modes": _scores(results),
        "groups": {name: _scores(members) for name, members in sorted(groups.items())},
        "hybrid_vs_vector": asdict(compare(hybrid, vector)),
        "out_of_scope_through_threshold": threshold_misses,
        "largest_best_similarity_change": largest_change(
            [r.best_similarity for r in results], [r.best_similarity_again for r in results]
        ),
    }


def _row(r: QueryResult) -> dict[str, Any]:
    return {
        "id": r.query.id, "query": r.query.query, "expected": r.query.expected,
        "written_by": r.query.written_by, "source": r.query.source,
        "best_similarity": r.best_similarity, "best_similarity_again": r.best_similarity_again,
        "baseline_returned": r.baseline_returned,
        "modes": {m: asdict(a) | {"right": correct(r.query, a)} for m, a in r.answers.items()},
    }  # fmt: skip


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


NOISE = 0.0005  # docs/NOTES.md: a change this large makes the third decimal noise


def third_decimal(change: float) -> str:
    if change >= NOISE:
        return "the threshold's third decimal is noise"
    return "the threshold's third decimal is stable"


def report(data: dict[str, Any]) -> str:
    run, summary = data["run"], data["summary"]
    lines = [f"split {run['split']}, threshold {run['search_settings']['threshold']},"
             f" cost {run['cost_micro_usd']} µ$", "",
             "mode         recall@3  (before threshold)  NO_MATCH precision"
             "  NO_MATCH recall"]  # fmt: skip
    for mode, s in summary["modes"].items():
        lines.append(f"  {mode:11} {_pct(s['recall_at_3']):>8}"
                     f"  {_pct(s['ranked_recall_at_3']):>18}"
                     f"  {_pct(s['no_match_precision']):>18}"
                     f"  {_pct(s['no_match_recall']):>15}")  # fmt: skip
    comparison = summary["hybrid_vs_vector"]
    lines += ["", f"hybrid vs vector only: {comparison['verdict']}"
              f" (hybrid only: {list(comparison['only_first'])};"
              f" vector only: {list(comparison['only_second'])})",
              f"out of scope through the threshold: {summary['out_of_scope_through_threshold']}",
              f"largest change in best similarity between two embeddings:"
              f" {summary['largest_best_similarity_change']:.6f}"
              f" ({third_decimal(summary['largest_best_similarity_change'])})",
              "", "per query (modes right):"]  # fmt: skip
    for row in data["queries"]:
        right = [m for m, a in row["modes"].items() if a["right"]]
        lines.append(f"  {row['id']:34} best {row['best_similarity']:.3f}  right: {right}")
    return "\n".join(lines)


def write(results_dir: Path, started: datetime, split: str, model: str,
          data: dict[str, Any]) -> Path:  # fmt: skip
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{started.strftime('%Y%m%dT%H%M%SZ')}-retrieval-{split}-{model}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
