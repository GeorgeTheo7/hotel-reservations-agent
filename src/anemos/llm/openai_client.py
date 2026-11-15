"""The OpenAI implementation of `ModelClient` (D3: the plain `openai` SDK).

- Responses API with structured outputs: the pydantic schema is sent as a strict JSON
  schema (the SDK's own converter), and the reply is parsed here, not by the SDK. The
  SDK's `parse` tries to parse a cut-off reply before returning it, which loses both the
  reason (incomplete: max_output_tokens) and the usage; parsing here keeps both.
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

import httpx2
import openai
import pydantic
from openai import Omit, OpenAI, omit
from openai.lib._pydantic import to_strict_json_schema  # the SDK's own strict converter
from openai.types.responses import Response, ResponseTextConfigParam
from openai.types.shared_params import Reasoning
from pydantic import BaseModel

from anemos.llm.adapter import PermanentError, Prompt, Reply, Settings, TransientError
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
            raise PermanentError(f"HTTP {exc.status_code}: {exc.message}") from exc
        except (openai.APIResponseValidationError, pydantic.ValidationError, ValueError) as exc:
            # A reply arrived, but it could not be read at all: tokens spent, amount unknown.
            raise PermanentError(f"unreadable reply: {exc}", cost_unknown=True) from exc
        return _read(response, schema)


def _read[T: BaseModel](response: Response, schema: type[T]) -> Reply[T]:
    if response.usage is None:
        raise PermanentError("reply without usage", cost_unknown=True)
    usage = Usage(
        input_tokens=response.usage.input_tokens,
        cached_input_tokens=response.usage.input_tokens_details.cached_tokens,
        output_tokens=response.usage.output_tokens,
    )
    if response.status != "completed":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        raise PermanentError(f"reply {response.status}: {reason}", usage)
    for item in response.output:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                raise PermanentError(f"refusal: {part.refusal}", usage)
    try:
        output = schema.model_validate_json(response.output_text)
    except pydantic.ValidationError as exc:
        raise PermanentError(f"unparsable reply: {exc}", usage) from exc
    return Reply(output, usage, response.model)
