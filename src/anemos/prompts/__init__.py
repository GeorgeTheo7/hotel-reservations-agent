"""Versioned prompts. A prompt file is never edited once its version is pinned here: a
change is a new file and a new version (extraction_v2.md), so every audit row's
`prompt_version` names exactly the text that was sent.

`prompt_version` is "<name>@<first 8 hex of the SHA-256 of the file>"; a unit test fails
if a pinned file no longer matches its hash. Each prompt also carries the call settings
it was written for (its output limit and reasoning effort); the model is chosen
separately (D2).
"""

import hashlib
from dataclasses import dataclass
from importlib.resources import files

from anemos.llm.adapter import Effort


@dataclass(frozen=True)
class PromptSpec:
    name: str
    sha256_8: str  # pinned: the first 8 hex digits of the file's SHA-256
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


def content_hash(text: str) -> str:
    """The first 8 hex digits of the SHA-256 of the text, line endings unified."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:8]


# Output: the largest gold extraction is ~400 bytes of JSON (so at most ~400 tokens),
# plus headroom for minimal reasoning; a test checks every gold label fits.
EXTRACTION_V1 = PromptSpec(
    name="extraction_v1", sha256_8="82cc2864", max_output_tokens=600, reasoning_effort="minimal"
)

PROMPTS = (EXTRACTION_V1,)
