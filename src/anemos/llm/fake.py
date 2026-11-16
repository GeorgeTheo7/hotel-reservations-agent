"""A scripted model client for tests and the demo: no network, no key, no cost.

It goes behind the real `LLM` (adapter.py), so budget, retry and audit code run exactly
as in a real call; only the provider is replaced. Each `send` takes the next scripted
step: an answer (with the usage it reports) or an error to raise. A step may also be a
function of the prompt, for answers that depend on the email (the demo's gold labels).
"""

import hashlib
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from anemos.llm.adapter import ClientError, EmbedReply, Prompt, Reply, Settings, Vector
from anemos.llm.pricing import Usage

DEFAULT_USAGE = Usage(input_tokens=1_000, cached_input_tokens=0, output_tokens=200)


@dataclass(frozen=True)
class Answer:
    output: BaseModel | dict[str, Any]  # a dict is validated against the requested schema
    usage: Usage = DEFAULT_USAGE
    response_model: str | None = None  # default: the requested model


type Step = Answer | ClientError | Callable[[Prompt], Answer | ClientError]


@dataclass(frozen=True)
class Sent:
    prompt: Prompt
    schema: type[BaseModel]
    settings: Settings


@dataclass
class ScriptedClient:
    script: list[Step] = field(default_factory=list)
    sent: list[Sent] = field(default_factory=list)

    @classmethod
    def of(cls, steps: Iterable[Step]) -> "ScriptedClient":
        return cls(list(steps))

    def send[T: BaseModel](self, prompt: Prompt, schema: type[T], settings: Settings) -> Reply[T]:
        self.sent.append(Sent(prompt, schema, settings))
        if not self.script:
            raise AssertionError(f"the script ran out at call {len(self.sent)}")
        step = self.script.pop(0)
        if callable(step) and not isinstance(step, ClientError):
            step = step(prompt)
        if isinstance(step, ClientError):
            raise step
        output = schema.model_validate(
            step.output.model_dump() if isinstance(step.output, BaseModel) else step.output
        )
        return Reply(output, step.usage, step.response_model or settings.model)


_WORD = re.compile(r"[a-z0-9]+")


def hashing_vector(text: str, dimensions: int) -> Vector:
    """A bag-of-words vector: each lowercase word adds 1 to the slot its hash picks,
    then the vector is scaled to length 1. Texts sharing words come out similar, so
    retrieval code can be tested without a model; meaning is not captured at all."""
    counts = [0.0] * dimensions
    for word in _WORD.findall(text.lower()):
        slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:8], "big") % dimensions
        counts[slot] += 1.0
    norm = math.sqrt(sum(c * c for c in counts)) or 1.0
    return tuple(c / norm for c in counts)


@dataclass
class HashingEmbedClient:
    """A deterministic embedding client for tests: no network, no key. Usage reports one
    token per word. `fail_with` errors are raised first, one per call, to test retries."""

    dimensions: int
    fail_with: list[ClientError] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def embed(self, texts: Sequence[str], model: str, timeout_s: float) -> EmbedReply:
        self.calls.append(tuple(texts))
        if self.fail_with:
            raise self.fail_with.pop(0)
        words = sum(len(_WORD.findall(t.lower())) for t in texts)
        vectors = tuple(hashing_vector(t, self.dimensions) for t in texts)
        return EmbedReply(vectors, Usage(input_tokens=words, cached_input_tokens=0,
                                         output_tokens=0), model)  # fmt: skip
