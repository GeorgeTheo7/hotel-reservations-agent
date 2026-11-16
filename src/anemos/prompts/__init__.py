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

from pydantic import BaseModel

from anemos.extraction import ExtractionOutput
from anemos.llm.adapter import Effort
from anemos.llm.openai_client import text_format
from anemos.policies import BaselineAnswer, PolicySelection


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

# The policy selector (D15): search_policies. The same text as retrieval_baseline_v1,
# which scored 100% / 100% on the dev queries (a test keeps the two texts equal); its own
# name and schema (PolicySelection), so a run's prompt_version is never "baseline".
POLICY_SELECT_V1 = PromptSpec(
    name="policy_select_v1",
    schema=PolicySelection,
    sha256_8="a2c70954",
    max_output_tokens=100,
    reasoning_effort="none",
)

PROMPTS = (EXTRACTION_V1, EXTRACTION_V2, RETRIEVAL_BASELINE_V1, POLICY_SELECT_V1)
CURRENT_EXTRACTION = EXTRACTION_V2  # what runs and evaluations send (D2)
