"""anemos.policies.ranking against Postgres (the retrieval eval's comparison modes):
hybrid ranking, the threshold, the table check, full text on the packaged chunks.

Most tests load a small made-up corpus with hashing vectors inside the rolled-back `db`
transaction, so query and chunk vectors come from the same fake. The full-text tests
use the packaged chunks the seed loaded.
"""

from collections.abc import Sequence

import psycopg
import pytest
from psycopg.rows import TupleRow

from anemos.llm.fake import hashing_vector
from anemos.policies import PolicyChunk
from anemos.policies.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EmbeddingsFile,
    StoredVector,
    embedded_text,
    text_sha256,
)
from anemos.policies.ranking import (
    BelowThreshold,
    PolicySource,
    PolicyTableError,
    Ranked,
    SearchSettings,
    check_table,
    decide,
    fetch_rows,
    fts_ranking,
)
from anemos.policies.store import load_policy_chunks

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[TupleRow]

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


@pytest.fixture
def corpus(db: Conn) -> Conn:
    load_policy_chunks(db, CHUNKS, hashed_file(CHUNKS))
    return db


def search(conn: Conn, query: str, source: PolicySource = SOURCE) -> Ranked | BelowThreshold:
    check_table(conn, source)
    rows = fetch_rows(conn, hashing_vector(query, EMBEDDING_DIMENSIONS), query)
    return decide(query, rows, SETTINGS)


# --- ranking and the threshold, small corpus --------------------------------------------


def test_the_chunk_sharing_the_query_words_ranks_first(corpus: Conn) -> None:
    result = search(corpus, "When is breakfast served?")
    assert isinstance(result, Ranked)
    assert result.hits[0].policy_id == "breakfast"
    assert len(result.hits) == 3


def test_a_full_text_match_lifts_a_chunk_in_the_fused_order(corpus: Conn) -> None:
    """'terrace' is in breakfast and wifi: both are on the full-text list, parking and
    pets are not, so both rank above them."""
    result = search(corpus, "terrace")
    assert isinstance(result, Ranked)
    assert {h.policy_id for h in result.hits[:2]} == {"breakfast", "wifi"}
    assert all(h.fts_rank is not None for h in result.hits[:2])


def test_a_question_no_chunk_covers_is_below_the_threshold(corpus: Conn) -> None:
    result = search(corpus, "Is there a swimming pool or a gym?")
    assert isinstance(result, BelowThreshold)
    assert result.best_similarity < SETTINGS.threshold


def test_the_same_query_gives_the_same_result(corpus: Conn) -> None:
    assert search(corpus, "free terrace") == search(corpus, "free terrace")


def test_hits_carry_the_text_from_the_table(corpus: Conn) -> None:
    result = search(corpus, "Are dogs allowed?")
    assert isinstance(result, Ranked)
    top = result.hits[0]
    assert (top.policy_id, top.text, top.text_sha256) == (
        "pets", CHUNKS[2].text, text_sha256(CHUNKS[2]))  # fmt: skip


def test_query_text_cannot_break_the_full_text_syntax(corpus: Conn) -> None:
    """Quotes, backslashes and tsquery operators in a query are only words."""
    search(corpus, r"it's a back\slash 'quote' & | ! : <-> (x) dogs:*")


# --- a table that does not fit -------------------------------------------------------


def test_chunks_from_another_model_are_refused(corpus: Conn) -> None:
    corpus.execute("UPDATE policy_chunks SET embedding_model = 'text-embedding-3-large'"
                   " WHERE policy_id = 'wifi'")  # fmt: skip
    with pytest.raises(PolicyTableError, match="configured 'text-embedding-3-small'"):
        search(corpus, "wifi")


def test_an_empty_table_is_refused(corpus: Conn) -> None:
    corpus.execute("DELETE FROM policy_chunks")
    with pytest.raises(PolicyTableError, match="empty"):
        search(corpus, "wifi")


def test_a_table_not_loaded_from_these_policies_is_refused(corpus: Conn) -> None:
    changed = (*CHUNKS[:3], PolicyChunk("wifi", "Wi-Fi", "Wi-Fi in the lobby only."))
    with pytest.raises(PolicyTableError, match="packaged policies"):
        search(corpus, "wifi", source=PolicySource.of(changed, "e" * 64))


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
