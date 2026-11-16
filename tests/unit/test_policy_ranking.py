"""search_policies without a database: query checks, RRF, the NO_MATCH decision, records.

The SQL side (full text, the table check, the transaction order) is in
tests/integration/test_policy_search.py.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from anemos.clock import FixedClock
from anemos.llm.adapter import LLM, Settings
from anemos.llm.budget import Budget
from anemos.llm.fake import HashingEmbedClient, ScriptedClient
from anemos.policies import load_policies
from anemos.policies.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from anemos.policies.search import (
    FIX_HINT,
    InvalidQuery,
    NoMatch,
    PolicyHits,
    PolicySource,
    Row,
    SearchSettings,
    check_query,
    decide,
    fts_ranking,
    packaged_source,
    rrf,
    search_policies,
    vector_ranking,
)

pytestmark = pytest.mark.unit

# --- the query check ----------------------------------------------------------------


def test_the_fix_hint_tells_the_model_what_to_do() -> None:
    assert FIX_HINT == "write the query in English, up to 300 characters"


@pytest.mark.parametrize(
    ("query", "problem"),
    [
        ("", "the query is blank"),
        (" \n\t", "the query is blank"),
        ("a" * 301, "the query has 301 characters"),
        ("κατοικίδια επιτρέπονται;", "the query contains Greek letters"),
        ("pets allowed? σκύλος", "the query contains Greek letters"),
        ("ἀγαθός", "the query contains Greek letters"),  # Greek Extended (polytonic)
    ],
    ids=["empty", "whitespace", "301-chars", "greek", "mixed", "polytonic"],
)
def test_a_bad_query_is_refused_with_how_to_fix_it(query: str, problem: str) -> None:
    result = check_query(query)
    assert isinstance(result, InvalidQuery)
    assert result.message == f"{problem}: write the query in English, up to 300 characters"
    assert result.record() == {"query": query, "invalid_query": result.message}


@pytest.mark.parametrize(
    "query",
    ["Are pets allowed?", "a" * 300, "  café near the port  ", "Ti ora einai to proino?"],
    ids=["english", "300-chars", "latin-accents", "greeklish"],
)
def test_an_acceptable_query_is_kept_stripped(query: str) -> None:
    assert check_query(query) == query.strip()


def test_an_invalid_query_makes_no_call_and_touches_no_database(tmp_path: Path) -> None:
    embedder = HashingEmbedClient(dimensions=EMBEDDING_DIMENSIONS)
    clock = FixedClock(datetime(2026, 11, 16, 9, 0, tzinfo=UTC))
    budget = Budget(clock, 1_000_000, tmp_path / "ledger.jsonl")
    llm = LLM(ScriptedClient(), Settings(EMBEDDING_MODEL, max_output_tokens=0), budget, None,
              clock, embed_client=embedder)  # fmt: skip
    result = search_policies(
        None,  # type: ignore[arg-type]  # any use of the connection would raise
        llm, "Επιτρέπονται σκύλοι;", email_id=None, settings=SearchSettings(threshold=0.5),
    )  # fmt: skip
    assert isinstance(result, InvalidQuery)
    assert embedder.calls == [] and budget.spent() == 0


# --- RRF ----------------------------------------------------------------------------


def test_rrf_adds_one_over_k_plus_rank_per_list() -> None:
    """a: rank 1 and rank 2 -> 1/61 + 1/62; b: rank 2 and rank 1 -> the same; c: rank 3
    in one list only -> 1/63."""
    fused = dict(rrf([["a", "b", "c"], ["b", "a"]], k=60))
    assert fused == {"a": 1 / 61 + 1 / 62, "b": 1 / 62 + 1 / 61, "c": 1 / 63}


@pytest.mark.parametrize(
    ("rankings", "order"),
    [
        ([["b", "a"], ["a", "b"]], ["a", "b"]),  # equal scores: by policy_id
        ([["z"], ["y"]], ["y", "z"]),  # one list each, both rank 1
        ([["a", "b"], ["b"]], ["b", "a"]),  # in both lists beats rank 1 in one
        ([[], []], []),
        ([["c", "a", "b"], []], ["c", "a", "b"]),  # an empty full-text list
    ],
    ids=["tie", "tie-one-list-each", "both-lists", "empty", "fts-empty"],
)
def test_rrf_order_is_fixed(rankings: list[list[str]], order: list[str]) -> None:
    assert [policy_id for policy_id, _ in rrf(rankings, k=60)] == order


def row(policy_id: str, similarity: float, fts_rank: float | None = None) -> Row:
    return Row(policy_id, policy_id.title(), f"Text of {policy_id}.", "0" * 64, similarity,
               fts_rank)  # fmt: skip


def test_each_list_breaks_ties_by_policy_id() -> None:
    rows = [row("b", 0.5, 0.1), row("a", 0.5, 0.1), row("c", 0.9), row("d", 0.1, 0.3)]
    assert vector_ranking(rows) == ["c", "a", "b", "d"]
    assert fts_ranking(rows) == ["d", "a", "b"]  # c matches no lexeme: not in the list


# --- the NO_MATCH decision and the record ---------------------------------------------

SOURCE = PolicySource({"a": "0" * 64}, "f" * 64)


def test_below_the_threshold_is_no_match() -> None:
    result = decide("q", [row("a", 0.49, 0.9), row("b", 0.2)], SearchSettings(0.5), SOURCE)
    assert result == NoMatch("q", 0.49, SearchSettings(0.5), "f" * 64, EMBEDDING_MODEL)


def test_a_full_text_match_does_not_override_no_match() -> None:
    """Only the vector side decides NO_MATCH (D15)."""
    rows = [row("a", 0.1, fts_rank=0.9)]
    assert isinstance(decide("q", rows, SearchSettings(0.5), SOURCE), NoMatch)


def test_at_the_threshold_is_a_match_with_the_top_k_fused() -> None:
    rows = [row("a", 0.5), row("b", 0.4, 0.2), row("c", 0.3, 0.1), row("d", 0.2)]
    result = decide("q", rows, SearchSettings(0.5, top_k=3), SOURCE)
    assert isinstance(result, PolicyHits)
    # b: 1/62 + 1/61 beats a: 1/61 alone; c: 1/63 + 1/62
    assert [h.policy_id for h in result.hits] == ["b", "c", "a"]


def test_the_record_carries_the_text_and_the_policies_hash() -> None:
    result = decide("pets?", [row("a", 0.8, 0.2)], SearchSettings(0.5), SOURCE)
    assert result.record() == {
        "query": "pets?", "policies_sha256": "f" * 64, "embedding_model": EMBEDDING_MODEL,
        "threshold": 0.5, "top_k": 3, "rrf_k": 60,
        "hits": [{"policy_id": "a", "title": "A", "text": "Text of a.", "text_sha256": "0" * 64,
                  "similarity": 0.8, "fts_rank": 0.2, "rrf": 2 / 61}],
    }  # fmt: skip


def test_a_no_match_record_names_the_best_similarity() -> None:
    result = decide("pool?", [row("a", 0.3)], SearchSettings(0.5), SOURCE)
    assert result.record()["no_match"] is True
    assert result.record()["best_similarity"] == 0.3
    assert result.record()["policies_sha256"] == "f" * 64


def test_the_packaged_source_is_hashed_once_per_process() -> None:
    assert packaged_source() is packaged_source()
    assert set(packaged_source().hashes) == {c.policy_id for c in load_policies()}
