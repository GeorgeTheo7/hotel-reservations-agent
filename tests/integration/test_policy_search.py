"""search_policies against Postgres: hybrid ranking, NO_MATCH, the table check, and no
transaction open during the embedding call (D65, D71).

Most tests load a small made-up corpus with HashingEmbedClient vectors inside the
rolled-back `db` transaction, so query and chunk vectors come from the same fake. The
full-text tests use the packaged chunks the seed loaded. The transaction and audit tests
need committed connections (committed_db), where the packaged chunks are seed data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow

from anemos.clock import FixedClock
from anemos.llm.adapter import LLM, EmbedReply, LLMError, PermanentError, Settings
from anemos.llm.budget import Budget
from anemos.llm.fake import HashingEmbedClient, ScriptedClient, hashing_vector
from anemos.policies import PolicyChunk
from anemos.policies.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EmbeddingsFile,
    StoredVector,
    embedded_text,
    text_sha256,
)
from anemos.policies.search import (
    NoMatch,
    PolicyHits,
    PolicySource,
    PolicyTableError,
    SearchSettings,
    fetch_rows,
    fts_ranking,
    search_policies,
)
from anemos.policies.store import load_policy_chunks

if TYPE_CHECKING:  # tests/ is not a package: the fixture's class is for mypy only
    from conftest import CommittedDb

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
SETTINGS = SearchSettings(threshold=0.2)  # for the hashing fake: one shared word ~0.25, none ~0.1

CHUNKS = (
    PolicyChunk("breakfast", "Breakfast", "Breakfast is served on the terrace, 08:00 to 10:30."),
    PolicyChunk("parking", "Parking", "Free public parking is 100 m from the hotel."),
    PolicyChunk("pets", "Pets", "Pets are not allowed. Assistance dogs are welcome."),
    PolicyChunk("wifi", "Wi-Fi", "Free Wi-Fi in every room and on the terrace."),
)
SOURCE = PolicySource.of(CHUNKS, "e" * 64)


def hashed_file(chunks: Sequence[PolicyChunk]) -> EmbeddingsFile:
    stored = {c.policy_id: StoredVector(text_sha256(c),
                                        hashing_vector(embedded_text(c), EMBEDDING_DIMENSIONS))
              for c in chunks}  # fmt: skip
    return EmbeddingsFile(EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, stored)


def make_llm(embedder: object, tmp_path: Path, connect: object = None) -> tuple[LLM, Budget]:
    clock = FixedClock(NOW)
    budget = Budget(clock, 1_000_000, tmp_path / "ledger.jsonl")
    llm = LLM(ScriptedClient(), Settings(EMBEDDING_MODEL, max_output_tokens=0), budget,
              connect, clock, sleep=lambda s: None, embed_client=embedder)  # type: ignore[arg-type]  # fmt: skip
    return llm, budget


@pytest.fixture
def corpus(db: Conn) -> Conn:
    load_policy_chunks(db, CHUNKS, hashed_file(CHUNKS))
    return db


@pytest.fixture
def embedder() -> HashingEmbedClient:
    return HashingEmbedClient(dimensions=EMBEDDING_DIMENSIONS)


def search(conn: Conn, embedder: HashingEmbedClient, tmp_path: Path, query: str,
           source: PolicySource = SOURCE) -> PolicyHits | NoMatch:  # fmt: skip
    llm, _ = make_llm(embedder, tmp_path)
    result = search_policies(conn, llm, query, email_id=None, settings=SETTINGS, source=source)
    assert isinstance(result, PolicyHits | NoMatch)
    return result


# --- ranking and NO_MATCH, small corpus -------------------------------------------------


def test_the_chunk_sharing_the_query_words_ranks_first(
    corpus: Conn, embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    result = search(corpus, embedder, tmp_path, "When is breakfast served?")
    assert isinstance(result, PolicyHits)
    assert result.hits[0].policy_id == "breakfast"
    assert len(result.hits) == 3


def test_a_full_text_match_lifts_a_chunk_in_the_fused_order(
    corpus: Conn, embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    """'terrace' is in breakfast and wifi: both are on the full-text list, parking and
    pets are not, so both rank above them."""
    result = search(corpus, embedder, tmp_path, "terrace")
    assert isinstance(result, PolicyHits)
    assert {h.policy_id for h in result.hits[:2]} == {"breakfast", "wifi"}
    assert all(h.fts_rank is not None for h in result.hits[:2])


def test_a_question_no_chunk_covers_is_no_match(
    corpus: Conn, embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    result = search(corpus, embedder, tmp_path, "Is there a swimming pool or a gym?")
    assert isinstance(result, NoMatch)
    assert result.best_similarity < SETTINGS.threshold


def test_the_same_query_gives_the_same_result(
    corpus: Conn, embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    first = search(corpus, embedder, tmp_path, "free terrace")
    assert search(corpus, embedder, tmp_path, "free terrace").record() == first.record()


def test_the_record_holds_the_text_returned_from_the_table(
    corpus: Conn, embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    result = search(corpus, embedder, tmp_path, "Are dogs allowed?")
    assert isinstance(result, PolicyHits)
    top = result.record()["hits"][0]
    assert (top["policy_id"], top["text"], top["text_sha256"]) == (
        "pets", CHUNKS[2].text, text_sha256(CHUNKS[2]))  # fmt: skip
    assert result.record()["policies_sha256"] == "e" * 64


def test_query_text_cannot_break_the_full_text_syntax(
    corpus: Conn, embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    """Quotes, backslashes and tsquery operators in the model's query are only words."""
    search(corpus, embedder, tmp_path, r"it's a back\slash 'quote' & | ! : <-> (x) dogs:*")


# --- a failed embedding and a table that does not fit: errors, never NO_MATCH ----------


def test_a_failed_query_embedding_raises_never_no_match(corpus: Conn, tmp_path: Path) -> None:
    failing = HashingEmbedClient(EMBEDDING_DIMENSIONS, fail_with=[PermanentError("HTTP 400")])
    with pytest.raises(LLMError, match="search_policies: PermanentError: HTTP 400"):
        search(corpus, failing, tmp_path, "Are dogs allowed?")


def test_chunks_from_another_model_are_refused(
    corpus: Conn, embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    corpus.execute("UPDATE policy_chunks SET embedding_model = 'text-embedding-3-large'"
                   " WHERE policy_id = 'wifi'")  # fmt: skip
    with pytest.raises(PolicyTableError, match="configured 'text-embedding-3-small'"):
        search(corpus, embedder, tmp_path, "wifi")


def test_an_empty_table_is_refused(
    corpus: Conn, embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    corpus.execute("DELETE FROM policy_chunks")
    with pytest.raises(PolicyTableError, match="empty"):
        search(corpus, embedder, tmp_path, "wifi")


def test_a_table_not_loaded_from_these_policies_is_refused(
    corpus: Conn, embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    changed = (*CHUNKS[:3], PolicyChunk("wifi", "Wi-Fi", "Wi-Fi in the lobby only."))
    with pytest.raises(PolicyTableError, match="packaged policies"):
        search(corpus, embedder, tmp_path, "wifi", source=PolicySource.of(changed, "e" * 64))


# --- full text on the packaged chunks ------------------------------------------------


def fts(db: Conn, query: str) -> list[str]:
    return fts_ranking(fetch_rows(db, hashing_vector(query, EMBEDDING_DIMENSIONS), query))


def test_or_of_the_words_finds_the_chunk_of_the_rare_word(db: Conn) -> None:
    """'dog' is in one chunk, 'room' in many; with OR, the chunk with the rare word still
    ranks first (narrowly: ts_rank has no IDF, see NOTES)."""
    assert fts(db, "dog") == ["pets-assistance-dogs"]
    assert fts(db, "Can I bring my dog to my room?")[0] == "pets-assistance-dogs"
    assert fts(db, "Is breakfast served in the room?")[0] == "breakfast"


def test_a_query_of_stop_words_has_no_full_text_list(db: Conn) -> None:
    assert fts(db, "the and of") == []


# --- no transaction during the embedding call; audit with the run's email ----------------


@dataclass
class WatchingEmbedder:
    """Records the connection's transaction status at the moment of the call."""

    conn: Conn
    seen: list[TransactionStatus] = field(default_factory=list)

    def embed(self, texts: Sequence[str], model: str, timeout_s: float) -> EmbedReply:
        self.seen.append(self.conn.info.transaction_status)
        return HashingEmbedClient(EMBEDDING_DIMENSIONS).embed(texts, model, timeout_s)


def test_no_transaction_is_open_during_the_embedding_call(
    committed_db: CommittedDb, tmp_path: Path
) -> None:
    conn = committed_db.connect()
    watcher = WatchingEmbedder(conn)
    llm, _ = make_llm(watcher, tmp_path)
    result = search_policies(conn, llm, "Are dogs allowed?", email_id=None,
                             settings=SearchSettings(threshold=-1.0))  # fmt: skip
    assert isinstance(result, PolicyHits)
    assert watcher.seen == [TransactionStatus.IDLE]
    assert conn.info.transaction_status is TransactionStatus.IDLE  # the read committed


def test_the_query_embedding_is_audited_and_charged_to_the_run(
    committed_db: CommittedDb, embedder: HashingEmbedClient, tmp_path: Path
) -> None:
    conn = committed_db.connect()
    email_id = committed_db.rows(conn).email()
    conn.commit()
    llm, budget = make_llm(embedder, tmp_path, connect=committed_db.connect)
    search_policies(conn, llm, "Are dogs allowed?", email_id=email_id,
                    settings=SearchSettings(threshold=-1.0))  # fmt: skip
    rows = conn.execute(
        "SELECT event, email_id, step, model, cost_micro_usd FROM audit_log ORDER BY id"
    ).fetchall()
    conn.commit()
    assert rows == [("llm.called", email_id, "search_policies", EMBEDDING_MODEL,
                     budget.spent())]  # fmt: skip
    assert budget.spent() > 0
