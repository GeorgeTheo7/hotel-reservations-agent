"""Load the policies into `policy_chunks` (D15): replaced as a whole, in one transaction.

Reads only packaged files (policies.md, embeddings.json): no network, so tests, CI and
the seed can all call it. Refuses embeddings that do not fit the chunks or the
configured model (embeddings.checked_vectors) and writes nothing then.
"""

from collections.abc import Sequence

import psycopg
from psycopg.rows import TupleRow

from anemos.llm.adapter import Vector
from anemos.policies import PolicyChunk, load_policies
from anemos.policies.embeddings import (
    EMBEDDING_MODEL,
    EmbeddingsFile,
    checked_vectors,
    read_embeddings,
    text_sha256,
)

type Conn = psycopg.Connection[TupleRow]


def vector_literal(vector: Vector) -> str:
    """pgvector's text form, '[0.1,0.2,...]': cast with ::vector in SQL, so no client
    library is needed (SPEC Open items). repr() keeps every digit of each float."""
    return "[" + ",".join(repr(x) for x in vector) + "]"


def load_policy_chunks(
    conn: Conn,
    chunks: Sequence[PolicyChunk] | None = None,
    embeddings: EmbeddingsFile | None = None,
    model: str = EMBEDDING_MODEL,
) -> int:
    """Replace every row with the given (default: packaged) chunks and their vectors.
    Inside the caller's transaction, as a savepoint if one is open (like `seed`).
    Returns the number of rows written."""
    chunks = load_policies() if chunks is None else chunks
    embeddings = read_embeddings() if embeddings is None else embeddings
    vectors = checked_vectors(embeddings, chunks, model)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM policy_chunks")
        cur.executemany(
            "INSERT INTO policy_chunks"
            " (policy_id, title, text, text_sha256, embedding, embedding_model)"
            " VALUES (%s, %s, %s, %s, %s::vector, %s)",
            [
                (c.policy_id, c.title, c.text, text_sha256(c),
                 vector_literal(vectors[c.policy_id]), embeddings.model)
                for c in chunks
            ],
        )  # fmt: skip
    return len(chunks)
