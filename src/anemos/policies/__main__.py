"""`python -m anemos.policies embed | load`.

- embed: embed new or changed chunks with the real model (needs OPENAI_API_KEY; under
  the spend cap) and rewrite embeddings.json. Nothing stale: no call.
- load: write the chunks and vectors into the database (no network). `seed` does this
  too, so a freshly seeded database already has them.
"""

import argparse
import sys
from pathlib import Path

from anemos.clock import SystemClock
from anemos.db.connection import connect
from anemos.evaluation import git_state
from anemos.llm.adapter import LLM, Settings
from anemos.llm.budget import Budget, cap_from_env
from anemos.llm.openai_client import OpenAIClient
from anemos.policies import load_policies
from anemos.policies.embeddings import (
    EMBEDDING_MODEL,
    EMBEDDINGS_FILE,
    read_embeddings,
    refresh,
    stale,
)
from anemos.policies.store import load_policy_chunks

REPO = Path(__file__).resolve().parents[3]


def embed() -> int:
    current = read_embeddings()
    todo = stale(current, load_policies()) if current.model == EMBEDDING_MODEL else ["all"]
    if not todo:
        print("embeddings.json is up to date: nothing to embed")
        return 0
    clock = SystemClock()
    client = OpenAIClient()
    llm = LLM(client, Settings(EMBEDDING_MODEL, max_output_tokens=0),
              Budget(clock, cap_from_env()), None, clock, embed_client=client)  # fmt: skip
    commit, dirty = git_state(REPO)
    updated = refresh(llm, current, commit=commit, dirty=dirty, at=clock.now())
    EMBEDDINGS_FILE.write_text(updated.to_json(), encoding="utf-8", newline="\n")
    print(f"embedded {todo}; {updated.generated}")
    return 0


def load() -> int:
    with connect() as conn:
        count = load_policy_chunks(conn)
    print(f"loaded {count} policy chunks")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m anemos.policies")
    parser.add_argument("command", choices=["embed", "load"])
    args = parser.parse_args(argv)
    return embed() if args.command == "embed" else load()


if __name__ == "__main__":
    sys.exit(main())
