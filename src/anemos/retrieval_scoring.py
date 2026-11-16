"""Scoring for the retrieval eval (D15): four modes, the NO_MATCH threshold rule, and
the comparison of hybrid with vector only. Pure functions: no database, no model.

The modes, for one query:
- vector: chunks by cosine similarity; NO_MATCH below the threshold;
- full_text: chunks matching a query lexeme, by ts_rank; NO_MATCH when none matches;
- hybrid: RRF of the two lists (`anemos.policies.ranking.decide`); NO_MATCH as vector
  (only the vector side decides it), so the two share their NO_MATCH numbers;
- all_chunks: the baseline, a model given every chunk; NO_MATCH when it returns none.

A query is right in a mode when (in scope) every expected id is in the top 3 and the
mode did not answer NO_MATCH, or (out of scope) the mode answered NO_MATCH. Extra
relevant ids in the top 3 are not counted wrong.

The threshold rule and "better" are fixed in docs/NOTES.md before the first run.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from anemos.policies import BaselineAnswer
from anemos.policies.ranking import (
    BelowThreshold,
    Ranked,
    Row,
    SearchSettings,
    decide,
    fts_ranking,
    vector_ranking,
)
from anemos.retrieval_eval import RetrievalQuery

type Mode = Literal["vector", "full_text", "hybrid", "all_chunks"]
MODES: tuple[Mode, ...] = ("vector", "full_text", "hybrid", "all_chunks")


@dataclass(frozen=True)
class ModeAnswer:
    top: tuple[str, ...]  # up to top_k ids, best first, as ranked before any NO_MATCH
    no_match: bool


def search_answers(
    query: str, rows: Sequence[Row], settings: SearchSettings
) -> dict[Mode, ModeAnswer]:
    """The three ranking modes from one query's rows. Hybrid goes through
    `ranking.decide`; its ranking before the threshold through `decide` with no
    threshold."""
    k = settings.top_k
    unthresholded = decide(query, rows, SearchSettings(-2.0, k, settings.rrf_k))
    assert isinstance(unthresholded, Ranked)  # cosine similarity is never below -1
    below = isinstance(decide(query, rows, settings), BelowThreshold)
    fts = fts_ranking(rows)
    return {
        "vector": ModeAnswer(tuple(vector_ranking(rows)[:k]), below),
        "full_text": ModeAnswer(tuple(fts[:k]), not fts),
        "hybrid": ModeAnswer(tuple(h.policy_id for h in unthresholded.hits), below),
    }


def baseline_answer(answer: BaselineAnswer, top_k: int) -> ModeAnswer:
    ids = tuple(dict.fromkeys(answer.policy_ids))  # repeated ids count once
    return ModeAnswer(ids[:top_k], not ids)


def correct(query: RetrievalQuery, answer: ModeAnswer) -> bool:
    if not query.in_scope:
        return answer.no_match
    return not answer.no_match and set(query.expected) <= set(answer.top)


def ranked(query: RetrievalQuery, answer: ModeAnswer) -> bool:
    """In scope only: every expected id in the top 3, NO_MATCH ignored (ranking alone)."""
    return set(query.expected) <= set(answer.top)


@dataclass(frozen=True)
class ModeScore:
    in_scope: int
    hits: int  # in scope and right (recall@3)
    ranked_hits: int  # in scope, right before the threshold
    out_of_scope: int
    caught: int  # out of scope and NO_MATCH
    no_match_answers: int  # every NO_MATCH answer, right or wrong

    @property
    def recall_at_3(self) -> float | None:
        return self.hits / self.in_scope if self.in_scope else None

    @property
    def ranked_recall_at_3(self) -> float | None:
        return self.ranked_hits / self.in_scope if self.in_scope else None

    @property
    def no_match_recall(self) -> float | None:
        return self.caught / self.out_of_scope if self.out_of_scope else None

    @property
    def no_match_precision(self) -> float | None:
        return self.caught / self.no_match_answers if self.no_match_answers else None


def score_mode(pairs: Sequence[tuple[RetrievalQuery, ModeAnswer]]) -> ModeScore:
    inside = [(q, a) for q, a in pairs if q.in_scope]
    outside = [(q, a) for q, a in pairs if not q.in_scope]
    return ModeScore(
        in_scope=len(inside),
        hits=sum(correct(q, a) for q, a in inside),
        ranked_hits=sum(ranked(q, a) for q, a in inside),
        out_of_scope=len(outside),
        caught=sum(a.no_match for _, a in outside),
        no_match_answers=sum(a.no_match for _, a in pairs),
    )


# --- the threshold rule (docs/NOTES.md, fixed before the first run) ---------------------

MAX_IN_SCOPE_MISS_PERCENT = 5


@dataclass(frozen=True)
class ThresholdChoice:
    threshold: float
    allowed_misses: int  # k = floor(5% of the in-scope dev queries)
    upper: float  # s(k+1): the highest threshold leaving at most k in-scope at NO_MATCH
    lower: float | None  # the highest out-of-scope best similarity below `upper`
    rounded: bool  # the midpoint rounded to 3 decimals stayed inside (lower, upper]


def choose_threshold(in_scope: Sequence[float], out_of_scope: Sequence[float]) -> ThresholdChoice:
    """Catch as many out-of-scope dev queries as possible with at most 5% of in-scope
    dev queries at NO_MATCH (NO_MATCH is best < threshold). Every threshold in
    (lower, upper] catches the same out-of-scope set; the midpoint keeps a margin on
    both sides. No out-of-scope query below `upper`: the threshold is `upper`."""
    ordered = sorted(in_scope)
    if not ordered:
        raise ValueError("no in-scope similarities")
    k = len(ordered) * MAX_IN_SCOPE_MISS_PERCENT // 100
    upper = ordered[k]
    below = [s for s in out_of_scope if s < upper]
    if not below:
        return ThresholdChoice(upper, k, upper, None, rounded=False)
    lower = max(below)
    middle = (lower + upper) / 2
    rounded = round(middle, 3)
    if lower < rounded <= upper:
        return ThresholdChoice(rounded, k, upper, lower, rounded=True)
    return ThresholdChoice(middle, k, upper, lower, rounded=False)


# --- hybrid against vector only (docs/NOTES.md: a difference of 3 or more queries) ------

BETTER_MARGIN = 3


@dataclass(frozen=True)
class Comparison:
    only_first: tuple[str, ...]  # query ids the first mode got right and the second not
    only_second: tuple[str, ...]
    verdict: Literal["better", "worse", "same"]  # the first mode, against the second


def compare(
    first: Sequence[tuple[RetrievalQuery, ModeAnswer]],
    second: Sequence[tuple[RetrievalQuery, ModeAnswer]],
) -> Comparison:
    """Per query, which of the two modes got it right; the first is better or worse
    only with a difference of BETTER_MARGIN or more queries."""
    second_right = {q.id: correct(q, a) for q, a in second}
    first_right = {q.id: correct(q, a) for q, a in first}
    if first_right.keys() != second_right.keys():
        raise ValueError("the two modes were run on different queries")
    only_first = tuple(i for i in first_right if first_right[i] and not second_right[i])
    only_second = tuple(i for i in first_right if second_right[i] and not first_right[i])
    difference = len(only_first) - len(only_second)
    verdict: Literal["better", "worse", "same"] = (
        "better" if difference >= BETTER_MARGIN
        else "worse" if difference <= -BETTER_MARGIN
        else "same"
    )  # fmt: skip
    return Comparison(only_first, only_second, verdict)


def largest_change(first: Sequence[float], second: Sequence[float]) -> float:
    """The largest change in a query's best similarity between two embeddings of the
    same queries: below 0.0005, a threshold's third decimal means something."""
    return max(abs(a - b) for a, b in zip(first, second, strict=True))
