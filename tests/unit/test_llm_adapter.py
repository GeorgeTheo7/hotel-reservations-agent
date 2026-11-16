"""The LLM adapter (D3) with the scripted client: budget, one retry, timing, audit.

No database: audit rows go to a recording stand-in for a connection (the real SQL is
covered by tests/integration/test_llm_audit.py).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest
from pydantic import BaseModel

from anemos.clock import FixedClock
from anemos.llm.adapter import (
    LLM,
    LLMError,
    PermanentError,
    Prompt,
    Settings,
    TransientError,
    max_embed_input_tokens,
    max_input_tokens,
)
from anemos.llm.budget import Budget, BudgetExceeded
from anemos.llm.fake import Answer, HashingEmbedClient, ScriptedClient
from anemos.llm.pricing import Usage, cost, max_cost

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=500)
PROMPT = Prompt(instructions="Extract the request.", input="<email>Hello</email>")
USAGE = Usage(input_tokens=1_200, cached_input_tokens=200, output_tokens=300)


class Out(BaseModel):
    adults: int


@dataclass
class Recorder:
    """Stands in for `connect()`: records each audit INSERT and whether it committed."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    committed: int = 0

    def __call__(self) -> Self:
        return self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...]) -> None:
        names = ["at", "event", "email_id", "step", "model", "prompt_version", "input_tokens",
                 "output_tokens", "cost_micro_usd", "latency_ms", "error", "output"]  # fmt: skip
        row = dict(zip(names, params, strict=True))
        row["output"] = row["output"].obj if row["output"] is not None else None
        self.rows.append(row)

    def commit(self) -> None:
        self.committed += 1

    def events(self) -> list[str]:
        return [row["event"] for row in self.rows]


class Ticks:
    """A monotonic timer that advances 0.25 s per reading."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.25
        return self.now


@pytest.fixture
def audit() -> Recorder:
    return Recorder()


@pytest.fixture
def budget(tmp_path: Path) -> Budget:
    return Budget(FixedClock(NOW), 1_000_000, tmp_path / "llm_spend.jsonl")


def make(client: ScriptedClient, budget: Budget, audit: Recorder) -> tuple[LLM, list[float]]:
    slept: list[float] = []
    llm = LLM(client, SETTINGS, budget, audit, FixedClock(NOW), sleep=slept.append, timer=Ticks())  # type: ignore[arg-type]
    return llm, slept


def call(llm: LLM) -> Any:
    return llm.structured(
        PROMPT, Out, step="extract", prompt_version="extraction_v1@abc", email_id=7
    )


# --- success ----------------------------------------------------------------------------


def test_a_call_returns_its_output_cost_and_latency(budget: Budget, audit: Recorder) -> None:
    client = ScriptedClient.of([Answer({"adults": 2}, USAGE)])
    llm, slept = make(client, budget, audit)
    result = call(llm)
    assert result.output == Out(adults=2)
    assert (result.model, result.usage, result.attempts) == ("gpt-6-luna", USAGE, 1)
    assert result.cost_micro_usd == cost("gpt-6-luna", USAGE)
    assert result.latency_ms == 250  # one tick between the two timer readings
    assert budget.spent() == result.cost_micro_usd  # reserved, then settled at the real cost
    assert budget.open_reservations() == []
    assert slept == []
    [row] = audit.rows
    assert {k: row[k] for k in ("event", "email_id", "step", "model", "prompt_version",
                                "input_tokens", "output_tokens", "cost_micro_usd",
                                "latency_ms", "error")} == {
        "event": "llm.called", "email_id": 7, "step": "extract", "model": "gpt-6-luna",
        "prompt_version": "extraction_v1@abc", "input_tokens": 1_200, "output_tokens": 300,
        "cost_micro_usd": result.cost_micro_usd, "latency_ms": 250, "error": None,
    }  # fmt: skip
    assert row["output"] == {"attempt": 1, "response_model": "gpt-6-luna",
                             "cached_input_tokens": 200, "reasoning_tokens": 0,
                             "result": {"adults": 2}}  # fmt: skip
    assert audit.committed == 1  # its own transaction


def test_the_client_gets_the_prompt_schema_and_settings(budget: Budget, audit: Recorder) -> None:
    client = ScriptedClient.of([Answer({"adults": 1})])
    call(make(client, budget, audit)[0])
    [sent] = client.sent
    assert (sent.prompt, sent.schema, sent.settings) == (PROMPT, Out, SETTINGS)


# --- retry ------------------------------------------------------------------------------


def test_a_transient_error_is_retried_once(budget: Budget, audit: Recorder) -> None:
    client = ScriptedClient.of([TransientError("HTTP 503"), Answer({"adults": 2}, USAGE)])
    llm, slept = make(client, budget, audit)
    result = call(llm)
    assert result.attempts == 2
    assert slept == [2.0]
    assert audit.events() == ["llm.failed", "llm.called"]
    assert audit.rows[0]["error"] == "TransientError: HTTP 503"
    assert audit.rows[0]["output"] == {"attempt": 1}
    assert budget.spent() == cost("gpt-6-luna", USAGE)  # the failed attempt cost nothing


def test_two_transient_errors_give_up(budget: Budget, audit: Recorder) -> None:
    client = ScriptedClient.of([TransientError("timeout"), TransientError("timeout")])
    llm, slept = make(client, budget, audit)
    with pytest.raises(LLMError, match="extract: TransientError: timeout"):
        call(llm)
    assert len(client.sent) == 2  # never a third attempt
    assert slept == [2.0]
    assert audit.events() == ["llm.failed", "llm.failed"]
    assert budget.spent() == 0


def test_a_permanent_error_is_not_retried(budget: Budget, audit: Recorder) -> None:
    client = ScriptedClient.of([PermanentError("HTTP 400: bad schema")])
    llm, slept = make(client, budget, audit)
    with pytest.raises(LLMError, match="HTTP 400"):
        call(llm)
    assert (len(client.sent), slept, audit.events()) == (1, [], ["llm.failed"])


def test_a_failed_reply_that_used_tokens_is_billed(budget: Budget, audit: Recorder) -> None:
    """A refusal still reports usage: the budget is settled with the real cost."""
    client = ScriptedClient.of([PermanentError("refusal", USAGE)])
    with pytest.raises(LLMError):
        call(make(client, budget, audit)[0])
    assert budget.spent() == cost("gpt-6-luna", USAGE)
    assert (audit.rows[0]["input_tokens"], audit.rows[0]["cost_micro_usd"]) == (
        1_200, cost("gpt-6-luna", USAGE),
    )  # fmt: skip


def test_a_reply_of_unknown_cost_keeps_the_reserved_maximum(
    budget: Budget, audit: Recorder
) -> None:
    client = ScriptedClient.of([PermanentError("unreadable reply", cost_unknown=True)])
    with pytest.raises(LLMError):
        call(make(client, budget, audit)[0])
    expected = max_cost("gpt-6-luna", max_input_tokens=max_input_tokens(PROMPT, Out),
                        max_output_tokens=500)  # fmt: skip
    assert budget.spent() == expected
    assert budget.open_reservations() == []


def test_an_unexpected_exception_is_recorded_and_raised(budget: Budget, audit: Recorder) -> None:
    def boom(prompt: Prompt) -> Answer:
        raise RuntimeError("bug in the client")

    client = ScriptedClient.of([boom])
    with pytest.raises(RuntimeError, match="bug in the client"):
        call(make(client, budget, audit)[0])
    assert audit.events() == ["llm.failed"]
    assert budget.open_reservations() == []  # settled, not left blocking the cap


# --- budget -----------------------------------------------------------------------------


def test_no_call_is_sent_when_the_budget_cannot_pay(tmp_path: Path, audit: Recorder) -> None:
    poor = Budget(FixedClock(NOW), 10, tmp_path / "llm_spend.jsonl")
    client = ScriptedClient.of([Answer({"adults": 2})])
    with pytest.raises(BudgetExceeded):
        call(make(client, poor, audit)[0])
    assert client.sent == []
    assert audit.events() == ["llm.refused_budget"]
    assert "ANEMOS_LLM_BUDGET_USD" in audit.rows[0]["error"]


def test_the_retry_is_refused_when_the_budget_ran_out(tmp_path: Path, audit: Recorder) -> None:
    """Enough for one attempt's maximum, not two: the first fails having used tokens,
    so the retry cannot be paid for."""
    one = max_cost("gpt-6-luna", max_input_tokens=max_input_tokens(PROMPT, Out),
                   max_output_tokens=500)  # fmt: skip
    tight = Budget(FixedClock(NOW), one + 10, tmp_path / "llm_spend.jsonl")
    big = Usage(input_tokens=10_000, cached_input_tokens=0, output_tokens=0)
    client = ScriptedClient.of([TransientError("HTTP 500", big), Answer({"adults": 2})])
    with pytest.raises(BudgetExceeded):
        call(make(client, tight, audit)[0])
    assert len(client.sent) == 1
    assert audit.events() == ["llm.failed", "llm.refused_budget"]


def test_the_input_bound_counts_bytes_so_greek_text_is_not_underestimated() -> None:
    """Greek letters take two bytes in UTF-8; a token never covers less than one byte."""
    greek = Prompt(instructions="x", input="Καλησπέρα σας" * 50)
    assert max_input_tokens(greek, Out) >= len(greek.input.encode("utf-8"))


# --- the scripted client ----------------------------------------------------------------


def test_the_script_running_out_is_a_test_error() -> None:
    client = ScriptedClient.of([])
    with pytest.raises(AssertionError, match="ran out at call 1"):
        client.send(PROMPT, Out, SETTINGS)


def test_a_step_can_answer_from_the_prompt() -> None:
    client = ScriptedClient.of([lambda p: Answer({"adults": len(p.input)})])
    assert client.send(PROMPT, Out, SETTINGS).output == Out(adults=len(PROMPT.input))


def test_a_scripted_answer_must_fit_the_schema() -> None:
    client = ScriptedClient.of([Answer({"children": 2})])
    with pytest.raises(ValueError):
        client.send(PROMPT, Out, SETTINGS)


# --- embeddings (D15) ---------------------------------------------------------------------

EMBED_MODEL = "text-embedding-3-small"


def make_embedder(embedder: HashingEmbedClient, budget: Budget, audit: Recorder) -> LLM:
    return LLM(ScriptedClient(), SETTINGS, budget, audit, FixedClock(NOW),  # type: ignore[arg-type]
               sleep=lambda _: None, timer=Ticks(), embed_client=embedder)  # fmt: skip


def embed(llm: LLM, texts: list[str], dimensions: int = 8) -> Any:
    return llm.embed(texts, model=EMBED_MODEL, dimensions=dimensions, step="policy_embeddings",
                     input_version="policies@abc")  # fmt: skip


def test_embed_returns_one_vector_per_text_and_settles_the_budget(
    budget: Budget, audit: Recorder
) -> None:
    embedder = HashingEmbedClient(dimensions=8)
    result = embed(make_embedder(embedder, budget, audit), ["breakfast time", "pets allowed"])
    assert len(result.vectors) == 2 and {len(v) for v in result.vectors} == {8}
    assert result.usage == Usage(4, 0, 0)  # one token per word in the fake
    assert result.cost_micro_usd == cost(EMBED_MODEL, Usage(4, 0, 0))
    assert budget.spent() == result.cost_micro_usd and budget.open_reservations() == []
    [row] = audit.rows
    assert (row["event"], row["step"], row["model"], row["prompt_version"]) == (
        "llm.called",
        "policy_embeddings",
        EMBED_MODEL,
        "policies@abc",
    )
    assert row["output"] == {"attempt": 1, "response_model": EMBED_MODEL, "count": 2,
                             "dimensions": 8}  # fmt: skip


def test_embed_reserves_its_maximum_before_the_call(tmp_path: Path, audit: Recorder) -> None:
    """A cap below the bound refuses the call: nothing is sent."""
    texts = ["x" * 1_000]
    bound = max_cost(EMBED_MODEL, max_input_tokens=max_embed_input_tokens(texts),
                     max_output_tokens=0)  # fmt: skip
    small = Budget(FixedClock(NOW), bound - 1, tmp_path / "ledger.jsonl")
    embedder = HashingEmbedClient(dimensions=8)
    with pytest.raises(BudgetExceeded):
        embed(make_embedder(embedder, small, audit), texts)
    assert embedder.calls == [] and audit.events() == ["llm.refused_budget"]


def test_embed_retries_a_transient_error_once(budget: Budget, audit: Recorder) -> None:
    embedder = HashingEmbedClient(dimensions=8, fail_with=[TransientError("timeout")])
    result = embed(make_embedder(embedder, budget, audit), ["wifi"])
    assert result.attempts == 2 and len(embedder.calls) == 2
    assert audit.events() == ["llm.failed", "llm.called"]


def test_embed_refuses_vectors_of_another_size_and_bills_them(
    budget: Budget, audit: Recorder
) -> None:
    """A reply of 8 dimensions when 1536 are expected could not be compared with the
    stored vectors: a permanent failure, billed at its reported usage, not retried."""
    embedder = HashingEmbedClient(dimensions=8)
    with pytest.raises(LLMError, match="1536 dimensions"):
        embed(make_embedder(embedder, budget, audit), ["wifi"], dimensions=1536)
    assert len(embedder.calls) == 1 and audit.events() == ["llm.failed"]
    assert budget.spent() == cost(EMBED_MODEL, Usage(1, 0, 0))


def test_embed_without_an_embedding_client_is_refused(budget: Budget, audit: Recorder) -> None:
    llm, _ = make(ScriptedClient(), budget, audit)
    with pytest.raises(LLMError, match="no embedding client"):
        embed(llm, ["wifi"])
    assert audit.rows == [] and budget.spent() == 0
