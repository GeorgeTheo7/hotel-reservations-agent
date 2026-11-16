"""`search_policies` (D15, D73): a model selector over every policy chunk.

One call, in this order:

1. Check the query (pure). Blank, over 300 characters, containing Greek letters, or
   containing `<` or `>` (it could close the prompt's `<question>` block early):
   `InvalidQuery`, an invalid call that counts toward the loop's 8 (D11); its message
   tells the model how to fix the query. No model call.
2. One structured call through the LLM adapter (budget, one retry, an audit row per
   attempt with the run's email id): the current prompt (`policy_select_v2`), every
   packaged chunk and then the query, so the instructions and the chunks are a cached
   prefix. The answer is at most 3 ids (maxItems in the schema) from an enum of the
   real ids (`PolicySelection`). A failed call, a reply cut off at the output limit or
   one that does not fit the schema included, raises `LLMError`: never `NoMatch`, and
   never a silent cut of a 4th id. The loop escalates it `SYSTEM_ERROR` (D49).
3. The distinct ids are the hits; an empty list is `NoMatch` → `NO_POLICY`.

The chunks come from the packaged policies.md: no database and no transaction. The
record keeps each hit's text and the hash of policies.md, so guardrails 2 and 6 check a
draft against exactly what this call returned.

The vector / full-text / hybrid ranking is in `anemos.policies.ranking`, for the
retrieval eval only; this module does not import it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any

from anemos.llm.adapter import LLM, Prompt, Settings
from anemos.policies import PolicyChunk, PolicySelection, load_policies
from anemos.policies.embeddings import policies_sha256, text_sha256
from anemos.policies.query import InvalidQuery, check_query
from anemos.prompts import CURRENT_POLICY_SELECT

STEP = "search_policies"
SELECTOR_MODEL = "gpt-6-luna"  # D2, D15


def policies_block(chunks: Sequence[PolicyChunk]) -> str:
    """Every chunk with its id and title: the part of the input that never changes."""
    sections = "\n\n".join(f"[{c.policy_id}] {c.title}\n{c.text}" for c in chunks)
    return f"<policies>\n{sections}\n</policies>\n\n"


@cache
def _packaged_block() -> str:
    return policies_block(load_policies())


def selector_prompt(query: str) -> Prompt:
    """Instructions, then every chunk, then the query last: the cached prefix."""
    return Prompt(
        instructions=CURRENT_POLICY_SELECT.text(),
        input=f"{_packaged_block()}<question>\n{query}\n</question>",
    )


@dataclass(frozen=True)
class Hit:
    policy_id: str
    title: str
    text: str
    text_sha256: str


@dataclass(frozen=True)
class Selection:
    """What the selector call was and returned, shared by both outcomes' records."""

    query: str
    returned: tuple[str, ...]  # the ids exactly as the model returned them
    policies_sha256: str
    model: str
    response_model: str
    prompt_version: str

    def record(self) -> dict[str, Any]:
        return {
            "query": self.query, "returned": list(self.returned),
            "policies_sha256": self.policies_sha256, "model": self.model,
            "response_model": self.response_model, "prompt_version": self.prompt_version,
        }  # fmt: skip


@dataclass(frozen=True)
class PolicyHits:
    selection: Selection
    hits: tuple[Hit, ...]

    @property
    def query(self) -> str:
        return self.selection.query

    def record(self) -> dict[str, Any]:
        """What the run stores for this call: the text the model may cite, and the hash
        of the policies.md it came from."""
        return self.selection.record() | {"hits": [vars(h) for h in self.hits]}


@dataclass(frozen=True)
class NoMatch:
    selection: Selection

    @property
    def query(self) -> str:
        return self.selection.query

    def record(self) -> dict[str, Any]:
        return self.selection.record() | {"no_match": True}


def select(llm: LLM, query: str, *, email_id: int | None) -> PolicyHits | NoMatch | InvalidQuery:
    """Steps 1-3 of the module docstring. Raises LLMError when the call fails."""
    checked = check_query(query)
    if isinstance(checked, InvalidQuery):
        return checked
    spec = CURRENT_POLICY_SELECT
    settings = Settings(SELECTOR_MODEL, spec.max_output_tokens, spec.reasoning_effort)
    result = llm.structured(selector_prompt(checked), PolicySelection, step=STEP,
                            prompt_version=spec.version, email_id=email_id,
                            settings=settings)  # fmt: skip
    returned = tuple(result.output.policy_ids)
    selection = Selection(checked, returned, policies_sha256(), result.model,
                          result.response_model, spec.version)  # fmt: skip
    chunks = {c.policy_id: c for c in load_policies()}
    ids = list(dict.fromkeys(returned))  # repeated ids count once; at most 3 by the schema
    if not ids:
        return NoMatch(selection)
    hits = tuple(Hit(i, chunks[i].title, chunks[i].text, text_sha256(chunks[i])) for i in ids)
    return PolicyHits(selection, hits)


search_policies = select  # the tool's name (spec Tools)
