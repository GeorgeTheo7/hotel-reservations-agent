"""Policy embeddings (D15): the committed file, and the build step that refreshes it.

`embeddings.json` sits next to policies.md and holds one vector per chunk, with the
SHA-256 of the exact text embedded (title + text). That hash is the real link between
a vector and its chunk: a chunk whose text changed has a stale vector, and loading
refuses it. The file also records how it was made (model, commit, dirty flag, time),
as every model call outside an evaluation must (CLAUDE.md).

Vectors are stored as base64 of little-endian float32: OpenAI's vectors have float32
precision, so nothing is lost, and the file is about half the size of JSON numbers.

Only `refresh` calls a model, through the LLM adapter (budget, retry, audit), and only
for chunks that are new or changed. Tests never call it with a real client.
"""

import base64
import hashlib
import json
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from anemos.llm.adapter import LLM, Vector
from anemos.policies import PolicyChunk, load_policies

EMBEDDING_MODEL = "text-embedding-3-small"  # D2; priced in llm/pricing.py
EMBEDDING_DIMENSIONS = 1536  # the model's default; policy_chunks.embedding is vector(1536)
ENCODING = "base64-float32-le"
STEP = "policy_embeddings"

EMBEDDINGS_FILE = Path(str(files("anemos.policies").joinpath("embeddings.json")))
POLICIES_FILE = Path(str(files("anemos.policies").joinpath("policies.md")))


class EmbeddingsError(ValueError):
    """The embeddings file does not fit the policies or the configured model."""


def embedded_text(chunk: PolicyChunk) -> str:
    """What is embedded for a chunk: its title, a blank line, its text."""
    return f"{chunk.title}\n\n{chunk.text}"


def text_sha256(chunk: PolicyChunk) -> str:
    return hashlib.sha256(embedded_text(chunk).encode("utf-8")).hexdigest()


def policies_sha256() -> str:
    """Hash of policies.md as packaged: names the policy text a run or eval used."""
    return hashlib.sha256(POLICIES_FILE.read_bytes()).hexdigest()


def encode_vector(vector: Sequence[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def decode_vector(encoded: str) -> Vector:
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) % 4:
        raise EmbeddingsError("a vector is not a whole number of float32 values")
    return struct.unpack(f"<{len(raw) // 4}f", raw)


@dataclass(frozen=True)
class StoredVector:
    sha256: str
    vector: Vector


@dataclass(frozen=True)
class Generated:
    """How the file was last written (CLAUDE.md: model, commit, dirty flag, time)."""

    commit: str
    dirty: bool
    at: str  # ISO time, injected clock
    response_model: str
    embedded: int  # chunks embedded in that run
    cost_micro_usd: int


@dataclass(frozen=True)
class EmbeddingsFile:
    model: str
    dimensions: int
    chunks: dict[str, StoredVector] = field(default_factory=dict)
    generated: Generated | None = None

    def to_json(self) -> str:
        data = {
            "model": self.model,
            "dimensions": self.dimensions,
            "encoding": ENCODING,
            "generated": None if self.generated is None else vars(self.generated),
            "chunks": {
                policy_id: {"sha256": v.sha256, "vector": encode_vector(v.vector)}
                for policy_id, v in sorted(self.chunks.items())
            },
        }
        return json.dumps(data, indent=1, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "EmbeddingsFile":
        data = json.loads(text)
        if data.get("encoding") != ENCODING:
            raise EmbeddingsError(f"unknown encoding {data.get('encoding')!r}")
        chunks = {
            policy_id: StoredVector(entry["sha256"], decode_vector(entry["vector"]))
            for policy_id, entry in data["chunks"].items()
        }
        generated = None if data["generated"] is None else Generated(**data["generated"])
        return cls(data["model"], data["dimensions"], chunks, generated)


def read_embeddings(path: Path = EMBEDDINGS_FILE) -> EmbeddingsFile:
    if not path.exists():
        return EmbeddingsFile(EMBEDDING_MODEL, EMBEDDING_DIMENSIONS)
    return EmbeddingsFile.from_json(path.read_text(encoding="utf-8"))


def stale(embeddings: EmbeddingsFile, chunks: Sequence[PolicyChunk]) -> list[str]:
    """Chunks with no vector, or whose text changed since it was embedded."""
    return [
        c.policy_id
        for c in chunks
        if (stored := embeddings.chunks.get(c.policy_id)) is None or stored.sha256 != text_sha256(c)
    ]


def checked_vectors(
    embeddings: EmbeddingsFile, chunks: Sequence[PolicyChunk], model: str = EMBEDDING_MODEL
) -> dict[str, Vector]:
    """The vector of every chunk, or EmbeddingsError: another model, wrong dimensions,
    a stale or missing vector, or a vector for a chunk that no longer exists."""
    if embeddings.model != model:
        raise EmbeddingsError(f"embeddings made with {embeddings.model!r}, configured {model!r}")
    if embeddings.dimensions != EMBEDDING_DIMENSIONS:
        raise EmbeddingsError(
            f"{embeddings.dimensions} dimensions, expected {EMBEDDING_DIMENSIONS}"
        )
    if old := stale(embeddings, chunks):
        raise EmbeddingsError(
            f"stale or missing vectors: {old}; run `python -m anemos.policies embed`"
        )
    extra = set(embeddings.chunks) - {c.policy_id for c in chunks}
    if extra:
        raise EmbeddingsError(f"vectors for chunks that no longer exist: {sorted(extra)}")
    vectors = {c.policy_id: embeddings.chunks[c.policy_id].vector for c in chunks}
    wrong = [i for i, v in vectors.items() if len(v) != EMBEDDING_DIMENSIONS]
    if wrong:
        raise EmbeddingsError(f"vectors of the wrong size: {wrong}")
    return vectors


def refresh(
    llm: LLM,
    embeddings: EmbeddingsFile,
    *,
    commit: str,
    dirty: bool,
    at: datetime,
    chunks: Sequence[PolicyChunk] | None = None,
) -> EmbeddingsFile:
    """Embed the stale chunks (one call for all of them) and return the new file
    contents; vectors of removed chunks are dropped. Nothing stale: no call, the
    contents unchanged (except removed chunks)."""
    chunks = load_policies() if chunks is None else chunks
    if embeddings.model != EMBEDDING_MODEL:
        embeddings = EmbeddingsFile(EMBEDDING_MODEL, EMBEDDING_DIMENSIONS)  # re-embed all
    current = {c.policy_id for c in chunks}
    kept = {i: v for i, v in embeddings.chunks.items() if i in current}
    todo = [c for c in chunks if c.policy_id in stale(embeddings, chunks)]
    if not todo:
        return EmbeddingsFile(embeddings.model, embeddings.dimensions, kept, embeddings.generated)
    result = llm.embed(
        [embedded_text(c) for c in todo], model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS, step=STEP,
        input_version=f"policies@{policies_sha256()[:8]}",
    )  # fmt: skip
    for chunk, vector in zip(todo, result.vectors, strict=True):
        kept[chunk.policy_id] = StoredVector(text_sha256(chunk), vector)
    generated = Generated(commit, dirty, at.isoformat(), result.response_model, len(todo),
                          result.cost_micro_usd)  # fmt: skip
    return EmbeddingsFile(EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, kept, generated)
