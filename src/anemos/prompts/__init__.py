"""Versioned prompts. What a prompt version covers is never edited once pinned here: a
change is a new file and a new version (extraction_v2.md), so every audit row's
`prompt_version` names exactly what was sent.

`prompt_version` is "<name>@<first 8 hex of a SHA-256>" over the prompt file and the
output schema exactly as it is sent (the strict JSON schema, `openai_client.
text_format`): the model reads both, so changing either is a new version. A unit test
fails if a pinned hash no longer matches. Each prompt also carries the call settings it
was written for (its output limit and reasoning effort); the model is chosen
separately (D2).
"""

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files

from pydantic import BaseModel

from anemos.extraction import ExtractionOutput
from anemos.llm.adapter import Effort
from anemos.llm.openai_client import text_format


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


def content_hash(text: str, schema: type[BaseModel]) -> str:
    """The first 8 hex digits of the SHA-256 of the prompt text (line endings unified)
    and the schema as sent (JSON with sorted keys)."""
    sent_schema = json.dumps(text_format(schema), sort_keys=True, ensure_ascii=False)
    both = text.replace("\r\n", "\n") + "\n--- output schema ---\n" + sent_schema
    return hashlib.sha256(both.encode("utf-8")).hexdigest()[:8]


# Output: the largest gold extraction is ~400 bytes of JSON (so at most ~400 tokens),
# plus headroom for minimal reasoning; a test checks every gold label fits.
EXTRACTION_V1 = PromptSpec(
    name="extraction_v1",
    schema=ExtractionOutput,
    sha256_8="21acca18",
    max_output_tokens=600,
    reasoning_effort="minimal",
)

PROMPTS = (EXTRACTION_V1,)
