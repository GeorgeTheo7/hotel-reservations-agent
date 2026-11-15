"""Score a results file again with today's scorer, from the outputs it recorded (no model
call, no cost):

    uv run python eval/rescore.py eval/results/<file>.json

The original file is never changed: it is the record of what the run scored. The new
scores go to <file>.rescored.json beside it, with the commit of the scorer used, and are
printed like a run's report.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from anemos.email_cases import load_cases
from anemos.evaluation import git_state, report, rescore

REPO = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None, *, repo: Path = REPO) -> int:
    parser = argparse.ArgumentParser(prog="eval/rescore.py", description=__doc__.split("\n")[0])
    parser.add_argument("results", type=Path)
    args = parser.parse_args(argv)

    source: Path = args.results
    data = json.loads(source.read_text(encoding="utf-8"))
    rows, summary = rescore(data, load_cases(repo / "eval" / "emails"))
    commit, dirty = git_state(repo)
    out = source.with_name(source.stem + ".rescored.json")
    record = {
        "source": source.name,
        "run": data["run"],
        "scorer": {"commit": commit, "dirty": dirty},
        "summary": asdict(summary) | {"consistent_cases": summary.consistent_cases},
        "calls": [{"case_id": r.case_id, "fields": r.fields} for r in rows],
    }
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report(rows, summary))
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
