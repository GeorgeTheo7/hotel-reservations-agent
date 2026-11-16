"""The retrieval eval's queries (D15): eval/retrieval/queries.yaml, read strictly.

One entry per query: the English text the agent would send to `search_policies`, the
policy ids that must all be in the top 3 (none: out of scope, NO_MATCH expected), where
it came from, who wrote it and its split. An unknown key, a wrong type, a duplicate id,
an unknown policy id or a query the tool would refuse fails the whole file, with the
query id in the message.

`written_by` keeps the authors apart, so their results can be compared:
- owner: the hotel owner;
- claude: Claude, who had read policies.md;
- mentor: written in the owner's review chat without reading policies.md.
"""

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, model_validator

from anemos.policies import load_policies
from anemos.policies.query import InvalidQuery, check_query

type Split = Literal["dev", "held_out"]
type Source = Literal["written", "email"]
type WrittenBy = Literal["owner", "claude", "mentor"]

Text = Annotated[StrictStr, Field(pattern=r"\S")]  # not blank


class QueryError(ValueError):
    """The queries file is not valid; the message names the query."""


class RetrievalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[StrictStr, Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]
    query: Text
    expected: list[Text]  # all must be in the top 3; [] = out of scope
    source: Source
    case: Text | None  # the test email's id, exactly when source is email
    written_by: WrittenBy
    split: Split
    note: Text | None = None

    @model_validator(mode="after")
    def check(self) -> Self:
        if isinstance(refused := check_query(self.query), InvalidQuery):
            raise ValueError(f"search_policies would refuse the query: {refused.message}")
        if len(set(self.expected)) != len(self.expected):
            raise ValueError("expected: an id is listed twice")
        if (self.case is not None) != (self.source == "email"):
            raise ValueError("case is given exactly when source is email")
        return self

    @property
    def in_scope(self) -> bool:
        return bool(self.expected)


def parse_queries(
    text: str, *, policy_ids: Collection[str], case_splits: Mapping[str, str]
) -> list[RetrievalQuery]:
    """Every query, in file order. `case_splits` maps each test email's id to its split:
    a query taken from an email must exist there and share its split (no leak from a
    held-out email into dev)."""
    data = yaml.safe_load(text)
    if not isinstance(data, list):
        raise QueryError("the file must be a list of queries")
    queries: list[RetrievalQuery] = []
    seen: set[str] = set()
    for index, entry in enumerate(data):
        name = entry.get("id", f"entry {index}") if isinstance(entry, dict) else f"entry {index}"
        try:
            query = RetrievalQuery.model_validate(entry)
        except ValidationError as exc:
            raise QueryError(f"{name}: {exc}") from exc
        if query.id in seen:
            raise QueryError(f"{query.id}: id used twice")
        seen.add(query.id)
        if unknown := sorted(set(query.expected) - set(policy_ids)):
            raise QueryError(f"{query.id}: unknown policy ids {unknown}")
        if query.case is not None:
            if query.case not in case_splits:
                raise QueryError(f"{query.id}: no test email {query.case!r}")
            if case_splits[query.case] != query.split:
                raise QueryError(f"{query.id}: split {query.split} but {query.case} is "
                                 f"{case_splits[query.case]}")  # fmt: skip
        queries.append(query)
    return queries


def load_queries(path: Path, *, case_splits: Mapping[str, str]) -> list[RetrievalQuery]:
    """The queries file, checked against the packaged policies."""
    ids = {c.policy_id for c in load_policies()}
    return parse_queries(path.read_text(encoding="utf-8"), policy_ids=ids, case_splits=case_splits)
