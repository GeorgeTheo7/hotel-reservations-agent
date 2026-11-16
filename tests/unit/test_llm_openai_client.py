"""OpenAIClient against a simulated Responses API (httpx2.MockTransport): no network.

The base URL is on the reserved `.invalid` domain, so a request that escaped the mock
could not reach anyone.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from anemos.clock import FixedClock
from anemos.llm.adapter import (
    LLM,
    IncompleteReply,
    PermanentError,
    Prompt,
    Settings,
    Tool,
    ToolCall,
    ToolOutput,
    Transcript,
    TransientError,
    UserText,
    max_turn_input_tokens,
)
from anemos.llm.budget import Budget
from anemos.llm.openai_client import OpenAIClient
from anemos.llm.pricing import Usage

pytestmark = pytest.mark.unit

SETTINGS = Settings(model="gpt-6-luna", max_output_tokens=400)
PROMPT = Prompt(instructions="Extract.", input="<email>Καλησπέρα</email>")
BASE_URL = "http://openai.invalid/v1"

type Handler = Callable[[httpx2.Request], httpx2.Response]


class Out(BaseModel):
    adults: int


def reply(
    text: str = '{"adults": 2}', *, status: str = "completed", **extra: Any
) -> dict[str, Any]:
    return {
        "id": "resp_1", "object": "response", "created_at": 1, "status": status,
        "model": "gpt-6-luna-2026-10-01", "parallel_tool_calls": False,
        "tool_choice": "auto", "tools": [],
        "output": [{"type": "message", "id": "msg_1", "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "usage": {"input_tokens": 120, "input_tokens_details": {"cached_tokens": 20},
                  "output_tokens": 30, "output_tokens_details": {"reasoning_tokens": 4},
                  "total_tokens": 150},
    } | extra  # fmt: skip


def client(handler: Handler) -> OpenAIClient:
    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return OpenAIClient(api_key="test-key", base_url=BASE_URL, http_client=http)


def answering(*responses: httpx2.Response | Exception) -> tuple[Handler, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []
    queue = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler, seen


def test_a_reply_is_parsed_with_its_usage() -> None:
    handler, seen = answering(httpx2.Response(200, json=reply()))
    result = client(handler).send(PROMPT, Out, SETTINGS)
    assert result.output == Out(adults=2)
    assert result.usage == Usage(
        input_tokens=120, cached_input_tokens=20, output_tokens=30, reasoning_tokens=4
    )
    assert result.response_model == "gpt-6-luna-2026-10-01"
    [request] = seen
    body = json.loads(request.content)
    assert request.url == "http://openai.invalid/v1/responses"
    assert request.headers["authorization"] == "Bearer test-key"
    assert (body["model"], body["instructions"], body["input"]) == (
        "gpt-6-luna", "Extract.", "<email>Καλησπέρα</email>",
    )  # fmt: skip
    assert (body["store"], body["max_output_tokens"]) == (False, 400)
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert "reasoning" not in body  # only sent when set


def test_the_reasoning_effort_is_sent_when_set() -> None:
    handler, seen = answering(httpx2.Response(200, json=reply()))
    settings = Settings(model="gpt-6-luna", max_output_tokens=400, reasoning_effort="minimal")
    client(handler).send(PROMPT, Out, settings)
    assert json.loads(seen[0].content)["reasoning"] == {"effort": "minimal"}


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limits_and_server_errors_are_transient(status: int) -> None:
    handler, _ = answering(httpx2.Response(status, json={"error": {"message": "busy"}}))
    with pytest.raises(TransientError, match=f"HTTP {status}") as error:
        client(handler).send(PROMPT, Out, SETTINGS)
    assert error.value.request_refused is False


@pytest.mark.parametrize("status", [400, 401, 404])
def test_other_statuses_are_permanent(status: int) -> None:
    handler, _ = answering(httpx2.Response(status, json={"error": {"message": "no"}}))
    with pytest.raises(PermanentError, match=f"HTTP {status}") as error:
        client(handler).send(PROMPT, Out, SETTINGS)
    assert (error.value.usage, error.value.cost_unknown) == (None, False)
    assert error.value.request_refused is True  # the request itself: never worth repeating


@pytest.mark.parametrize(
    "failure",
    [httpx2.ReadTimeout("timed out"), httpx2.ConnectError("refused")],
    ids=["timeout", "connection"],
)
def test_timeouts_and_connection_errors_are_transient(failure: Exception) -> None:
    handler, _ = answering(failure)
    with pytest.raises(TransientError):
        client(handler).send(PROMPT, Out, SETTINGS)


def test_an_incomplete_reply_is_permanent_and_billed() -> None:
    handler, _ = answering(httpx2.Response(200, json=reply(
        status="incomplete", incomplete_details={"reason": "max_output_tokens"})))  # fmt: skip
    with pytest.raises(PermanentError, match="incomplete: max_output_tokens") as error:
        client(handler).send(PROMPT, Out, SETTINGS)
    assert error.value.request_refused is False  # the reply, not the request
    assert error.value.usage == Usage(120, 20, 30, 4)


def test_output_that_does_not_fit_the_schema_is_permanent_and_billed() -> None:
    """The reply is parsed here, after its usage is read, so it is billed at its real
    cost (not the reserved maximum)."""
    handler, _ = answering(httpx2.Response(200, json=reply('{"adults": "two"}')))
    with pytest.raises(PermanentError, match="unparsable reply") as error:
        client(handler).send(PROMPT, Out, SETTINGS)
    assert (error.value.usage, error.value.cost_unknown) == (Usage(120, 20, 30, 4), False)


def test_a_reply_without_usage_has_an_unknown_cost() -> None:
    no_usage = reply()
    no_usage["usage"] = None
    handler, _ = answering(httpx2.Response(200, json=no_usage))
    with pytest.raises(PermanentError, match="without usage") as error:
        client(handler).send(PROMPT, Out, SETTINGS)
    assert error.value.cost_unknown is True


def test_a_refusal_is_permanent_and_billed() -> None:
    refusal = reply()
    refusal["output"][0]["content"] = [{"type": "refusal", "refusal": "I can't help."}]
    handler, _ = answering(httpx2.Response(200, json=refusal))
    with pytest.raises(PermanentError, match="refusal: I can't help") as error:
        client(handler).send(PROMPT, Out, SETTINGS)
    assert error.value.usage == Usage(120, 20, 30, 4)


def test_no_key_is_needed_until_the_first_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    no_key = OpenAIClient(base_url=BASE_URL)  # constructing is fine without a key
    with pytest.raises(PermanentError, match="OPENAI_API_KEY is not set"):
        no_key.send(PROMPT, Out, SETTINGS)


def test_the_adapter_retries_a_503_through_the_real_client(tmp_path: Path) -> None:
    """End to end without a network: 503, then a reply; the SDK's own retries are off,
    so exactly two requests are made, both budgeted."""
    handler, seen = answering(
        httpx2.Response(503, json={"error": {"message": "busy"}}),
        httpx2.Response(200, json=reply()),
    )
    clock = FixedClock(datetime(2026, 11, 16, tzinfo=UTC))
    budget = Budget(clock, 1_000_000, tmp_path / "llm_spend.jsonl")
    rows: list[Any] = []

    class Conn:
        def __enter__(self) -> "Conn":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, query: str, params: tuple[Any, ...]) -> None:
            rows.append(params[1])

        def commit(self) -> None:
            return None

    llm = LLM(client(handler), SETTINGS, budget, Conn, clock, sleep=lambda s: None)  # type: ignore[arg-type]
    result = llm.structured(PROMPT, Out, step="extract", prompt_version="v@1")
    assert (result.output, result.attempts, len(seen)) == (Out(adults=2), 2, 2)
    assert rows == ["llm.failed", "llm.called"]
    assert budget.open_reservations() == []


# --- embeddings (D15) ---------------------------------------------------------------------


def embedding_reply(*vectors: list[float], tokens: int = 7) -> dict[str, Any]:
    """Items deliberately out of order: the client must put them back by index."""
    data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
    return {"object": "list", "data": list(reversed(data)), "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}  # fmt: skip


def test_embeddings_come_back_in_input_order_with_their_usage() -> None:
    handler, seen = answering(httpx2.Response(200, json=embedding_reply([1.0, 0.0], [0.0, 1.0])))
    reply_ = client(handler).embed(["wifi", "parking"], "text-embedding-3-small", 5.0)
    assert reply_.vectors == ((1.0, 0.0), (0.0, 1.0))
    assert reply_.usage == Usage(input_tokens=7, cached_input_tokens=0, output_tokens=0)
    assert reply_.response_model == "text-embedding-3-small"
    [request] = seen
    body = json.loads(request.content)
    assert request.url.path.endswith("/embeddings")
    assert body == {"model": "text-embedding-3-small", "input": ["wifi", "parking"],
                    "encoding_format": "float"}  # fmt: skip


@pytest.mark.parametrize(
    ("status", "error"), [(429, TransientError), (503, TransientError), (400, PermanentError)]
)
def test_embedding_errors_are_sorted_like_responses(status: int, error: type[Exception]) -> None:
    handler, _ = answering(httpx2.Response(status, json={"error": {"message": "no"}}))
    with pytest.raises(error):
        client(handler).embed(["wifi"], "text-embedding-3-small", 5.0)


# --- tool turns (D11) ----------------------------------------------------------------------


class Draft(BaseModel):
    """Write the reply."""

    text: str
    policy_ids: list[str]
    note: str | None


TOOLS = (Tool("write_draft", "Write the reply.", Draft),)
TRANSCRIPT = Transcript("You are the agent.", (
    UserText("<email>Καλησπέρα</email>"),
    ToolCall("call_1", "search_policies", '{"query": "pets"}'),
    ToolOutput("call_1", "pets: no pets, assistance dogs welcome"),
))  # fmt: skip
CALL = {"type": "function_call", "id": "fc_1", "call_id": "call_2", "name": "write_draft",
        "arguments": '{"text":"Γεια","policy_ids":["pets"],"note":null}',
        "status": "completed"}  # fmt: skip
REASONING = {"type": "reasoning", "id": "rs_1", "summary": []}


def turn_reply(*output: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return reply() | {"output": list(output)} | extra


def test_a_turn_sends_the_whole_transcript_and_strict_tools() -> None:
    handler, seen = answering(httpx2.Response(200, json=turn_reply(CALL)))
    result = client(handler).send_turn(TRANSCRIPT, TOOLS, SETTINGS)
    assert result.call == ToolCall("call_2", "write_draft", CALL["arguments"])
    assert (result.usage, result.response_model) == (Usage(120, 20, 30, 4), "gpt-6-luna-2026-10-01")
    body = json.loads(seen[0].content)
    assert (body["tool_choice"], body["parallel_tool_calls"], body["store"]) == (
        "required",
        False,
        False,
    )
    assert "previous_response_id" not in body
    assert body["instructions"] == "You are the agent."
    assert body["input"] == [
        {"role": "user", "content": "<email>Καλησπέρα</email>"},
        {"type": "function_call", "call_id": "call_1", "name": "search_policies",
         "arguments": '{"query": "pets"}'},
        {"type": "function_call_output", "call_id": "call_1",
         "output": "pets: no pets, assistance dogs welcome"},
    ]  # fmt: skip
    [tool] = body["tools"]
    assert (tool["type"], tool["name"], tool["strict"]) == ("function", "write_draft", True)
    assert tool["parameters"]["additionalProperties"] is False
    assert tool["parameters"]["required"] == ["text", "policy_ids", "note"]


def test_the_turn_bound_covers_what_is_sent() -> None:
    """The adapter's bound is computed from pydantic's schema; the strict schema as sent
    is longer, so check the bound against the request's own bytes."""
    handler, seen = answering(httpx2.Response(200, json=turn_reply(CALL)))
    client(handler).send_turn(TRANSCRIPT, TOOLS, SETTINGS)
    body = json.loads(seen[0].content)
    sent = json.dumps([body["instructions"], body["input"], body["tools"]], ensure_ascii=False)
    assert max_turn_input_tokens(TRANSCRIPT, TOOLS) >= len(sent.encode("utf-8"))


def test_a_reasoning_item_beside_the_call_is_ignored() -> None:
    handler, _ = answering(httpx2.Response(200, json=turn_reply(REASONING, CALL)))
    assert client(handler).send_turn(TRANSCRIPT, TOOLS, SETTINGS).call.call_id == "call_2"


@pytest.mark.parametrize("output", [[], [CALL, CALL | {"id": "fc_2", "call_id": "call_3"}]],
                         ids=["no_call", "two_calls"])  # fmt: skip
def test_anything_but_exactly_one_call_is_permanent_and_billed(output: list[Any]) -> None:
    handler, _ = answering(httpx2.Response(200, json=turn_reply(*output)))
    with pytest.raises(PermanentError, match="exactly one tool call") as error:
        client(handler).send_turn(TRANSCRIPT, TOOLS, SETTINGS)
    assert error.value.usage == Usage(120, 20, 30, 4)


def test_a_turn_cut_off_at_the_output_limit_is_an_incomplete_reply() -> None:
    handler, _ = answering(httpx2.Response(200, json=turn_reply(
        CALL | {"arguments": '{"text":"Γει'}, status="incomplete",
        incomplete_details={"reason": "max_output_tokens"})))  # fmt: skip
    with pytest.raises(IncompleteReply) as error:
        client(handler).send_turn(TRANSCRIPT, TOOLS, SETTINGS)
    assert (error.value.reason, error.value.usage) == ("max_output_tokens", Usage(120, 20, 30, 4))
