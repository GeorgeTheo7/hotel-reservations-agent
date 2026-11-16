"""The retrieval eval's query set (eval/retrieval/queries.yaml) and its strict loader."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from anemos.email_cases import load_cases
from anemos.policies import load_policies
from anemos.retrieval_eval import QueryError, RetrievalQuery, load_queries, parse_queries

pytestmark = pytest.mark.unit

EVAL = Path(__file__).resolve().parents[2] / "eval"
CASE_SPLITS = {case.id: case.split for case in load_cases(EVAL / "emails")}
QUERIES = load_queries(EVAL / "retrieval" / "queries.yaml", case_splits=CASE_SPLITS)
POLICY_IDS = {c.policy_id for c in load_policies()}

# --- the packaged set ---------------------------------------------------------------


def test_the_set_has_its_planned_size() -> None:
    assert len(QUERIES) == 68
    assert sum(q.in_scope for q in QUERIES) == 47


def test_every_chunk_has_a_dev_query() -> None:
    """Each chunk can be found at least once on the split the threshold is set on."""
    covered = {i for q in QUERIES if q.split == "dev" for i in q.expected}
    assert covered == POLICY_IDS


@pytest.mark.parametrize("in_scope", [True, False], ids=["in-scope", "out-of-scope"])
def test_about_30_percent_is_held_out_in_each_group(in_scope: bool) -> None:
    group = [q for q in QUERIES if q.in_scope is in_scope]
    share = sum(q.split == "held_out" for q in group) / len(group)
    assert 0.25 <= share <= 0.35, share


def test_every_author_and_source_is_present() -> None:
    """The report compares them: each group must exist in the set."""
    assert {q.written_by for q in QUERIES} == {"mentor", "claude"}
    assert {q.source for q in QUERIES} == {"written", "email"}
    assert any(not q.in_scope for q in QUERIES if q.source == "email")


# --- the loader ----------------------------------------------------------------------

VALID: dict[str, Any] = {
    "id": "q-x", "query": "Are pets allowed?", "expected": ["pets-assistance-dogs"],
    "source": "written", "case": None, "written_by": "mentor", "split": "dev",
}  # fmt: skip


def parse(*entries: dict[str, Any]) -> list[RetrievalQuery]:
    return parse_queries(yaml.safe_dump(list(entries)), policy_ids=POLICY_IDS,
                         case_splits={"el-pets-policy": "dev"})  # fmt: skip


@pytest.mark.parametrize("written_by", ["owner", "claude", "mentor"])
def test_each_author_is_accepted(written_by: str) -> None:
    assert parse({**VALID, "written_by": written_by})[0].written_by == written_by


def test_an_email_query_with_its_case_and_split_is_accepted() -> None:
    entry = {**VALID, "source": "email", "case": "el-pets-policy"}
    assert parse(entry)[0].case == "el-pets-policy"


def test_an_empty_expected_list_is_out_of_scope() -> None:
    assert parse({**VALID, "expected": []})[0].in_scope is False


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"written_by": "intern"}, "written_by"),
        ({"extra": 1}, "extra"),
        ({"split": "test"}, "split"),
        ({"id": "Q_X"}, "id"),
        ({"query": "Επιτρέπονται κατοικίδια;"}, "write the query in English"),
        ({"query": "a" * 301}, "up to 300 characters"),
        ({"query": " "}, r"query\n  String should match pattern"),  # the not-blank rule
        ({"expected": ["pool"]}, "unknown policy ids"),
        ({"expected": ["wifi", "wifi"]}, "listed twice"),
        ({"case": "el-pets-policy"}, "exactly when source is email"),
        ({"source": "email"}, "exactly when source is email"),
        ({"source": "email", "case": "no-such-case"}, "no test email"),
        ({"source": "email", "case": "el-pets-policy", "split": "held_out"}, "split held_out"),
    ],
    ids=[
        "author",
        "unknown-key",
        "split",
        "id-shape",
        "greek",
        "too-long",
        "blank",
        "unknown-policy",
        "duplicate-expected",
        "case-without-email",
        "email-without-case",
        "unknown-case",
        "split-leak",
    ],
)
def test_a_bad_entry_is_refused_with_its_id(change: dict[str, Any], message: str) -> None:
    with pytest.raises(QueryError, match=message) as refused:
        parse({**VALID, **change})
    assert str(refused.value).startswith(("q-x:", "Q_X:"))


def test_a_duplicate_id_is_refused() -> None:
    with pytest.raises(QueryError, match="q-x: id used twice"):
        parse(VALID, VALID)


def test_a_file_that_is_not_a_list_is_refused() -> None:
    with pytest.raises(QueryError, match="a list"):
        parse_queries("id: q-x", policy_ids=POLICY_IDS, case_splits={})
