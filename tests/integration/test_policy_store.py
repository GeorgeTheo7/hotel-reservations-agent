"""policy_chunks: loaded by the seed, replaced as a whole, refused when it does not fit.

The test databases are seeded, so the packaged policies are already there; tests that
replace them run inside the rolled-back `db` transaction.
"""

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.policies import PolicyChunk, load_policies
from anemos.policies.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EmbeddingsError,
    EmbeddingsFile,
    StoredVector,
    read_embeddings,
    text_sha256,
)
from anemos.policies.store import load_policy_chunks

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

CHUNKS = (PolicyChunk("wifi", "Wi-Fi", "Free everywhere."),
          PolicyChunk("parking", "Parking", "Public parking nearby."))  # fmt: skip


def file_for(chunks: tuple[PolicyChunk, ...], model: str = EMBEDDING_MODEL) -> EmbeddingsFile:
    vector = (0.5,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)
    stored = {c.policy_id: StoredVector(text_sha256(c), vector) for c in chunks}
    return EmbeddingsFile(model, EMBEDDING_DIMENSIONS, stored)


def ids(db: Conn) -> set[str]:
    return {row[0] for row in db.execute("SELECT policy_id FROM policy_chunks").fetchall()}


def test_the_seed_loads_every_packaged_chunk(db: Conn) -> None:
    rows = db.execute(
        "SELECT policy_id, text_sha256, embedding_model, vector_dims(embedding),"
        " length(tsv::text) > 0 FROM policy_chunks"
    ).fetchall()
    expected = {c.policy_id: text_sha256(c) for c in load_policies()}
    assert {r[0]: r[1] for r in rows} == expected
    assert {(r[2], r[3], r[4]) for r in rows} == {(EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, True)}


def test_stored_vectors_are_the_packaged_ones(db: Conn) -> None:
    packaged = read_embeddings().chunks["breakfast"].vector
    row = db.execute(
        "SELECT embedding::text FROM policy_chunks WHERE policy_id = 'breakfast'"
    ).fetchone()
    assert row is not None
    stored = [float(x) for x in row[0].strip("[]").split(",")]
    assert len(stored) == len(packaged)
    assert max(abs(a - b) for a, b in zip(stored, packaged, strict=True)) < 1e-6


def test_full_text_finds_a_chunk_by_a_word_in_its_text(db: Conn) -> None:
    rows = db.execute(
        "SELECT policy_id FROM policy_chunks WHERE tsv @@ plainto_tsquery('english', 'served')"
    ).fetchall()
    assert [r[0] for r in rows] == ["breakfast"]


def test_loading_replaces_every_row(db: Conn) -> None:
    assert load_policy_chunks(db, CHUNKS, file_for(CHUNKS)) == 2
    assert ids(db) == {"wifi", "parking"}


def test_a_stale_vector_is_refused_and_nothing_is_written(db: Conn) -> None:
    before = ids(db)
    changed = (PolicyChunk("wifi", "Wi-Fi", "Free in the rooms only."), CHUNKS[1])
    with pytest.raises(EmbeddingsError, match="stale"):
        load_policy_chunks(db, changed, file_for(CHUNKS))
    assert ids(db) == before


def test_a_file_from_another_model_than_configured_is_refused(db: Conn) -> None:
    """Vectors from two models are not comparable (D15): a file made with another model
    than the configured one is never loaded, even if every hash fits."""
    before = ids(db)
    with pytest.raises(EmbeddingsError, match="configured"):
        load_policy_chunks(db, CHUNKS, file_for(CHUNKS, model="text-embedding-3-large"))
    assert ids(db) == before


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("policy_id", "Wi_Fi", "policy_chunks_policy_id_slug"),
        ("policy_id", "", "policy_chunks_policy_id_slug"),
        ("title", " \n", "policy_chunks_title_not_blank"),
        ("text", "\t", "policy_chunks_text_not_blank"),
        ("embedding_model", " ", "policy_chunks_embedding_model_not_blank"),
        ("text_sha256", "abc", "policy_chunks_text_sha256_hex"),
    ],
)
def test_constraints_refuse_bad_rows(db: Conn, column: str, value: str, constraint: str) -> None:
    row = {"policy_id": "x", "title": "T", "text": "Text.", "text_sha256": "0" * 64,
           "embedding_model": EMBEDDING_MODEL, column: value}  # fmt: skip
    with pytest.raises(psycopg.errors.CheckViolation, match=constraint), db.transaction():
        db.execute(
            "INSERT INTO policy_chunks (policy_id, title, text, text_sha256, embedding,"
            " embedding_model) VALUES (%(policy_id)s, %(title)s, %(text)s, %(text_sha256)s,"
            " array_fill(0, ARRAY[1536])::vector, %(embedding_model)s)", row,
        )  # fmt: skip


@pytest.mark.parametrize("column", ["title", "text", "text_sha256", "embedding", "embedding_model"])
def test_every_column_is_not_null(db: Conn, column: str) -> None:
    row = db.execute(
        "SELECT is_nullable FROM information_schema.columns"
        " WHERE table_name = 'policy_chunks' AND column_name = %s", (column,),
    ).fetchone()  # fmt: skip
    assert row == ("NO",)


def test_a_vector_of_another_size_is_refused(db: Conn) -> None:
    with pytest.raises(psycopg.errors.DataException), db.transaction():
        db.execute(
            "INSERT INTO policy_chunks (policy_id, title, text, text_sha256, embedding,"
            " embedding_model) VALUES ('x', 'T', 'Text.', %s, array_fill(0, ARRAY[3])::vector,"
            " %s)", ("0" * 64, EMBEDDING_MODEL),
        )  # fmt: skip
