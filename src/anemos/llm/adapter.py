"""The LLM adapter (D3): every model call in the project goes through `LLM.structured`
or `LLM.embed` (embeddings, D15), which share one attempt loop (`_attempts`).

`LLM` holds what is the same for every provider: the spend cap (budget.py), one retry
on a transient error, timing, cost from token usage (pricing.py), and one audit row per
attempt, each in its own short transaction (so a call is on record even if the run's
own transaction is later rolled back, D65). The provider is a `ModelClient`:
`OpenAIClient` (openai_client.py) in real runs, `ScriptedClient` (fake.py) in tests.

Per attempt:
1. Reserve the call's maximum cost (a refusal is audited, and nothing is sent).
2. Send it, timing it with a monotonic timer.
3. Settle the actual cost: from the response's usage, or 0 if no tokens were used.
4. Audit: llm.called, llm.failed or llm.refused_budget, with step, model,
   prompt_version, tokens, cost, latency and error; a successful call also stores the
   parsed output (not the prompt, which prompt_version identifies).
A transient error (timeout, connection, rate limit, server error) is retried once after
a short wait; any other error is not retried.
"""

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel

from anemos.audit import AuditEvent, write_llm_audit
from anemos.clock import Clock
from anemos.llm.budget import Budget, BudgetExceeded
from anemos.llm.pricing import Usage, cost, max_cost

type Conn = psycopg.Connection[TupleRow]

# The reasoning efforts the OpenAI SDK accepts (its ReasoningEffort type).
type Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

ATTEMPTS = 2  # the call, plus one retry on a transient error
RETRY_WAIT_S = 2.0
# Upper bound on input tokens: a token covers at least one byte of UTF-8 text, so the
# byte count of everything sent bounds the token count; plus framing per message.
FRAMING_TOKENS = 64


@dataclass(frozen=True)
class Prompt:
    instructions: str  # trusted: ours
    input: str  # may contain untrusted text (an email), already delimited by the caller


@dataclass(frozen=True)
class Settings:
    model: str
    max_output_tokens: int
    reasoning_effort: Effort | None = None  # passed only when set
    timeout_s: float = 30.0


@dataclass(frozen=True)
class Reply[T: BaseModel]:
    """What a client returns: the parsed output and the usage the response reported."""

    output: T
    usage: Usage
    response_model: str  # the model name the response gives (may be a dated snapshot)


@dataclass(frozen=True)
class Result[T: BaseModel]:
    output: T
    model: str
    usage: Usage
    cost_micro_usd: int
    latency_ms: int
    attempts: int
    response_model: str = ""  # the model name the response gives (may be a dated snapshot)


class ClientError(Exception):
    """A failed attempt. `usage` is set if the response reported tokens anyway (e.g. a
    refusal), so the budget is settled with the real cost. `cost_unknown`: a response
    arrived but its usage could not be read, so the budget keeps the reserved maximum.
    `request_refused`: the API refused the request itself (a 4xx other than 429: a bad
    schema, an unsupported setting, a bad key), so sending it again cannot help."""

    transient = False

    def __init__(
        self,
        message: str,
        usage: Usage | None = None,
        *,
        cost_unknown: bool = False,
        request_refused: bool = False,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.cost_unknown = cost_unknown
        self.request_refused = request_refused


class TransientError(ClientError):
    """Worth one retry: timeout, connection error, rate limit, server error."""

    transient = True


class PermanentError(ClientError):
    """Not worth a retry: bad request, authentication, output that does not parse."""


class LLMError(Exception):
    """The call failed for good (after the retry, if any); already audited."""


class ModelClient(Protocol):
    def send[T: BaseModel](self, prompt: Prompt, schema: type[T], settings: Settings) -> Reply[T]:
        """One attempt. Raises TransientError or PermanentError."""
        ...


type Vector = tuple[float, ...]


@dataclass(frozen=True)
class EmbedReply:
    """What an embedding client returns: one vector per text, in order."""

    vectors: tuple[Vector, ...]
    usage: Usage
    response_model: str


@dataclass(frozen=True)
class EmbedResult:
    vectors: tuple[Vector, ...]
    model: str
    usage: Usage
    cost_micro_usd: int
    latency_ms: int
    attempts: int
    response_model: str


class EmbedClient(Protocol):
    def embed(self, texts: Sequence[str], model: str, timeout_s: float) -> EmbedReply:
        """One attempt. Raises TransientError or PermanentError."""
        ...


def max_embed_input_tokens(texts: Sequence[str]) -> int:
    """Same bound as for a prompt: bytes of each text, plus framing per text."""
    return sum(len(t.encode("utf-8")) + FRAMING_TOKENS for t in texts)


def max_input_tokens(prompt: Prompt, schema: type[BaseModel]) -> int:
    schema_text = json.dumps(schema.model_json_schema())
    sent = prompt.instructions + prompt.input + schema_text
    return len(sent.encode("utf-8")) + FRAMING_TOKENS


class LLM:
    def __init__(
        self,
        client: ModelClient,
        settings: Settings,
        budget: Budget,
        connect: Callable[[], Conn] | None,
        clock: Clock,
        *,
        sleep: Callable[[float], None] = time.sleep,
        timer: Callable[[], float] = time.monotonic,
        embed_client: EmbedClient | None = None,
    ) -> None:
        self.client = client
        self.embed_client = embed_client
        self.settings = settings
        self.budget = budget
        self.connect = connect
        self.clock = clock
        self.sleep = sleep
        self.timer = timer

    def structured[T: BaseModel](
        self,
        prompt: Prompt,
        schema: type[T],
        *,
        step: str,
        prompt_version: str,
        email_id: int | None = None,
        settings: Settings | None = None,
    ) -> Result[T]:
        """`settings` replaces the adapter's defaults for this call (a prompt carries its
        own output limit and reasoning effort)."""
        settings = settings or self.settings
        model = settings.model
        bound = max_cost(model, max_input_tokens=max_input_tokens(prompt, schema),
                         max_output_tokens=settings.max_output_tokens)  # fmt: skip
        call = _Call(email_id, step, model, prompt_version)
        reply, spent, latency, attempt = self._attempts(
            call, bound, lambda: self.client.send(prompt, schema, settings),
            usage_of=lambda r: r.usage,
            output_of=lambda r: {
                "response_model": r.response_model,
                "cached_input_tokens": r.usage.cached_input_tokens,
                "reasoning_tokens": r.usage.reasoning_tokens,
                # What the model returned, for monitoring and debugging (week 8); the
                # prompt itself is not stored: prompt_version identifies it.
                "result": r.output.model_dump(mode="json"),
            },
        )  # fmt: skip
        return Result(reply.output, model, reply.usage, spent, latency, attempt,
                      reply.response_model)  # fmt: skip

    def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
        dimensions: int,
        step: str,
        input_version: str,
        email_id: int | None = None,
        timeout_s: float = 30.0,
    ) -> EmbedResult:
        """One vector per text, through the same budget, retry and audit as `structured`.
        `input_version` names what was embedded (e.g. `policies@<hash>`), stored where a
        prompt's version goes. A reply with the wrong number of vectors or dimensions is
        a permanent failure, billed at its usage: a vector of another size could not be
        compared with the stored ones (D15)."""
        if self.embed_client is None:
            raise LLMError(f"{step}: no embedding client configured")
        if not texts:
            raise ValueError("nothing to embed")
        client = self.embed_client
        bound = max_cost(model, max_input_tokens=max_embed_input_tokens(texts),
                         max_output_tokens=0)  # fmt: skip

        def send() -> EmbedReply:
            reply = client.embed(texts, model, timeout_s)
            sizes = {len(v) for v in reply.vectors}
            if len(reply.vectors) != len(texts) or sizes - {dimensions}:
                raise PermanentError(
                    f"expected {len(texts)} vectors of {dimensions} dimensions, got "
                    f"{len(reply.vectors)} of {sorted(sizes)}", reply.usage,
                )  # fmt: skip
            return reply

        reply, spent, latency, attempt = self._attempts(
            _Call(email_id, step, model, input_version), bound, send,
            usage_of=lambda r: r.usage,
            output_of=lambda r: {"response_model": r.response_model, "count": len(r.vectors),
                                 "dimensions": dimensions},
        )  # fmt: skip
        return EmbedResult(reply.vectors, model, reply.usage, spent, latency, attempt,
                           reply.response_model)  # fmt: skip

    def _attempts[R](
        self,
        call: "_Call",
        bound: int,
        send: Callable[[], R],
        *,
        usage_of: Callable[[R], Usage],
        output_of: Callable[[R], dict[str, object]],
    ) -> tuple[R, int, int, int]:
        """The attempt loop every call shares (module docstring, steps 1-4). Returns the
        reply, its cost, its latency and the attempt number."""
        model = call.model
        for attempt in range(1, ATTEMPTS + 1):
            try:
                reservation = self.budget.reserve(model, bound)
            except BudgetExceeded as exc:
                self._audit(call, AuditEvent.LLM_REFUSED_BUDGET, error=str(exc))
                raise
            start = self.timer()
            try:
                reply = send()
            except ClientError as exc:
                latency = self._ms(start)
                if exc.usage is not None:
                    spent = cost(model, exc.usage)
                else:
                    spent = reservation.micro_usd if exc.cost_unknown else 0
                self.budget.settle(reservation, spent)
                self._audit(
                    call, AuditEvent.LLM_FAILED, latency_ms=latency, cost_micro_usd=spent,
                    input_tokens=exc.usage.input_tokens if exc.usage else None,
                    output_tokens=exc.usage.output_tokens if exc.usage else None,
                    error=f"{type(exc).__name__}: {exc}", output={"attempt": attempt},
                )  # fmt: skip
                if exc.transient and attempt < ATTEMPTS:
                    self.sleep(RETRY_WAIT_S)
                    continue
                raise LLMError(f"{call.step}: {type(exc).__name__}: {exc}") from exc
            except BaseException as exc:  # a bug: record it, then let it surface
                self.budget.settle(reservation, 0)
                self._audit(
                    call, AuditEvent.LLM_FAILED, latency_ms=self._ms(start),
                    error=f"{type(exc).__name__}: {exc}", output={"attempt": attempt},
                )  # fmt: skip
                raise
            latency = self._ms(start)
            usage = usage_of(reply)
            spent = cost(model, usage)
            self.budget.settle(reservation, spent)
            self._audit(
                call, AuditEvent.LLM_CALLED, latency_ms=latency, cost_micro_usd=spent,
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                output={"attempt": attempt, **output_of(reply)},
            )  # fmt: skip
            return reply, spent, latency, attempt
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _ms(self, start: float) -> int:
        return round((self.timer() - start) * 1000)

    def _audit(
        self,
        call: "_Call",
        event: AuditEvent,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_micro_usd: int | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
        output: dict[str, object] | None = None,
    ) -> None:
        """One row in its own short transaction, on its own connection. No connection
        (an evaluation run, whose results file is its record): no row."""
        if self.connect is None:
            return
        with self.connect() as conn:
            write_llm_audit(
                conn, self.clock.now(), event, email_id=call.email_id, step=call.step,
                model=call.model, prompt_version=call.prompt_version,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cost_micro_usd=cost_micro_usd, latency_ms=latency_ms, error=error,
                output=output,
            )  # fmt: skip
            conn.commit()


@dataclass(frozen=True)
class _Call:
    """What every audit row of one call shares."""

    email_id: int | None
    step: str
    model: str
    prompt_version: str
