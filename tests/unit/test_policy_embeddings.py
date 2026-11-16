"""The committed policy embeddings and the build step that refreshes them (D15).

No network: the packaged embeddings.json is read as it is, and `refresh` runs with the
deterministic HashingEmbedClient behind the real adapter.
"""

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from anemos.clock import FixedClock
from anemos.llm.adapter import LLM, Settings
from anemos.llm.budget import Budget
from anemos.llm.fake import HashingEmbedClient, ScriptedClient
from anemos.policies import PolicyChunk, load_policies
from anemos.policies.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EmbeddingsError,
    EmbeddingsFile,
    StoredVector,
    checked_vectors,
    decode_vector,
    encode_vector,
    read_embeddings,
    refresh,
    stale,
    text_sha256,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def test_the_packaged_embeddings_fit_every_chunk() -> None:
    """Fails when a chunk changed without `python -m anemos.policies embed`."""
    embeddings = read_embeddings()
    vectors = checked_vectors(embeddings, load_policies())
    assert set(vectors) == {c.policy_id for c in load_policies()}
    assert (embeddings.model, embeddings.dimensions) == (EMBEDDING_MODEL, EMBEDDING_DIMENSIONS)


def test_the_packaged_embeddings_record_how_they_were_made() -> None:
    generated = read_embeddings().generated
    assert generated is not None
    assert re.fullmatch(r"[0-9a-f]{40}", generated.commit)
    assert isinstance(generated.dirty, bool)
    assert datetime.fromisoformat(generated.at).tzinfo is not None
    assert generated.response_model.startswith(EMBEDDING_MODEL)


def test_a_vector_survives_the_float32_round_trip() -> None:
    vector = (0.5, -0.25, 1.0, 0.125)  # exact in float32
    assert decode_vector(encode_vector(vector)) == vector


CHUNKS = (PolicyChunk("wifi", "Wi-Fi", "Free everywhere."),
          PolicyChunk("parking", "Parking", "Public parking nearby."))  # fmt: skip


def file_for(chunks: tuple[PolicyChunk, ...], model: str = EMBEDDING_MODEL) -> EmbeddingsFile:
    vector = (0.0,) * EMBEDDING_DIMENSIONS
    stored = {c.policy_id: StoredVector(text_sha256(c), vector) for c in chunks}
    return EmbeddingsFile(model, EMBEDDING_DIMENSIONS, stored)


def test_a_changed_chunk_is_stale_and_refused() -> None:
    embeddings = file_for(CHUNKS)
    changed = (replace(CHUNKS[0], text="Free in the rooms only."), CHUNKS[1])
    assert stale(embeddings, changed) == ["wifi"]
    with pytest.raises(EmbeddingsError, match="stale"):
        checked_vectors(embeddings, changed)


def test_a_title_change_is_stale_too() -> None:
    """The title is part of what is embedded."""
    renamed = (replace(CHUNKS[0], title="Internet"), CHUNKS[1])
    assert stale(file_for(CHUNKS), renamed) == ["wifi"]


def test_embeddings_from_another_model_are_refused() -> None:
    with pytest.raises(EmbeddingsError, match="text-embedding-3-large"):
        checked_vectors(file_for(CHUNKS, model="text-embedding-3-large"), CHUNKS)


def test_a_vector_for_a_removed_chunk_is_refused() -> None:
    with pytest.raises(EmbeddingsError, match="no longer exist"):
        checked_vectors(file_for(CHUNKS), CHUNKS[:1])


# --- refresh (the build step), with the fake embedder ----------------------------------


@pytest.fixture
def embedder() -> HashingEmbedClient:
    return HashingEmbedClient(dimensions=EMBEDDING_DIMENSIONS)


def llm_with(embedder: HashingEmbedClient, tmp_path: Path) -> LLM:
    clock = FixedClock(NOW)
    return LLM(ScriptedClient(), Settings(EMBEDDING_MODEL, max_output_tokens=0),
               Budget(clock, 1_000_000, tmp_path / "ledger.jsonl"), None, clock,
               embed_client=embedder)  # fmt: skip


def run(embedder: HashingEmbedClient, tmp_path: Path, current: EmbeddingsFile,
        chunks: tuple[PolicyChunk, ...]) -> EmbeddingsFile:  # fmt: skip
    return refresh(llm_with(embedder, tmp_path), current, commit="a" * 40, dirty=True, at=NOW,
                   chunks=chunks)  # fmt: skip


def test_refresh_embeds_only_new_or_changed_chunks(
    embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    current = file_for(CHUNKS[:1])  # parking has no vector yet
    updated = run(embedder, tmp_path, current, CHUNKS)
    assert embedder.calls == [("Parking\n\nPublic parking nearby.",)]
    assert updated.chunks["wifi"] == current.chunks["wifi"]
    checked_vectors(updated, CHUNKS)  # every chunk fits now
    assert updated.generated is not None
    assert (updated.generated.commit, updated.generated.dirty, updated.generated.embedded) == (
        "a" * 40,
        True,
        1,
    )


def test_refresh_with_nothing_stale_makes_no_call(
    embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    current = file_for(CHUNKS)
    assert run(embedder, tmp_path, current, CHUNKS) == current
    assert embedder.calls == []


def test_refresh_drops_vectors_of_removed_chunks(
    embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    updated = run(embedder, tmp_path, file_for(CHUNKS), CHUNKS[:1])
    assert set(updated.chunks) == {"wifi"} and embedder.calls == []


def test_refresh_embeds_everything_again_after_a_model_change(
    embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    updated = run(embedder, tmp_path, file_for(CHUNKS, model="text-embedding-ada-002"), CHUNKS)
    assert updated.model == EMBEDDING_MODEL and len(embedder.calls[0]) == 2


def test_the_file_round_trips_through_json(embedder: HashingEmbedClient, tmp_path: Path) -> None:
    updated = run(embedder, tmp_path, EmbeddingsFile(EMBEDDING_MODEL, EMBEDDING_DIMENSIONS), CHUNKS)
    again = EmbeddingsFile.from_json(updated.to_json())
    assert again.generated == updated.generated and set(again.chunks) == set(updated.chunks)
    for policy_id, stored in updated.chunks.items():
        assert again.chunks[policy_id].sha256 == stored.sha256
        # float32 in the file: equal to float32 precision
        assert max(abs(a - b) for a, b in zip(again.chunks[policy_id].vector, stored.vector,
                                              strict=True)) < 1e-6  # fmt: skip
