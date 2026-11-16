"""`search_policies` (D15, D73): hybrid search over `policy_chunks`.

One call, in this order (D65, D71: no transaction is open during a model call):

1. Check the query (pure). Blank, over 300 characters, or containing Greek letters:
   `InvalidQuery`, an invalid call that counts toward the loop's 8 (D11); its message
   tells the model how to fix the query. No embedding, no database.
2. Embed the query through the LLM adapter (budget, one retry, an audit row per attempt
   with the run's email id), with no transaction open. A failed embedding raises
   `LLMError`: never `NoMatch`. The loop escalates it `SYSTEM_ERROR` (D49).
3. One short read transaction, so the check and the search see one snapshot: the table
   must fit the packaged policies (same ids and text hashes) and the configured
   embedding model, else `PolicyTableError` (also `SYSTEM_ERROR`); then one query
   returns each chunk's cosine similarity and full-text rank.

Two ranked lists, fused by RRF (reciprocal rank fusion: each list adds 1/(k + rank)
for a chunk, so cosine similarity and ts_rank never need a common scale):

- vector: every chunk, by cosine similarity (exact search, no index, D15);
- full text: chunks matching at least one query lexeme, by ts_rank with length
  normalisation. The lexemes are ORed: AND (plainto_tsquery) would need every word of a
  natural query in one chunk and usually match nothing. ts_rank has no IDF, so a word
  common to many chunks ("room") weighs as much as a rare one ("dog"); RRF only uses
  positions, which limits the harm, and the retrieval eval measures it.

Every ordering breaks ties by `policy_id`, so a query always gives the same result.
`NoMatch` when the best cosine similarity is below the threshold; only the vector side
decides it (D15).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any

import psycopg
from psycopg.rows import TupleRow

from anemos.llm.adapter import LLM, Vector
from anemos.policies import PolicyChunk, load_policies
from anemos.policies.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    policies_sha256,
    text_sha256,
)
from anemos.policies.store import vector_literal

type Conn = psycopg.Connection[TupleRow]

STEP = "search_policies"
QUERY_MAX_CHARS = 300
FIX_HINT = f"write the query in English, up to {QUERY_MAX_CHARS} characters"
_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")  # Greek and Coptic, Greek Extended


class PolicyTableError(RuntimeError):
    """`policy_chunks` does not fit the packaged policies or the configured model: a
    system error (run the seed), never a NO_MATCH."""


@dataclass(frozen=True)
class SearchSettings:
    """`threshold` has no default: the production value comes from the retrieval eval's
    dev split (D15); tests set their own for the fake embedder."""

    threshold: float
    top_k: int = 3
    rrf_k: int = 60


@dataclass(frozen=True)
class PolicySource:
    """The policy text a search must find in the table: chunk id → text hash, and the
    hash of the policies.md they came from (recorded with every result)."""

    hashes: Mapping[str, str]
    policies_sha256: str

    @classmethod
    def of(cls, chunks: Sequence[PolicyChunk], policies_hash: str) -> "PolicySource":
        return cls({c.policy_id: text_sha256(c) for c in chunks}, policies_hash)


@cache
def packaged_source() -> PolicySource:
    """Hashed once per process: the packaged files do not change while it runs."""
    return PolicySource.of(load_policies(), policies_sha256())


@dataclass(frozen=True)
class InvalidQuery:
    query: str
    message: str

    def record(self) -> dict[str, Any]:
        return {"query": self.query, "invalid_query": self.message}


def check_query(query: str) -> str | InvalidQuery:
    """The query, stripped, or why the model must write it again."""
    text = query.strip()
    if not text:
        problem = "the query is blank"
    elif len(text) > QUERY_MAX_CHARS:
        problem = f"the query has {len(text)} characters"
    elif _GREEK.search(text):
        problem = "the query contains Greek letters"
    else:
        return text
    return InvalidQuery(query, f"{problem}: {FIX_HINT}")


@dataclass(frozen=True)
class Row:
    """One chunk as the search query returns it."""

    policy_id: str
    title: str
    text: str
    text_sha256: str
    similarity: float
    fts_rank: float | None  # None: no query lexeme in the chunk


def vector_ranking(rows: Sequence[Row]) -> list[str]:
    return [r.policy_id for r in sorted(rows, key=lambda r: (-r.similarity, r.policy_id))]


def fts_ranking(rows: Sequence[Row]) -> list[str]:
    matched = [r for r in rows if r.fts_rank is not None]
    return [r.policy_id for r in sorted(matched, key=lambda r: (-(r.fts_rank or 0), r.policy_id))]


def rrf(rankings: Sequence[Sequence[str]], k: int) -> list[tuple[str, float]]:
    """Fused (policy_id, score), best first; ties by policy_id. Ranks start at 1."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, policy_id in enumerate(ranking, start=1):
            scores[policy_id] = scores.get(policy_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


@dataclass(frozen=True)
class Hit:
    policy_id: str
    title: str
    text: str
    text_sha256: str
    similarity: float
    fts_rank: float | None
    rrf: float


@dataclass(frozen=True)
class PolicyHits:
    query: str
    hits: tuple[Hit, ...]
    settings: SearchSettings
    policies_sha256: str
    embedding_model: str

    def record(self) -> dict[str, Any]:
        """What the run stores for this call: the text the model saw, and the hash of
        the policies.md it came from."""
        return _record(self, hits=[vars(h) for h in self.hits])


@dataclass(frozen=True)
class NoMatch:
    query: str
    best_similarity: float
    settings: SearchSettings
    policies_sha256: str
    embedding_model: str

    def record(self) -> dict[str, Any]:
        return _record(self, no_match=True, best_similarity=self.best_similarity)


def _record(result: PolicyHits | NoMatch, **outcome: object) -> dict[str, Any]:
    return {
        "query": result.query,
        "policies_sha256": result.policies_sha256,
        "embedding_model": result.embedding_model,
        "threshold": result.settings.threshold,
        "top_k": result.settings.top_k,
        "rrf_k": result.settings.rrf_k,
        **outcome,
    }


def decide(
    query: str, rows: Sequence[Row], settings: SearchSettings, source: PolicySource
) -> PolicyHits | NoMatch:
    """NoMatch below the threshold (vector side only), else the top_k fused hits."""
    vector = vector_ranking(rows)
    by_id = {r.policy_id: r for r in rows}
    best = by_id[vector[0]].similarity
    if best < settings.threshold:
        return NoMatch(query, best, settings, source.policies_sha256, EMBEDDING_MODEL)
    fused = rrf([vector, fts_ranking(rows)], settings.rrf_k)[: settings.top_k]
    hits = tuple(
        Hit(r.policy_id, r.title, r.text, r.text_sha256, r.similarity, r.fts_rank, score)
        for policy_id, score in fused
        for r in (by_id[policy_id],)
    )
    return PolicyHits(query, hits, settings, source.policies_sha256, EMBEDDING_MODEL)


# The query's lexemes, each quoted for tsquery input ('' for a quote, \\ for a
# backslash), ORed. Taken from to_tsvector so the query is stemmed like the chunks;
# NULL when every word is a stop word (then no chunk matches the full-text side).
SEARCH_SQL = r"""
WITH q AS (
    SELECT %(vector)s::vector AS v,
           (SELECT string_agg(
                       '''' || replace(replace(lexeme, '\', '\\'), '''', '''''') || '''',
                       ' | ')
              FROM unnest(to_tsvector('english', %(query)s)))::tsquery AS terms
)
SELECT c.policy_id, c.title, c.text, c.text_sha256,
       1 - (c.embedding <=> q.v) AS similarity,
       CASE WHEN c.tsv @@ q.terms THEN ts_rank(c.tsv, q.terms, 1) END AS fts_rank
  FROM policy_chunks c, q
"""


def check_table(conn: Conn, source: PolicySource, model: str = EMBEDDING_MODEL) -> None:
    rows = conn.execute("SELECT policy_id, text_sha256, embedding_model FROM policy_chunks")
    found = rows.fetchall()
    if not found:
        raise PolicyTableError("policy_chunks is empty; run the seed")
    models = {m for _, _, m in found}
    if models != {model}:
        raise PolicyTableError(
            f"policy_chunks embedded with {sorted(models)}, configured {model!r}"
        )
    if {i: h for i, h, _ in found} != dict(source.hashes):
        raise PolicyTableError("policy_chunks does not match the packaged policies; run the seed")


def fetch_rows(conn: Conn, vector: Vector, query: str) -> list[Row]:
    found = conn.execute(SEARCH_SQL, {"vector": vector_literal(vector), "query": query})
    return [Row(*row) for row in found.fetchall()]


def search_policies(
    conn: Conn,
    llm: LLM,
    query: str,
    *,
    email_id: int | None,
    settings: SearchSettings,
    source: PolicySource | None = None,
) -> PolicyHits | NoMatch | InvalidQuery:
    """Steps 1-3 of the module docstring. `conn` must not be in a transaction (D71):
    nothing runs on it before the embedding call."""
    checked = check_query(query)
    if isinstance(checked, InvalidQuery):
        return checked
    source = packaged_source() if source is None else source
    embedded = llm.embed(
        [checked], model=EMBEDDING_MODEL, dimensions=EMBEDDING_DIMENSIONS, step=STEP,
        input_version=f"policies@{source.policies_sha256[:8]}", email_id=email_id,
    )  # fmt: skip
    with conn.transaction():
        check_table(conn, source)
        rows = fetch_rows(conn, embedded.vectors[0], checked)
    return decide(checked, rows, settings, source)
