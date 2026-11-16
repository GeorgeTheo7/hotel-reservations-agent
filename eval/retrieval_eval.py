"""The retrieval eval (D15): recall@3 and NO_MATCH for vector only, full text only,
hybrid, and the production selector (`search_policies`), on one split of
eval/retrieval/queries.yaml.

    uv run --env-file .env python eval/retrieval_eval.py --split dev --repeat 3
    uv run --env-file .env python eval/retrieval_eval.py --split held_out --repeat 3 ...
        (held_out also takes the dev threshold: --threshold 0.394)

Against a scratch database (`<dev db>_retrieval_test`: migrated and seeded, so the
packaged policy chunks are loaded), never the dev database. The queries are embedded in
one call, twice: the second embedding only measures how much a query's best similarity
moves between two calls. The three ranking modes come from one `fetch_rows` per query
(anemos.policies.ranking). The selector is `search_policies` exactly as production
calls it (anemos.policies.search.select, prompt policy_select_v1), --repeat times per
query: each repeat is a new call, scored on its own, and the result file records the
consistency share and pass or fail per line of the selector's bar (docs/NOTES.md).

dev: the threshold is chosen by the rule in docs/NOTES.md (anemos.retrieval_scoring.
choose_threshold). held_out: the threshold chosen on dev must be given; held-out queries
never choose it. The threshold is for the ranking modes only; the selector has none.

Before any call the worst-case cost is estimated; over --max-usd (default 0.10) nothing
is sent. Refuses a dirty working tree unless --allow-dirty (then recorded). Writes
eval/results/<time>-retrieval-<split>-<selector model>.json with the models, settings,
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
    Settings,
    max_embed_input_tokens,
    max_input_tokens,
)
from anemos.llm.budget import Budget, cap_from_env
from anemos.llm.openai_client import OpenAIClient
from anemos.llm.pricing import max_cost
from anemos.policies import PolicySelection
from anemos.policies.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    read_embeddings,
)
from anemos.policies.query import InvalidQuery
from anemos.policies.ranking import (
    Row,
    SearchSettings,
    check_table,
    fetch_rows,
    packaged_source,
)
from anemos.policies.search import SELECTOR_MODEL, NoMatch, PolicyHits, select, selector_prompt
from anemos.prompts import POLICY_SELECT_V1
from anemos.retrieval_eval import RetrievalQuery, Split, load_queries
from anemos.retrieval_scoring import (
    RANKING_MODES,
    Mode,
    ModeAnswer,
    ModeScore,
    ThresholdChoice,
    choose_threshold,
    compare,
    consistency_share,
    consistent,
    correct,
    largest_change,
    score_mode,
    search_answers,
    selector_answer,
    selector_bars,
)

REPO = Path(__file__).resolve().parents[1]
EVAL = REPO / "eval"
STEP = "eval-retrieval"


@dataclass(frozen=True)
class QueryResult:
    query: RetrievalQuery
    best_similarity: float
    best_similarity_again: float  # from the second embedding of the same query
    answers: dict[Mode, ModeAnswer]  # the ranking modes
    selector: list[ModeAnswer]  # one per repeat
    selector_returned: list[list[str]]  # the ids as the model returned them, per repeat


def estimate(queries: Sequence[RetrievalQuery], repeat: int) -> int:
    """Worst case in µ$: two embedding calls and `repeat` selector calls per query."""
    texts = [q.query for q in queries]
    embed = max_cost(EMBEDDING_MODEL, max_input_tokens=max_embed_input_tokens(texts),
                     max_output_tokens=0)  # fmt: skip
    calls = sum(
        max_cost(SELECTOR_MODEL,
                 max_input_tokens=max_input_tokens(selector_prompt(q.query), PolicySelection),
                 max_output_tokens=POLICY_SELECT_V1.max_output_tokens)
        for q in queries
    )  # fmt: skip
    return 2 * embed + repeat * calls


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
    parser.add_argument("--repeat", type=int, default=1, help="selector calls per query")
    parser.add_argument("--max-usd", type=float, default=0.10)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--results-dir", type=Path, default=EVAL / "results")
    args = parser.parse_args(argv)
    split: Split = args.split
    if (split == "held_out") != (args.threshold is not None):
        parser.error("--threshold is given exactly for --split held_out (dev chooses it)")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    commit, dirty = git(repo)
    if dirty and not args.allow_dirty:
        print("the working tree has uncommitted changes: commit them first", file=sys.stderr)
        return 2
    case_splits = {c.id: c.split for c in load_cases(eval_dir / "emails")}
    queries_file = eval_dir / "retrieval" / "queries.yaml"
    queries = [q for q in load_queries(queries_file, case_splits=case_splits) if q.split == split]
    worst = estimate(queries, args.repeat)
    print(f"{len(queries)} {split} queries x{args.repeat}, worst-case cost {worst} µ$"
          f" (${worst / 1e6:.4f})")  # fmt: skip
    if worst > args.max_usd * 1_000_000:
        print(f"over --max-usd {args.max_usd}: nothing sent", file=sys.stderr)
        return 3

    clock = clock or SystemClock()
    started = clock.now()
    budget = budget or Budget(clock, cap_from_env())
    spent_before = budget.spent()
    openai = OpenAIClient()  # no network until a call; replaced by fakes in tests
    # The selector passes its own settings (D2); these defaults are never used for it.
    llm = LLM(client or openai, Settings(SELECTOR_MODEL, POLICY_SELECT_V1.max_output_tokens),
              budget, None, clock, embed_client=embed_client or openai)  # fmt: skip
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
    response_models: set[str] = set()
    results: list[QueryResult] = []
    for query, found, found_again, top in zip(queries, rows, rows_again, best, strict=True):
        repeats: list[ModeAnswer] = []
        returned: list[list[str]] = []
        for _ in range(args.repeat):
            result = selector_call(llm, query)
            repeats.append(selector_answer(result))
            returned.append(list(result.selection.returned))
            response_models.add(result.selection.response_model)
        results.append(QueryResult(query, top, _best(found_again),
                                   search_answers(query.query, found, search), repeats,
                                   returned))  # fmt: skip

    run = {
        "split": split, "commit": commit, "dirty": dirty, "started_at": started.isoformat(),
        "embedding_model": EMBEDDING_MODEL, "embedding_response_model": first.response_model,
        "selector_model": SELECTOR_MODEL, "selector_response_models": sorted(response_models),
        "selector_prompt_version": POLICY_SELECT_V1.version,
        "selector_settings": {"max_output_tokens": POLICY_SELECT_V1.max_output_tokens,
                              "reasoning_effort": POLICY_SELECT_V1.reasoning_effort},
        "repeat": args.repeat, "search_settings": asdict(search),
        "threshold_choice": None if choice is None else asdict(choice),
        "policies_sha256": source.policies_sha256, "embeddings_generated": _generated(),
        "queries_sha256": sha256(queries_file), "estimate_micro_usd": worst,
        "cost_micro_usd": budget.spent() - spent_before,
    }  # fmt: skip
    data = {"run": run, "summary": summarise(results), "queries": [_row(r) for r in results]}
    print(report(data))
    path = write(args.results_dir, started, split, SELECTOR_MODEL, data)
    print(f"\nwritten: {path}")
    return 0


def selector_call(llm: LLM, query: RetrievalQuery) -> PolicyHits | NoMatch:
    """One production `search_policies` call. The loader refuses any query the tool
    would refuse, so an InvalidQuery here is a bug."""
    result = select(llm, query.query, email_id=None)
    if isinstance(result, InvalidQuery):
        raise AssertionError(f"{query.id}: the tool refused a loaded query: {result.message}")
    return result


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


def _score(score: ModeScore) -> dict[str, Any]:
    return asdict(score) | {
        "recall_at_3": score.recall_at_3, "ranked_recall_at_3": score.ranked_recall_at_3,
        "no_match_precision": score.no_match_precision, "no_match_recall": score.no_match_recall,
    }  # fmt: skip


def _selector_scores(results: Sequence[QueryResult]) -> list[ModeScore]:
    repeats = len(results[0].selector) if results else 0
    return [score_mode([(r.query, r.selector[n]) for r in results]) for n in range(repeats)]


def _scores(results: Sequence[QueryResult]) -> dict[str, Any]:
    out: dict[str, Any] = {
        mode: _score(score_mode([(r.query, r.answers[mode]) for r in results]))
        for mode in RANKING_MODES
    }
    out["selector"] = [_score(s) for s in _selector_scores(results)]  # one per repeat
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
    share = consistency_share([r.selector for r in results])
    bars = selector_bars(_selector_scores(results), share)
    return {
        "modes": _scores(results),
        "selector_consistency": share,
        "selector_inconsistent": [r.query.id for r in results if not consistent(r.selector)],
        "selector_bar": [asdict(b) | {"passed": b.passed} for b in bars],
        "selector_bar_passed": all(b.passed for b in bars),
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
        "modes": {m: asdict(a) | {"right": correct(r.query, a)} for m, a in r.answers.items()},
        "selector": [asdict(a) | {"right": correct(r.query, a), "returned": returned}
                     for a, returned in zip(r.selector, r.selector_returned, strict=True)],
        "selector_consistent": consistent(r.selector),
    }  # fmt: skip


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


NOISE = 0.0005  # docs/NOTES.md: a change this large makes the third decimal noise


def third_decimal(change: float) -> str:
    if change >= NOISE:
        return "the threshold's third decimal is noise"
    return "the threshold's third decimal is stable"


def _mode_line(name: str, s: dict[str, Any]) -> str:
    return (f"  {name:11} {_pct(s['recall_at_3']):>8}  {_pct(s['ranked_recall_at_3']):>18}"
            f"  {_pct(s['no_match_precision']):>18}  {_pct(s['no_match_recall']):>15}")  # fmt: skip


def report(data: dict[str, Any]) -> str:
    run, summary = data["run"], data["summary"]
    modes = summary["modes"]
    lines = [f"split {run['split']}, selector x{run['repeat']}, threshold"
             f" {run['search_settings']['threshold']} (ranking modes only),"
             f" cost {run['cost_micro_usd']} µ$", "",
             "mode         recall@3  (before threshold)  NO_MATCH precision"
             "  NO_MATCH recall"]  # fmt: skip
    lines += [_mode_line(mode, modes[mode]) for mode in RANKING_MODES]
    lines += [_mode_line(f"selector #{n}", s) for n, s in enumerate(modes["selector"], start=1)]
    lines += ["", f"selector consistency: {_pct(summary['selector_consistency'])}"
              f" (inconsistent: {summary['selector_inconsistent']})", "selector bar:"]  # fmt: skip
    lines += [f"  {'PASS' if b['passed'] else 'FAIL'}  {b['line']}: {_pct(b['value'])}"
              f" (target ≥ {b['target']:.0%})" for b in summary["selector_bar"]]  # fmt: skip
    comparison = summary["hybrid_vs_vector"]
    lines += ["", f"hybrid vs vector only: {comparison['verdict']}"
              f" (hybrid only: {list(comparison['only_first'])};"
              f" vector only: {list(comparison['only_second'])})",
              f"out of scope through the threshold: {summary['out_of_scope_through_threshold']}",
              f"largest change in best similarity between two embeddings:"
              f" {summary['largest_best_similarity_change']:.6f}"
              f" ({third_decimal(summary['largest_best_similarity_change'])})",
              "", "per query (modes right; selector right per repeat, y/n):"]  # fmt: skip
    for row in data["queries"]:
        right = [m for m, a in row["modes"].items() if a["right"]]
        selector = "".join("y" if a["right"] else "n" for a in row["selector"])
        lines.append(f"  {row['id']:34} best {row['best_similarity']:.3f}  right: {right}"
                     f"  selector {selector}")  # fmt: skip
    return "\n".join(lines)


def write(results_dir: Path, started: datetime, split: str, model: str,
          data: dict[str, Any]) -> Path:  # fmt: skip
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{started.strftime('%Y%m%dT%H%M%SZ')}-retrieval-{split}-{model}.json"
    path.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return path


if __name__ == "__main__":
    sys.exit(main())
