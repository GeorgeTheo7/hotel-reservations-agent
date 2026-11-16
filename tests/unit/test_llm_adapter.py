"""The LLM adapter (D3) with the scripted client: budget, one retry, timing, audit.

No database: audit rows go to a recording stand-in for a connection (the real SQL is
covered by tests/integration/test_llm_audit.py).
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import pytest
from pydantic import BaseModel, Field

from anemos.clock import FixedClock
from anemos.llm.adapter import (
    LLM,
    IncompleteReply,
    InvalidArguments,
    LLMError,
    PermanentError,
    Prompt,
    ReplyCutOff,
    Settings,
    Tool,
    ToolCall,
    ToolOutput,
    Transcript,
    TransientError,
    UnknownTool,
    UserText,
    flat_schema_problems,
    max_embed_input_tokens,
    max_input_tokens,
    max_turn_input_tokens,
)
from anemos.llm.budget import Budget, BudgetExceeded
from anemos.llm.fake import (
    Answer,
    HashingEmbedClient,
    OnTranscript,
    ScriptedClient,
    ToolCallStep,
)
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


# --- tool turns (D11) ----------------------------------------------------------------------


class Quote(BaseModel):
    """Price the stored request."""


class Draft(BaseModel):
    text: str
    offer_quote_ids: list[int]
    note: str | None  # the one anyOf allowed: X | None


TOOLS = (Tool("quote_price", "Price the request.", Quote),
         Tool("write_draft", "Write the reply.", Draft))  # fmt: skip
TRANSCRIPT = Transcript("You are the agent.", (UserText("<email>Καλησπέρα</email>"),))
DRAFT = {"text": "Αγαπητή Ελένη, ευχαριστούμε.", "offer_quote_ids": [3], "note": None}


def turn(llm: LLM, transcript: Transcript = TRANSCRIPT) -> Any:
    return llm.tool_turn(transcript, TOOLS, step="agent", prompt_version="agent_v1@abc",
                         email_id=7)  # fmt: skip


def test_a_turn_returns_one_validated_call(budget: Budget, audit: Recorder) -> None:
    client = ScriptedClient.of([ToolCallStep("write_draft", DRAFT, "call_a", USAGE)])
    result = turn(make(client, budget, audit)[0])
    assert result.call == ToolCall("call_a", "write_draft", json.dumps(DRAFT, ensure_ascii=False))
    assert (result.tool, result.args) == (TOOLS[1], Draft.model_validate(DRAFT))
    assert (result.attempts, result.cost_micro_usd) == (1, cost("gpt-6-luna", USAGE))
    assert budget.spent() == result.cost_micro_usd and budget.open_reservations() == []
    [row] = audit.rows
    assert (row["event"], row["email_id"], row["step"], row["prompt_version"]) == (
        "llm.called", 7, "agent", "agent_v1@abc")  # fmt: skip
    assert {k: row["output"][k] for k in ("tool", "call_id", "arguments")} == {
        "tool": "write_draft", "call_id": "call_a", "arguments": DRAFT}  # fmt: skip
    [sent] = client.turns
    assert (sent.transcript, sent.tools, sent.settings) == (TRANSCRIPT, TOOLS, SETTINGS)


def test_an_unknown_tool_is_never_a_call(budget: Budget, audit: Recorder) -> None:
    client = ScriptedClient.of([ToolCallStep("hold_room", {"room": 201}, "call_x", USAGE)])
    with pytest.raises(UnknownTool, match="hold_room") as error:
        turn(make(client, budget, audit)[0])
    assert error.value.call == ToolCall("call_x", "hold_room", '{"room": 201}')
    assert len(client.turns) == 1  # not retried
    assert budget.spent() == cost("gpt-6-luna", USAGE)  # billed at its usage
    assert audit.events() == ["llm.failed"]


@pytest.mark.parametrize("arguments", [
    '{"text": "Hi", "offer_quote_ids": [3]',  # cut-off JSON
    {"text": "Hi", "offer_quote_ids": ["three"], "note": None},  # wrong type
    {"text": "Hi", "note": None},  # a field missing
], ids=["broken_json", "wrong_type", "missing_field"])  # fmt: skip
def test_invalid_arguments_are_never_a_call(
    arguments: dict[str, Any] | str, budget: Budget, audit: Recorder
) -> None:
    client = ScriptedClient.of([ToolCallStep("write_draft", arguments, "call_b", USAGE)])
    with pytest.raises(InvalidArguments, match="write_draft") as error:
        turn(make(client, budget, audit)[0])
    assert (error.value.call.call_id, error.value.call.name) == ("call_b", "write_draft")
    assert len(client.turns) == 1
    assert budget.spent() == cost("gpt-6-luna", USAGE)
    assert audit.events() == ["llm.failed"]


def test_a_reply_cut_off_at_the_output_limit_is_typed(budget: Budget, audit: Recorder) -> None:
    client = ScriptedClient.of([IncompleteReply("max_output_tokens", USAGE)])
    with pytest.raises(ReplyCutOff):
        turn(make(client, budget, audit)[0])
    assert len(client.turns) == 1 and budget.spent() == cost("gpt-6-luna", USAGE)


def test_another_incomplete_reply_is_a_plain_failure(budget: Budget, audit: Recorder) -> None:
    client = ScriptedClient.of([IncompleteReply("content_filter", USAGE)])
    with pytest.raises(LLMError) as error:
        turn(make(client, budget, audit)[0])
    assert type(error.value) is LLMError


def test_a_transient_error_in_a_turn_is_retried_once(budget: Budget, audit: Recorder) -> None:
    client = ScriptedClient.of([TransientError("timeout"), ToolCallStep("quote_price", {})])
    llm, slept = make(client, budget, audit)
    assert turn(llm).attempts == 2
    assert audit.events() == ["llm.failed", "llm.called"] and len(slept) == 1


def test_the_turn_reserves_the_bound_over_transcript_and_tools(
    budget: Budget, audit: Recorder
) -> None:
    seen: list[int] = []

    def look(transcript: Transcript) -> ToolCallStep:
        [reservation] = budget.open_reservations()
        seen.append(reservation.micro_usd)
        return ToolCallStep("quote_price", {})

    turn(make(ScriptedClient.of([OnTranscript(look)]), budget, audit)[0])
    bound = max_turn_input_tokens(TRANSCRIPT, TOOLS)
    assert seen == [max_cost("gpt-6-luna", max_input_tokens=bound, max_output_tokens=500)]


def test_the_input_bound_counts_every_tool_schema() -> None:
    without = max_turn_input_tokens(TRANSCRIPT, TOOLS[:1])
    schema = json.dumps(Draft.model_json_schema())
    assert max_turn_input_tokens(TRANSCRIPT, TOOLS) >= without + len(schema.encode("utf-8"))


def test_the_input_bound_counts_the_whole_transcript() -> None:
    output = "Δωμάτιο " * 100
    longer = TRANSCRIPT.then(ToolCall("call_1", "quote_price", "{}"),
                             ToolOutput("call_1", output))  # fmt: skip
    extra = len(("call_1quote_price{}" + "call_1" + output).encode("utf-8"))
    assert max_turn_input_tokens(longer, TOOLS) >= max_turn_input_tokens(TRANSCRIPT, TOOLS) + extra


def test_a_turn_needs_distinct_tool_names(budget: Budget, audit: Recorder) -> None:
    llm = make(ScriptedClient(), budget, audit)[0]
    with pytest.raises(ValueError, match="each name once"):
        llm.tool_turn(TRANSCRIPT, (TOOLS[0], TOOLS[0]), step="agent", prompt_version="v@1")


def test_a_tool_step_can_answer_from_the_transcript(budget: Budget, audit: Recorder) -> None:
    """The gold agent (step 8) picks its next call from earlier tool outputs."""

    def from_output(transcript: Transcript) -> ToolCallStep:
        last = transcript.items[-1]
        assert isinstance(last, ToolOutput)
        return ToolCallStep("write_draft", {"text": "Hi", "offer_quote_ids": [int(last.output)],
                                            "note": None})  # fmt: skip

    client = ScriptedClient.of([OnTranscript(from_output)])
    after = TRANSCRIPT.then(ToolCall("call_1", "quote_price", "{}"), ToolOutput("call_1", "42"))
    result = turn(make(client, budget, audit)[0], after)
    assert result.args == Draft(text="Hi", offer_quote_ids=[42], note=None)


def test_a_structured_call_refuses_a_tool_step_and_a_turn_an_answer() -> None:
    with pytest.raises(AssertionError, match="got a tool step"):
        ScriptedClient.of([ToolCallStep("quote_price", {})]).send(PROMPT, Out, SETTINGS)
    with pytest.raises(AssertionError, match="non-tool step"):
        ScriptedClient.of([Answer({"adults": 2})]).send_turn(TRANSCRIPT, TOOLS, SETTINGS)


# --- flat argument schemas ------------------------------------------------------------------


class Code(BaseModel):
    code: Literal["COMPLAINT", "DISCOUNT"]
    note: Literal["a", "b"] | None
    count: int | None = None
    ids: list[str]


def test_the_x_or_none_any_of_is_flat() -> None:
    """The one anyOf allowed: one plain type (an enum too) and null."""
    assert flat_schema_problems(Draft) == [] and flat_schema_problems(Code) == []
    assert Code.model_json_schema()["properties"]["note"]["anyOf"][1] == {"type": "null"}


class Inner(BaseModel):
    x: int


class TwoTypes(BaseModel):
    v: int | str  # anyOf of two plain types


class ListOrNone(BaseModel):
    v: list[int] | None  # anyOf of an array and null


class Nested(BaseModel):
    v: Inner  # $ref / $defs


class AnObject(BaseModel):
    v: dict[str, int]  # an object


class OneOf(BaseModel):
    v: int = Field(json_schema_extra={"oneOf": [{"type": "integer"}]})


class AllOf(BaseModel):
    v: int = Field(json_schema_extra={"allOf": [{"type": "integer"}]})


@pytest.mark.parametrize("args", [TwoTypes, ListOrNone, Nested, AnObject, OneOf, AllOf])
def test_every_other_shape_is_refused(args: type[BaseModel]) -> None:
    assert flat_schema_problems(args)
    with pytest.raises(ValueError, match="not flat"):
        Tool("t", "d", args)
