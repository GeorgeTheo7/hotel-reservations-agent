"""The schema `policy_select_v1` was pinned with: kept unchanged so the pinned
prompt_version still names exactly what the dev wiring check sent
(eval/results/20261116T154052Z-retrieval-dev-gpt-6-luna.json). Not used at run time:
`search_policies` sends `policy_select_v2` with `anemos.policies.PolicySelection`.

Its name and docstring are part of the hash (the docstring is sent as the schema's
description): do not edit them.
"""

from pydantic import BaseModel, ConfigDict

from anemos.policies import PolicyId


class PolicySelection(BaseModel):
    """The selector's answer (policy_select_v1): the ids of the chunks that answer the
    question, most useful first; an empty list when none does."""

    model_config = ConfigDict(extra="forbid")

    policy_ids: list[PolicyId]
