"""The OpenAI implementation of `ModelClient` and `EmbedClient` (D3: the plain `openai` SDK).

- Responses API with structured outputs: the pydantic schema is sent as a strict JSON
  schema (the SDK's own converter), and the reply is parsed here, not by the SDK. The
  SDK's `parse` tries to parse a cut-off reply before returning it, which loses both the
  reason (incomplete: max_output_tokens) and the usage; parsing here keeps both.
- Tool turns (D11): strict function tools, `tool_choice="required"`, no parallel calls;
  the whole transcript is sent every turn (no `previous_response_id`), and the reply
  must hold exactly one function call. Its arguments are checked by the adapter.
- `store=False`: OpenAI does not keep the request or response.
- The SDK's own retries are off (`max_retries=0`); the adapter retries once, so every
  attempt is budgeted and audited.
- The API key (OPENAI_API_KEY) is read when the first call is made, never at import or
  construction, so tests and CI never need one.
- Errors are sorted: timeout, connection, 429 and 5xx are transient; any other status,
  a refusal, an incomplete or unparsable reply is permanent. A reply that arrived is
  billed at its reported usage; only a reply without readable usage keeps the reserved
  maximum (cost_unknown).
"""

import os
from collections.abc import Sequence

import httpx2
import openai
import pydantic
from openai import Omit, OpenAI, omit
from openai.lib._pydantic import to_strict_json_schema  # the SDK's own strict converter
from openai.types.responses import (
    FunctionToolParam,
    Response,
    ResponseFunctionToolCall,
    ResponseInputParam,
    ResponseTextConfigParam,
)
from openai.types.shared_params import Reasoning
from pydantic import BaseModel

from anemos.llm.adapter import (
    EmbedReply,
    IncompleteReply,
    PermanentError,
    Prompt,
    Reply,
    Settings,
    Tool,
    ToolCall,
    ToolOutput,
    Transcript,
    TransientError,
    TurnReply,
    UserText,
)
from anemos.llm.pricing import Usage


def text_format(schema: type[BaseModel]) -> ResponseTextConfigParam:
    """The `text.format` request field: a strict JSON schema named after the model."""
    return {
        "format": {
            "type": "json_schema",
            "name": schema.__name__,
            "schema": to_strict_json_schema(schema),
            "strict": True,
        }
    }


class OpenAIClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        http_client: httpx2.Client | None = None,  # tests: a simulated endpoint
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._http_client = http_client
        self._client: OpenAI | None = None

    def _openai(self) -> OpenAI:
        if self._client is None:
            key = self._api_key or os.environ.get("OPENAI_API_KEY", "").strip()
            if not key:
                raise PermanentError("OPENAI_API_KEY is not set")
            self._client = OpenAI(
                api_key=key, base_url=self._base_url, max_retries=0,
                http_client=self._http_client,
            )  # fmt: skip
        return self._client

    def send[T: BaseModel](self, prompt: Prompt, schema: type[T], settings: Settings) -> Reply[T]:
        client = self._openai()
        reasoning: Reasoning | Omit = (
            Reasoning(effort=settings.reasoning_effort) if settings.reasoning_effort else omit
        )
        try:
            response = client.responses.create(
                model=settings.model,
                instructions=prompt.instructions,
                input=prompt.input,
                text=text_format(schema),
                max_output_tokens=settings.max_output_tokens,
                store=False,
                timeout=settings.timeout_s,
                reasoning=reasoning,
            )
        except openai.APIConnectionError as exc:  # includes APITimeoutError
            raise TransientError(f"{type(exc).__name__}: {exc}") from exc
        except openai.APIStatusError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                raise TransientError(f"HTTP {exc.status_code}: {exc.message}") from exc
            raise PermanentError(
                f"HTTP {exc.status_code}: {exc.message}", request_refused=True
            ) from exc
        except (openai.APIResponseValidationError, pydantic.ValidationError, ValueError) as exc:
            # A reply arrived, but it could not be read at all: tokens spent, amount unknown.
            raise PermanentError(f"unreadable reply: {exc}", cost_unknown=True) from exc
        return _read(response, schema)

    def send_turn(
        self, transcript: Transcript, tools: Sequence[Tool], settings: Settings
    ) -> TurnReply:
        """The whole transcript every turn (no previous_response_id), tools first in the
        request, so a repeated prefix gets the cached price."""
        client = self._openai()
        reasoning: Reasoning | Omit = (
            Reasoning(effort=settings.reasoning_effort) if settings.reasoning_effort else omit
        )
        try:
            response = client.responses.create(
                model=settings.model,
                instructions=transcript.instructions,
                input=input_items(transcript),
                tools=[function_tool(t) for t in tools],
                tool_choice="required",
                parallel_tool_calls=False,
                max_output_tokens=settings.max_output_tokens,
                store=False,
                timeout=settings.timeout_s,
                reasoning=reasoning,
            )
        except openai.APIConnectionError as exc:
            raise TransientError(f"{type(exc).__name__}: {exc}") from exc
        except openai.APIStatusError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                raise TransientError(f"HTTP {exc.status_code}: {exc.message}") from exc
            raise PermanentError(
                f"HTTP {exc.status_code}: {exc.message}", request_refused=True
            ) from exc
        except (openai.APIResponseValidationError, pydantic.ValidationError, ValueError) as exc:
            raise PermanentError(f"unreadable reply: {exc}", cost_unknown=True) from exc
        return _read_turn(response)

    def embed(self, texts: Sequence[str], model: str, timeout_s: float) -> EmbedReply:
        """Embeddings endpoint, default dimensions; vectors put back in input order."""
        client = self._openai()
        try:
            response = client.embeddings.create(
                model=model, input=list(texts), encoding_format="float", timeout=timeout_s
            )
        except openai.APIConnectionError as exc:
            raise TransientError(f"{type(exc).__name__}: {exc}") from exc
        except openai.APIStatusError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                raise TransientError(f"HTTP {exc.status_code}: {exc.message}") from exc
            raise PermanentError(
                f"HTTP {exc.status_code}: {exc.message}", request_refused=True
            ) from exc
        except (openai.APIResponseValidationError, pydantic.ValidationError, ValueError) as exc:
            raise PermanentError(f"unreadable reply: {exc}", cost_unknown=True) from exc
        usage = Usage(input_tokens=response.usage.prompt_tokens, cached_input_tokens=0,
                      output_tokens=0)  # fmt: skip
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = tuple(tuple(float(x) for x in item.embedding) for item in ordered)
        return EmbedReply(vectors, usage, response.model)


def function_tool(tool: Tool) -> FunctionToolParam:
    """A tool as sent: a strict function whose parameters are the SDK's strict schema."""
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": to_strict_json_schema(tool.args),
        "strict": True,
    }


def input_items(transcript: Transcript) -> ResponseInputParam:
    """The whole transcript as input items, in order (we keep it, not the provider)."""
    items: ResponseInputParam = []
    for item in transcript.items:
        match item:
            case UserText(text):
                items.append({"role": "user", "content": text})
            case ToolCall(call_id, name, arguments):
                items.append({"type": "function_call", "call_id": call_id, "name": name,
                              "arguments": arguments})  # fmt: skip
            case ToolOutput(call_id, output):
                items.append({"type": "function_call_output", "call_id": call_id,
                              "output": output})  # fmt: skip
    return items


def _checked(response: Response) -> Usage:
    """The usage, once the reply is known to be complete and not a refusal."""
    if response.usage is None:
        raise PermanentError("reply without usage", cost_unknown=True)
    usage = Usage(
        input_tokens=response.usage.input_tokens,
        cached_input_tokens=response.usage.input_tokens_details.cached_tokens,
        output_tokens=response.usage.output_tokens,
        reasoning_tokens=response.usage.output_tokens_details.reasoning_tokens or 0,
    )
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        raise IncompleteReply(reason, usage)
    if response.status != "completed":
        raise PermanentError(f"reply {response.status}", usage)
    for item in response.output:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                raise PermanentError(f"refusal: {part.refusal}", usage)
    return usage


def _read_turn(response: Response) -> TurnReply:
    """Exactly one function call; anything else (text only, several calls) is permanent.
    Reasoning items are not kept: effort "none" makes none, and with store=False they
    could not be sent back."""
    usage = _checked(response)
    calls = [item for item in response.output if isinstance(item, ResponseFunctionToolCall)]
    if len(calls) != 1:
        raise PermanentError(f"expected exactly one tool call, got {len(calls)}", usage)
    [call] = calls
    return TurnReply(ToolCall(call.call_id, call.name, call.arguments), usage, response.model)


def _read[T: BaseModel](response: Response, schema: type[T]) -> Reply[T]:
    usage = _checked(response)
    try:
        output = schema.model_validate_json(response.output_text)
    except pydantic.ValidationError as exc:
        raise PermanentError(f"unparsable reply: {exc}", usage) from exc
    return Reply(output, usage, response.model)
