"""Vector, full-text and hybrid ranking over `policy_chunks`: the retrieval eval's
comparison modes (D15). **Eval only:** production code never imports this module
(`search_policies` is a model selector, anemos.policies.search; checked by
tests/unit/test_import_direction.py).

The caller embeds the query (the eval embeds all its queries in one call), then one
`fetch_rows` returns each chunk's cosine similarity and full-text rank:

- vector: every chunk, by cosine similarity (exact search, no index);
- full text: chunks matching at least one query lexeme, by ts_rank with length
  normalisation. The lexemes are ORed; ts_rank has no IDF, so a word common to many
  chunks weighs as much as a rare one (docs/NOTES.md: why hybrid lost on dev);
- hybrid: RRF (reciprocal rank fusion: each list adds 1/(k + rank) for a chunk).

Every ordering breaks ties by `policy_id`, so a query always gives the same result.
`decide` answers `BelowThreshold` when the best cosine similarity is below the
threshold (the vector side alone decides it).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache

import psycopg
from psycopg.rows import TupleRow

from anemos.llm.adapter import Vector
from anemos.policies import PolicyChunk, load_policies
from anemos.policies.embeddings import EMBEDDING_MODEL, policies_sha256, text_sha256
from anemos.policies.store import vector_literal

type Conn = psycopg.Connection[TupleRow]


class PolicyTableError(RuntimeError):
    """`policy_chunks` does not fit the packaged policies or the configured model."""


@dataclass(frozen=True)
class SearchSettings:
    """`threshold` has no default: the eval's dev rule chooses it (docs/NOTES.md)."""

    threshold: float
    top_k: int = 3
    rrf_k: int = 60


@dataclass(frozen=True)
class PolicySource:
    """The policy text the table must hold: chunk id → text hash, and the hash of the
    policies.md they came from."""

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
class Row:
    """One chunk as the ranking query returns it."""

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
class RankedHit:
    policy_id: str
    title: str
    text: str
    text_sha256: str
    similarity: float
    fts_rank: float | None
    rrf: float


@dataclass(frozen=True)
class Ranked:
    query: str
    hits: tuple[RankedHit, ...]


@dataclass(frozen=True)
class BelowThreshold:
    query: str
    best_similarity: float


def decide(query: str, rows: Sequence[Row], settings: SearchSettings) -> Ranked | BelowThreshold:
    """BelowThreshold when the best similarity is below the threshold (vector side
    only), else the top_k hybrid hits."""
    vector = vector_ranking(rows)
    by_id = {r.policy_id: r for r in rows}
    best = by_id[vector[0]].similarity
    if best < settings.threshold:
        return BelowThreshold(query, best)
    fused = rrf([vector, fts_ranking(rows)], settings.rrf_k)[: settings.top_k]
    hits = tuple(
        RankedHit(r.policy_id, r.title, r.text, r.text_sha256, r.similarity, r.fts_rank, score)
        for policy_id, score in fused
        for r in (by_id[policy_id],)
    )
    return Ranked(query, hits)


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
    """The table must hold exactly the packaged chunks, embedded with `model`."""
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
