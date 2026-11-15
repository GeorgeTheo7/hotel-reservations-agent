"""The smoke run: extraction on a small case set with a real model (the Greek check, D2).

    uv run --env-file .env python eval/smoke.py --model gpt-6-luna --repeat 3

Calls the OpenAI API (OPENAI_API_KEY in .env) under the spend cap
(ANEMOS_LLM_BUDGET_USD). Prints one line per call and a summary, and writes the full
record to eval/results/<time>-<set>-<model>.json. Refuses a dirty working tree unless
--allow-dirty (the result is then marked dirty: not comparable). Never run by pytest
or CI: tests call main() with a scripted client.
"""

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

from anemos.clock import Clock, SystemClock
from anemos.evaluation import (
    RunInfo,
    git_state,
    load_set,
    report,
    run_cases,
    summarise,
    write_results,
)
from anemos.llm.adapter import LLM, ModelClient, Settings
from anemos.llm.budget import Budget, cap_from_env
from anemos.llm.openai_client import OpenAIClient
from anemos.llm.pricing import PRICES
from anemos.prompts import CURRENT_EXTRACTION

REPO = Path(__file__).resolve().parents[1]
EVAL = REPO / "eval"


def main(
    argv: list[str] | None = None,
    *,
    client: ModelClient | None = None,
    clock: Clock | None = None,
    budget: Budget | None = None,
    repo: Path = REPO,
) -> int:
    parser = argparse.ArgumentParser(prog="eval/smoke.py", description=__doc__.split("\n")[0])
    parser.add_argument("--model", required=True, choices=sorted(PRICES))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--set", default="smoke", dest="set_name")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--results-dir", type=Path, default=EVAL / "results")
    args = parser.parse_args(argv)

    commit, dirty = git_state(repo)
    if dirty and not args.allow_dirty:
        print("the working tree has uncommitted changes: commit them, or pass "
              "--allow-dirty (the result is then marked dirty)", file=sys.stderr)  # fmt: skip
        return 2
    cases = load_set(args.set_name, EVAL)
    clock = clock or SystemClock()
    spec = CURRENT_EXTRACTION
    settings = Settings(args.model, spec.max_output_tokens, spec.reasoning_effort)
    llm = LLM(client or OpenAIClient(), settings, budget or Budget(clock, cap_from_env()),
              None, clock)  # fmt: skip
    started = clock.now()
    rows = run_cases(llm, cases, spec, step=f"eval-{args.set_name}", repeat=args.repeat)
    summary = summarise(rows)
    info = RunInfo(set_name=args.set_name, model=args.model, settings=asdict(settings),
                   prompt_version=spec.version, commit=commit, dirty=dirty,
                   started_at=started, repeat=args.repeat)  # fmt: skip
    print(report(rows, summary))
    print(f"\nwritten: {write_results(args.results_dir, info, rows, summary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
