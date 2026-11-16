"""Retrieval eval scoring (anemos.retrieval_scoring): what counts as right, the four
modes' numbers, the threshold rule, "better", and the baseline's id enum."""

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from anemos.llm.openai_client import text_format
from anemos.policies import load_policies
from anemos.policies.search import PolicySource, Row, SearchSettings
from anemos.retrieval_eval import RetrievalQuery
from anemos.retrieval_scoring import (
    BaselineAnswer,
    ModeAnswer,
    PolicyId,
    baseline_answer,
    choose_threshold,
    compare,
    correct,
    largest_change,
    score_mode,
    search_answers,
)

pytestmark = pytest.mark.unit


def query(qid: str, *expected: str) -> RetrievalQuery:
    return RetrievalQuery(id=qid, query=f"question {qid}", expected=list(expected),
                          source="written", case=None, written_by="claude",
                          split="dev")  # fmt: skip


def hits(*ids: str) -> ModeAnswer:
    return ModeAnswer(ids, no_match=False)


NO_MATCH = ModeAnswer((), no_match=True)

# --- the baseline may only name a real chunk ---------------------------------------------


def test_the_baseline_ids_are_exactly_the_chunks() -> None:
    ids = get_args(PolicyId)
    assert len(ids) == len(set(ids))
    assert set(ids) == {c.policy_id for c in load_policies()}


def test_the_sent_schema_is_an_enum_of_the_chunk_ids() -> None:
    schema: Any = text_format(BaselineAnswer)["format"]["schema"]  # type: ignore[typeddict-item]
    items = schema["properties"]["policy_ids"]["items"]
    assert set(items["enum"]) == {c.policy_id for c in load_policies()}


def test_a_made_up_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        BaselineAnswer.model_validate({"policy_ids": ["swimming-pool"]})


def test_the_baseline_answer_keeps_the_first_three_distinct_ids() -> None:
    answer = BaselineAnswer.model_validate(
        {"policy_ids": ["wifi", "wifi", "parking", "transfer", "breakfast"]}
    )
    assert baseline_answer(answer, 3) == ModeAnswer(("wifi", "parking", "transfer"), False)
    assert baseline_answer(BaselineAnswer(policy_ids=[]), 3) == NO_MATCH


# --- what counts as right -------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "answer", "right"),
    [
        (["wifi"], hits("parking", "wifi"), True),
        (["wifi", "parking"], hits("wifi", "transfer", "parking"), True),
        (["wifi", "parking"], hits("wifi", "transfer", "breakfast"), False),  # all, not any
        (["wifi"], ModeAnswer(("wifi",), no_match=True), False),  # NO_MATCH in scope: a miss
        ([], NO_MATCH, True),
        ([], hits("transfer"), False),
    ],
    ids=["in-top-3", "both-ids", "one-of-two", "no-match-in-scope", "caught", "let-through"],
)
def test_right_or_wrong(expected: list[str], answer: ModeAnswer, right: bool) -> None:
    assert correct(query("q", *expected), answer) is right


def test_a_mode_score_counts_recall_and_no_match_both_ways() -> None:
    pairs = [
        (query("a", "wifi"), hits("wifi")),
        (query("b", "parking"), ModeAnswer(("parking",), no_match=True)),  # ranked, cut off
        (query("c", "deposit"), hits("wifi")),
        (query("d"), NO_MATCH),
        (query("e"), hits("transfer")),
    ]
    score = score_mode(pairs)
    assert (score.recall_at_3, score.ranked_recall_at_3) == (1 / 3, 2 / 3)
    assert score.no_match_recall == 1 / 2  # d caught, e let through
    assert score.no_match_precision == 1 / 2  # two NO_MATCH answers, one right (d)


# --- the threshold rule ----------------------------------------------------------------


def check_rule(in_scope: list[float], out_of_scope: list[float], threshold: float) -> None:
    """What the rule promises: at most 5% of in-scope below the threshold, and every
    out-of-scope query that could be caught under that cap is caught."""
    k = len(in_scope) * 5 // 100
    assert sum(s < threshold for s in in_scope) <= k
    upper = sorted(in_scope)[k]
    assert all(o < threshold for o in out_of_scope if o < upper)


def test_the_midpoint_between_the_cap_and_the_out_of_scope_below_it() -> None:
    """20 in scope: k = 1, so s(2) = 0.35 is the cap; 0.10 and 0.32 are caught, 0.50
    gets through; the midpoint of (0.32, 0.35] is 0.335."""
    in_scope = [0.30, 0.35] + [0.40 + i / 100 for i in range(18)]
    out_of_scope = [0.10, 0.32, 0.50]
    choice = choose_threshold(in_scope, out_of_scope)
    assert (choice.allowed_misses, choice.upper, choice.lower) == (1, 0.35, 0.32)
    assert choice.threshold == 0.335 and choice.rounded
    check_rule(in_scope, out_of_scope, choice.threshold)


def test_under_20_in_scope_queries_no_miss_is_allowed() -> None:
    in_scope = [0.30, 0.35] + [0.40] * 17  # 19: 5% is 0.95, k = 0
    choice = choose_threshold(in_scope, [0.10, 0.32])
    assert (choice.allowed_misses, choice.upper, choice.lower) == (0, 0.30, 0.10)
    check_rule(in_scope, [0.10, 0.32], choice.threshold)


def test_with_nothing_to_catch_the_threshold_is_the_cap() -> None:
    choice = choose_threshold([0.4] * 20, [0.6, 0.7])
    assert (choice.threshold, choice.lower, choice.rounded) == (0.4, None, False)


def test_a_rounding_that_leaves_the_interval_keeps_the_exact_midpoint() -> None:
    choice = choose_threshold([0.3334] * 20, [0.3331])
    assert not choice.rounded and choice.threshold == (0.3331 + 0.3334) / 2
    check_rule([0.3334] * 20, [0.3331], choice.threshold)


# --- hybrid against vector only ---------------------------------------------------------


@pytest.mark.parametrize(
    ("hybrid_right", "vector_right", "verdict"),
    [(3, 0, "better"), (2, 0, "same"), (3, 1, "same"), (0, 3, "worse"), (4, 1, "better")],
)
def test_better_needs_a_difference_of_three_queries(
    hybrid_right: int, vector_right: int, verdict: str
) -> None:
    """Queries 0..9 all in scope; the first `hybrid_right` are right in hybrid only, the
    next `vector_right` in vector only, the rest right in both."""
    queries = [query(f"q{i}", "wifi") for i in range(10)]
    hybrid = [(q, hits("parking") if hybrid_right <= i < hybrid_right + vector_right
               else hits("wifi")) for i, q in enumerate(queries)]  # fmt: skip
    vector = [(q, hits("parking") if i < hybrid_right else hits("wifi"))
              for i, q in enumerate(queries)]  # fmt: skip
    comparison = compare(hybrid, vector)
    assert (len(comparison.only_first), len(comparison.only_second)) == (
        hybrid_right, vector_right)  # fmt: skip
    assert comparison.verdict == verdict


def test_the_largest_change_between_two_embeddings() -> None:
    assert largest_change([0.5, 0.4], [0.5004, 0.3990]) == pytest.approx(0.001)


# --- the search modes come from the production code -------------------------------------


def row(policy_id: str, similarity: float, fts_rank: float | None = None) -> Row:
    return Row(policy_id, policy_id, "text", "0" * 64, similarity, fts_rank)


SOURCE = PolicySource({}, "f" * 64)


def test_the_modes_split_one_querys_rows() -> None:
    rows = [row("a", 0.5), row("b", 0.4, 0.2), row("c", 0.3, 0.1), row("d", 0.2)]
    answers = search_answers("q", rows, SearchSettings(0.45), SOURCE)
    assert answers["vector"] == ModeAnswer(("a", "b", "c"), no_match=False)
    assert answers["full_text"] == ModeAnswer(("b", "c"), no_match=False)
    assert answers["hybrid"] == ModeAnswer(("b", "c", "a"), no_match=False)  # decide's RRF


def test_below_the_threshold_vector_and_hybrid_answer_no_match_but_keep_their_ranking() -> None:
    rows = [row("a", 0.3), row("b", 0.2)]
    answers = search_answers("q", rows, SearchSettings(0.45), SOURCE)
    assert answers["vector"] == ModeAnswer(("a", "b"), no_match=True)
    assert answers["hybrid"] == ModeAnswer(("a", "b"), no_match=True)
    assert answers["full_text"] == NO_MATCH  # no lexeme matched
