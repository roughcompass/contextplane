"""Two adapters for the generation seam, over the transport the kit already owns.

E24-T3. Both families in one module rather than two, and the reason is that
almost nothing here is adapter-specific once `adapter_kit` has the transport:
what differs is the auth header, the request envelope, and the three field names
usage arrives under. The extraction adapters are two files because each carries a
long argument about its own vendor; there is no second argument to make here, and
splitting would leave two files that are the same file.

**No vendor SDK, for the reason the extraction adapters give.** The whole surface
used is one POST with a JSON body. A client library would add a dependency, a
retry policy this does not want, and an opinion about which endpoints are
legitimate -- and that last one is exactly what an operator pointing this at an
internal gateway needs it not to have.

**Tool-use, not prose parsing.** `RESPONSE_SCHEMA` is handed over as a tool the
model is required to call, and `tool_choice` names it, so free-form output is
impossible rather than discouraged. A model that answers in prose has failed, and
failing is correct: an answer whose citations were recovered by string-matching
prose against the envelope would be inventing the evidence E24-T13 then reasons
from.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from contextplane.extraction.adapter_kit import (
    OUTCOME_AUTH,
    OUTCOME_MALFORMED,
    OUTCOME_OK,
    OUTCOME_RATE_LIMIT,
    OUTCOME_SERVER,
    OUTCOME_TIMEOUT,
    PROVIDER_DURATION,
    build_token_usage,
    classify_status,
    read_json_capped,
    record_call,
    record_tokens,
)
from contextplane.extraction.provider import ProviderError, ProviderMalformedError, TokenUsage
from contextplane.extraction.response_provider import (
    RESPONSE_SCHEMA,
    RESPONSE_TOOL_NAME,
    Assertion,
    ResponseRequest,
    ResponseResult,
    assemble_response_prompt,
)

_log = logging.getLogger(__name__)

_ANTHROPIC_URL: Final = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION: Final = "2023-06-01"
_ANTHROPIC_AUTH_HEADER: Final = "x-api-key"
_ANTHROPIC_AUTH_TEMPLATE: Final = "{key}"

_OPENAI_URL: Final = "https://api.openai.com/v1/chat/completions"
_OPENAI_AUTH_HEADER: Final = "Authorization"
_OPENAI_AUTH_TEMPLATE: Final = "Bearer {key}"

#: Each adapter's own default model, declared here rather than in configuration.
#: A model id is a property of the provider that has to serve it; naming one
#: vendor's model as a global default is what made the extraction model setting
#: mean "whatever the one adapter used".
ANTHROPIC_DEFAULT_MODEL: Final = "claude-sonnet-5"
OPENAI_DEFAULT_MODEL: Final = "gpt-4o"


class _HttpResponseProvider:
    """What both adapters share: headers built once, one capped streamed POST."""

    provider_id = "unset"
    default_model_id = "unset"

    def __init__(
        self,
        api_key: str,
        *,
        url: str,
        auth_header: str,
        auth_template: str,
        extra_headers: dict[str, str],
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            msg = f"an API key is required to construct the {self.provider_id} response provider"
            raise ValueError(msg)
        self._timeout_s = timeout_s
        self._client = client
        self._url = url
        # Built once, here, so the credential is interpolated in exactly one
        # place. `str.replace`, never `str.format`: a template is operator-
        # supplied, and `format` would treat any other brace in it as a field to
        # expand -- against a value carrying a credential that is an
        # arbitrary-attribute-read, not a string substitution.
        headers = {auth_header: auth_template.replace("{key}", api_key), "content-type": "application/json"}
        headers.update(extra_headers)
        self._headers = headers
        self._log_effective_endpoint()

    def _log_effective_endpoint(self) -> None:
        """Record where a simulation will actually send served context.

        Scheme, host and port only. A gateway URL is operator-supplied and
        routinely carries a token in the path or the query, and this line exists
        to answer "which endpoint is this talking to" -- which the authority
        answers on its own.
        """
        parsed = urlsplit(self._url)
        authority = parsed.hostname or "?"
        if parsed.port is not None:
            authority = f"{authority}:{parsed.port}"
        _log.info("simulation.%s_endpoint: %s://%s", self.provider_id, parsed.scheme, authority)
        if parsed.scheme != "https":
            _log.warning(
                "simulation.%s_insecure_endpoint: %s is not https, so the API key and every "
                "envelope sent for simulation cross the network in the clear. This is only safe "
                "on a loopback or a trusted in-cluster address.",
                self.provider_id,
                parsed.scheme,
            )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send the call and read the body under a cap.

        Streamed, not read whole: a simulation runs inside the tenant-facing API
        process, so a response big enough to exhaust memory takes the API down
        with it.

        `follow_redirects=False`, stated per-request so it holds for an injected
        client too. httpx strips `Authorization` when a redirect crosses origins
        but knows nothing about `x-api-key`, so a compromised gateway answering
        `302` to an address it chooses would be handed the credential.
        """
        try:
            if self._client is not None:
                stream = self._client.stream(
                    "POST",
                    self._url,
                    json=payload,
                    headers=self._headers,
                    timeout=self._timeout_s,
                    follow_redirects=False,
                )
                async with stream as response:
                    return await self._interpret(response)
            async with (
                httpx.AsyncClient(timeout=self._timeout_s, follow_redirects=False) as client,
                client.stream("POST", self._url, json=payload, headers=self._headers) as response,
            ):
                return await self._interpret(response)
        except httpx.TimeoutException as exc:
            record_call(OUTCOME_TIMEOUT, self.provider_id)
            raise ProviderError("request timed out", is_retriable=True) from exc
        except httpx.HTTPError as exc:
            record_call(OUTCOME_SERVER, self.provider_id)
            raise ProviderError(f"transport error: {type(exc).__name__}", is_retriable=True) from exc

    async def _interpret(self, response: httpx.Response) -> dict[str, Any]:
        """Map status to a retriable or terminal failure.

        Response bodies are never included in the error message. An auth failure
        body can echo request material, and the reason string reaches logs.
        """
        outcome, retriable = classify_status(response.status_code)
        if outcome == OUTCOME_OK:
            try:
                return await read_json_capped(response)
            except ProviderMalformedError:
                record_call(OUTCOME_MALFORMED, self.provider_id)
                raise

        record_call(outcome, self.provider_id)
        if outcome == OUTCOME_AUTH:
            raise ProviderError(
                f"authentication rejected (HTTP {response.status_code}); check the configured key",
                is_retriable=retriable,
            )
        if outcome == OUTCOME_RATE_LIMIT:
            raise ProviderError("rate limited", is_retriable=retriable)
        if outcome == OUTCOME_SERVER:
            raise ProviderError(f"provider error (HTTP {response.status_code})", is_retriable=retriable)
        raise ProviderError(f"request rejected (HTTP {response.status_code})", is_retriable=retriable)

    async def _call(self, request: ResponseRequest, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        started = time.monotonic()
        try:
            body = await self._post(payload)
        finally:
            PROVIDER_DURATION.observe(time.monotonic() - started)
        return body, int((time.monotonic() - started) * 1000)


class AnthropicResponseProvider(_HttpResponseProvider):
    """Generation backed by the Anthropic Messages API, or anything shaped like it."""

    provider_id = "anthropic"
    default_model_id = ANTHROPIC_DEFAULT_MODEL

    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = 120.0,
        base_url: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            api_key,
            url=base_url or _ANTHROPIC_URL,
            auth_header=_ANTHROPIC_AUTH_HEADER,
            auth_template=_ANTHROPIC_AUTH_TEMPLATE,
            extra_headers={"anthropic-version": _ANTHROPIC_VERSION},
            timeout_s=timeout_s,
            client=client,
        )

    async def respond(self, request: ResponseRequest) -> ResponseResult:
        system, data = assemble_response_prompt(request)
        payload: dict[str, Any] = {
            "model": request.model_id,
            "max_tokens": request.max_output_tokens,
            "system": system,
            "tools": [
                {
                    "name": RESPONSE_TOOL_NAME,
                    "description": "Answer the prompt, citing the served items each assertion rests on.",
                    "input_schema": RESPONSE_SCHEMA,
                }
            ],
            # Forcing the tool is what makes prose output impossible rather than
            # merely discouraged.
            "tool_choice": {"type": "tool", "name": RESPONSE_TOOL_NAME},
            "messages": [{"role": "user", "content": data}],
        }
        body, duration_ms = await self._call(request, payload)

        usage = _anthropic_usage(body)
        record_tokens(usage)
        answer = _read_tool_input(_anthropic_tool_input(body))

        record_call(OUTCOME_OK, self.provider_id)
        return ResponseResult(
            answer=answer[0],
            assertions=answer[1],
            duration_ms=duration_ms,
            model_id=str(body.get("model") or request.model_id),
            usage=usage,
        )


class OpenAICompatibleResponseProvider(_HttpResponseProvider):
    """Generation backed by any `/v1/chat/completions` endpoint with tool calling.

    Not "the OpenAI adapter" so much as the adapter for the shape most things
    speak: Azure, Groq, Together, Fireworks, vLLM, Ollama, LiteLLM and the
    majority of internal gateways all serve this format, so pointing at one is a
    base URL and a credential rather than a new adapter.
    """

    provider_id = "openai"
    default_model_id = OPENAI_DEFAULT_MODEL

    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = 120.0,
        base_url: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            api_key,
            url=base_url or _OPENAI_URL,
            auth_header=_OPENAI_AUTH_HEADER,
            auth_template=_OPENAI_AUTH_TEMPLATE,
            extra_headers={},
            timeout_s=timeout_s,
            client=client,
        )

    async def respond(self, request: ResponseRequest) -> ResponseResult:
        system, data = assemble_response_prompt(request)
        payload: dict[str, Any] = {
            "model": request.model_id,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": data},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": RESPONSE_TOOL_NAME,
                        "description": "Answer the prompt, citing the served items each assertion rests on.",
                        "parameters": RESPONSE_SCHEMA,
                    },
                }
            ],
            # Naming the tool, rather than "auto", is what makes prose output
            # impossible instead of merely unlikely.
            "tool_choice": {"type": "function", "function": {"name": RESPONSE_TOOL_NAME}},
        }
        body, duration_ms = await self._call(request, payload)

        usage = _openai_usage(body)
        record_tokens(usage)
        answer = _read_tool_input(_openai_tool_input(body))

        record_call(OUTCOME_OK, self.provider_id)
        return ResponseResult(
            answer=answer[0],
            assertions=answer[1],
            duration_ms=duration_ms,
            model_id=str(body.get("model") or request.model_id),
            usage=usage,
        )


# ---------------------------------------------------------------------------
# Response reading
# ---------------------------------------------------------------------------


def _anthropic_usage(body: dict[str, Any]) -> TokenUsage:
    """This vendor's usage block, mapped onto the shared builder.

    Only the three field names are adapter-specific. What a usage record may look
    like -- all counts or none, unknown never spelled as zero -- is the kit's, so
    the adapters cannot drift on the one thing a spend total is built from.
    """
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return TokenUsage.unknown()
    return build_token_usage(
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("cache_read_input_tokens"),
    )


def _openai_usage(body: dict[str, Any]) -> TokenUsage:
    """This family's usage block. The cached figure lives a level down.

    Absent `prompt_tokens_details` means no cache was read, which is a real zero
    rather than an unknown, so it never poisons an otherwise complete record.
    """
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return TokenUsage.unknown()
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return build_token_usage(usage.get("prompt_tokens"), usage.get("completion_tokens"), cached)


def _anthropic_tool_input(body: dict[str, Any]) -> object:
    """Read the forced tool call. Prose is refused rather than parsed."""
    content = body.get("content")
    if not isinstance(content, list):
        raise ProviderMalformedError("response had no content array")
    inputs = [
        block.get("input")
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == RESPONSE_TOOL_NAME
    ]
    if not inputs:
        raise ProviderMalformedError(
            f"model did not call {RESPONSE_TOOL_NAME}; free-form output is refused rather than parsed"
        )
    return inputs[0]


def _openai_tool_input(body: dict[str, Any]) -> object:
    """Read the forced tool call. Arguments arrive as a JSON string, not an object."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderMalformedError("response had no choices array")
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderMalformedError("first choice was not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderMalformedError("first choice had no message object")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        finish = first.get("finish_reason")
        raise ProviderMalformedError(
            f"model did not call {RESPONSE_TOOL_NAME} (finish_reason={finish!r}); free-form output "
            "is refused rather than parsed"
        )
    named = [c for c in calls if isinstance(c, dict) and _function_name(c) == RESPONSE_TOOL_NAME]
    if not named:
        raise ProviderMalformedError(f"model called a tool other than {RESPONSE_TOOL_NAME}")
    function = named[0].get("function")
    arguments = function.get("arguments") if isinstance(function, dict) else None
    if not isinstance(arguments, str):
        raise ProviderMalformedError("tool call carried no arguments string")
    try:
        return json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ProviderMalformedError("tool call arguments were not valid JSON") from exc


def _function_name(call: dict[str, Any]) -> str | None:
    function = call.get("function")
    name = function.get("name") if isinstance(function, dict) else None
    return name if isinstance(name, str) else None


def _read_tool_input(raw: object) -> tuple[str, tuple[Assertion, ...]]:
    """The tool input as the two values the contract promises.

    Refuses rather than repairs. A malformed answer is not salvageable into a
    partial one: an assertion whose citation list failed to parse would be
    recorded as citing nothing, which is a specific and wrong finding rather than
    a missing one.
    """
    if not isinstance(raw, dict):
        raise ProviderMalformedError(f"tool input was {type(raw).__name__}, not an object")
    answer = raw.get("answer")
    if not isinstance(answer, str):
        raise ProviderMalformedError("tool input carried no answer string")
    raw_assertions = raw.get("assertions")
    if not isinstance(raw_assertions, list):
        raise ProviderMalformedError("tool input carried no assertions array")

    assertions: list[Assertion] = []
    for entry in raw_assertions:
        if not isinstance(entry, dict):
            raise ProviderMalformedError("an assertion was not an object")
        text = entry.get("text")
        cited = entry.get("cited_receipt_item_ids")
        if not isinstance(text, str):
            raise ProviderMalformedError("an assertion carried no text")
        if not isinstance(cited, list) or any(not isinstance(value, str) for value in cited):
            raise ProviderMalformedError("an assertion's citations were not a list of strings")
        assertions.append(Assertion(cited_receipt_item_ids=tuple(cited), text=text))
    return answer, tuple(assertions)


__all__ = [
    "ANTHROPIC_DEFAULT_MODEL",
    "OPENAI_DEFAULT_MODEL",
    "AnthropicResponseProvider",
    "OpenAICompatibleResponseProvider",
]
