"""anemos.policies.ranking without a database (the retrieval eval's comparison modes):
RRF, the two rankings, the threshold decision.

The SQL side (full text, the table check) is in tests/integration/test_policy_ranking_sql.py;
the production tool (the selector) in tests/unit/test_policy_selector.py.
"""

import pytest

from anemos.policies import load_policies
from anemos.policies.ranking import (
    BelowThreshold,
    Ranked,
    Row,
    SearchSettings,
    decide,
    fts_ranking,
    packaged_source,
    rrf,
    vector_ranking,
)

pytestmark = pytest.mark.unit

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


# --- the threshold decision ---------------------------------------------------------


def test_below_the_threshold() -> None:
    result = decide("q", [row("a", 0.49, 0.9), row("b", 0.2)], SearchSettings(0.5))
    assert result == BelowThreshold("q", 0.49)


def test_a_full_text_match_does_not_lift_a_query_above_the_threshold() -> None:
    """Only the vector side decides it."""
    rows = [row("a", 0.1, fts_rank=0.9)]
    assert isinstance(decide("q", rows, SearchSettings(0.5)), BelowThreshold)


def test_at_the_threshold_the_top_k_fused() -> None:
    rows = [row("a", 0.5), row("b", 0.4, 0.2), row("c", 0.3, 0.1), row("d", 0.2)]
    result = decide("q", rows, SearchSettings(0.5, top_k=3))
    assert isinstance(result, Ranked)
    # b: 1/62 + 1/61 beats a: 1/61 alone; c: 1/63 + 1/62
    assert [h.policy_id for h in result.hits] == ["b", "c", "a"]
    assert result.hits[0].rrf == 1 / 62 + 1 / 61


def test_the_packaged_source_is_hashed_once_per_process() -> None:
    assert packaged_source() is packaged_source()
    assert set(packaged_source().hashes) == {c.policy_id for c in load_policies()}
