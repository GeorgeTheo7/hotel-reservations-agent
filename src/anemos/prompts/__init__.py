"""Versioned prompts. What a prompt version covers is never edited once pinned here: a
change is a new file and a new version (extraction_v2.md), so every audit row's
`prompt_version` names exactly what was sent.

`prompt_version` is "<name>@<first 8 hex of a SHA-256>" over the prompt file, the
output schema exactly as it is sent (the strict JSON schema, `openai_client.
text_format`) and the call settings the prompt carries (its output limit and reasoning
effort): changing any of them is a new version. A unit test fails if a pinned hash no
longer matches. The model is chosen separately (D2).
"""

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from pydantic import BaseModel

from anemos.agent.tools import TOOLS_BY_ROUTE
from anemos.extraction import ExtractionOutput
from anemos.llm.adapter import Effort
from anemos.llm.openai_client import function_tool, text_format
from anemos.policies import BaselineAnswer, PolicySelection
from anemos.policies.selection_v1 import PolicySelection as PolicySelectionV1


@dataclass(frozen=True)
class PromptSpec:
    name: str
    schema: type[BaseModel]  # the structured output the prompt asks for
    sha256_8: str  # pinned: see content_hash
    max_output_tokens: int
    reasoning_effort: Effort | None

    @property
    def file(self) -> str:
        return f"{self.name}.md"

    @property
    def version(self) -> str:
        return f"{self.name}@{self.sha256_8}"

    def text(self) -> str:
        return files("anemos.prompts").joinpath(self.file).read_text(encoding="utf-8")

    def current_hash(self) -> str:
        """The hash of what this spec sends today; must equal the pinned sha256_8."""
        return content_hash(
            self.text(),
            self.schema,
            max_output_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort,
        )


@dataclass(frozen=True)
class ToolPromptSpec:
    """A prompt for tool turns (the agent loop, D11): its hash covers the prompt text,
    every route's tool definitions exactly as sent (strict functions) and the call
    settings, so a changed tool description or argument is a new version too."""

    name: str
    sha256_8: str  # pinned: see current_hash
    max_output_tokens: int
    reasoning_effort: Effort | None

    @property
    def file(self) -> str:
        return f"{self.name}.md"

    @property
    def version(self) -> str:
        return f"{self.name}@{self.sha256_8}"

    def text(self) -> str:
        return files("anemos.prompts").joinpath(self.file).read_text(encoding="utf-8")

    def current_hash(self) -> str:
        return tool_prompt_hash(
            self.text(),
            {route: [function_tool(t) for t in tools] for route, tools in TOOLS_BY_ROUTE.items()},
            max_output_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort,
        )


def tool_prompt_hash(
    text: str,
    tools: dict[str, Any],
    *,
    max_output_tokens: int,
    reasoning_effort: Effort | None,
) -> str:
    """As `content_hash`, with the tools as sent in place of one output schema."""
    sent = {"tools": tools, "max_output_tokens": max_output_tokens,
            "reasoning_effort": reasoning_effort}  # fmt: skip
    payload = json.dumps(sent, sort_keys=True, ensure_ascii=False)
    everything = text.replace("\r\n", "\n") + "\n--- sent with ---\n" + payload
    return hashlib.sha256(everything.encode("utf-8")).hexdigest()[:8]


def content_hash(
    text: str,
    schema: type[BaseModel],
    *,
    max_output_tokens: int,
    reasoning_effort: Effort | None,
) -> str:
    """The first 8 hex digits of the SHA-256 of the prompt text (line endings unified),
    the schema as sent and the call settings (JSON with sorted keys)."""
    sent = {
        "schema": text_format(schema),
        "max_output_tokens": max_output_tokens,
        "reasoning_effort": reasoning_effort,
    }
    payload = json.dumps(sent, sort_keys=True, ensure_ascii=False)
    everything = text.replace("\r\n", "\n") + "\n--- sent with ---\n" + payload
    return hashlib.sha256(everything.encode("utf-8")).hexdigest()[:8]


# Output: the largest gold extraction is ~400 bytes of JSON (so at most ~400 tokens),
# plus headroom; a test checks every gold label fits. Reasoning "none": extraction
# copies fields out of an email (and gpt-6-luna refuses "minimal" with HTTP 400).
EXTRACTION_V1 = PromptSpec(
    name="extraction_v1",
    schema=ExtractionOutput,
    sha256_8="c4e2a967",
    max_output_tokens=600,
    reasoning_effort="none",
)

# v2 (D68): holiday names are canonical (easter, christmas, new_year; New Year's year is
# the year of 1 January); code accepts only those, anything else asks for dates.
EXTRACTION_V2 = PromptSpec(
    name="extraction_v2",
    schema=ExtractionOutput,
    sha256_8="b37c0d57",
    max_output_tokens=600,
    reasoning_effort="none",
)

# v3 (docs/NOTES.md): (a) count the people the guest names, named children as children,
# a bare "we" not a count; (b) injection only for text addressed to an AI, assistant or
# "the system", never a request to the hotel. v2 left "my husband … we" at adults null
# and flagged "as agreed on the phone we get 50% off" as an injection.
EXTRACTION_V3 = PromptSpec(
    name="extraction_v3",
    schema=ExtractionOutput,
    sha256_8="c5155ec7",
    max_output_tokens=600,
    reasoning_effort="none",
)

# The retrieval eval's all-chunks baseline (D15): every policy chunk and one query in,
# up to 3 chunk ids out (an enum of the real ids). ~40 output tokens at most; reasoning
# "none", as for extraction. Not used by a run: only eval/retrieval_eval.py sends it.
RETRIEVAL_BASELINE_V1 = PromptSpec(
    name="retrieval_baseline_v1",
    schema=BaselineAnswer,
    sha256_8="9f50617c",
    max_output_tokens=100,
    reasoning_effort="none",
)

# The policy selector (D15): search_policies. v1: the same text as retrieval_baseline_v1,
# which scored 100% / 100% on the dev queries (a test keeps the two texts equal); its own
# name and schema (PolicySelection), so a run's prompt_version is never "baseline".
POLICY_SELECT_V1 = PromptSpec(
    name="policy_select_v1",
    schema=PolicySelectionV1,
    sha256_8="a2c70954",
    max_output_tokens=100,
    reasoning_effort="none",
)

# v2 (docs/NOTES.md): only the ids needed to answer, never more than 3, and maxItems 3 in
# the schema. v1 returned up to 7 ids on the dev wiring check, and the code silently kept
# the first 3.
POLICY_SELECT_V2 = PromptSpec(
    name="policy_select_v2",
    schema=PolicySelection,
    sha256_8="a8eea53b",
    max_output_tokens=100,
    reasoning_effort="none",
)

# The agent loop's call settings (D2), for the agent prompt (week 5, step 7). The model
# writes only the prose around the placeholders ({{QUOTE_BLOCK}}, {{REQUEST_SUMMARY}}):
# a long Greek reply is ~300 words, estimated (not measured) at 2-3 tokens a word, so
# ~900 tokens, ~1,000 with JSON escaping and the other arguments; 2,000 doubles that.
# A reply cut off at the limit is a typed error (ReplyCutOff), never a broken draft;
# the agent eval measures the real output tokens. Reasoning "none", as for extraction.
AGENT_MAX_OUTPUT_TOKENS = 2_000
AGENT_REASONING_EFFORT: Effort = "none"

# The agent loop (D11, NOTES): tools per route, placeholders only for prices and dates,
# the six escalation codes the model may choose.
AGENT_V1 = ToolPromptSpec(
    name="agent_v1",
    sha256_8="73d26941",
    max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
    reasoning_effort=AGENT_REASONING_EFFORT,
)
CURRENT_AGENT = AGENT_V1  # what the agent loop sends (D2, D11)

PROMPTS: tuple[PromptSpec | ToolPromptSpec, ...] = (
    EXTRACTION_V1, EXTRACTION_V2, EXTRACTION_V3, RETRIEVAL_BASELINE_V1, POLICY_SELECT_V1,
    POLICY_SELECT_V2, AGENT_V1,
)  # fmt: skip
CURRENT_POLICY_SELECT = POLICY_SELECT_V2  # what search_policies sends (D2, D15)
CURRENT_EXTRACTION = EXTRACTION_V3  # what runs and evaluations send (D2)
