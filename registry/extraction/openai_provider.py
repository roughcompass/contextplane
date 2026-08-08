"""The OpenAI-compatible adapter: one wire format, many endpoints.

This is not "the OpenAI adapter" so much as the adapter for the shape most
things speak. Azure OpenAI, Groq, Together, Fireworks, vLLM, Ollama, LiteLLM and
the large majority of internal gateways all serve `/v1/chat/completions` with
tool calling, so pointing this at one of them is a base URL and a credential
rather than a new adapter.

**Its second job is to keep the seam honest.** With one adapter, a vendor
assumption in a shared layer is invisible -- there is nothing for it to be wrong
against. With two, the kit cannot quietly grow an Anthropic-shaped detail
without something here failing. That is why this exists now rather than the
first time somebody asks for it.

**Tool-use, not prose parsing.** The strategy's schema is handed over as a tool
the model is required to call, and `tool_choice` names it, so free-form output is
impossible rather than discouraged. A model that answers in prose has failed, and
failing is correct: a best-effort parse of prose is how instruction text ends up
inside a stored value.

**No vendor SDK.** The whole surface used here is one POST with a JSON body. A
client library would add a dependency, a retry policy this adapter does not want,
and an opinion about which endpoints are legitimate -- and that last one is
exactly what this adapter exists to not have.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from registry.extraction.adapter_kit import (
    OUTCOME_AUTH,
    OUTCOME_MALFORMED,
    OUTCOME_OK,
    OUTCOME_RATE_LIMIT,
    OUTCOME_SERVER,
    OUTCOME_TIMEOUT,
    PROVIDER_DURATION,
    assemble_prompt,
    build_token_usage,
    classify_status,
    read_json_capped,
    record_call,
    record_tokens,
)
from registry.extraction.provider import (
    CandidateClaim,
    ExtractionRequest,
    ExtractionResult,
    ProviderError,
    ProviderMalformedError,
    TokenUsage,
)

_log = logging.getLogger(__name__)

# Vendor defaults, used when the operator configured nothing. The auth shape is
# the one difference from the other adapter that matters: this family carries the
# credential in `Authorization` with a `Bearer ` prefix rather than in a
# vendor-specific header.
_API_URL = "https://api.openai.com/v1/chat/completions"
_AUTH_HEADER = "Authorization"
_AUTH_TEMPLATE = "Bearer {key}"

#: This adapter's own default model, declared here rather than in configuration.
#: A model id is a property of the provider that has to serve it -- naming one
#: vendor's model as a global default is what made the model setting mean
#: "whatever the one adapter used".
DEFAULT_MODEL = "gpt-4o-mini"

# The tool the model is required to call. Named for what it does, because the
# name appears in the model's context.
_TOOL_NAME = "record_claims"


class OpenAICompatibleExtractionProvider:
    """Extraction backed by any `/v1/chat/completions` endpoint with tool calling."""

    provider_id = "openai"
    default_model_id = DEFAULT_MODEL

    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
        base_url: str = "",
        auth_header: str = "",
        auth_template: str = "",
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if not api_key.strip():
            msg = "an API key is required to construct the OpenAI-compatible provider"
            raise ValueError(msg)
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client = client
        self._url = base_url or _API_URL

        # `str.replace`, never `str.format`: the template is operator-supplied,
        # and `format` would treat any other brace in it as a field to expand --
        # against a string carrying a credential that is an arbitrary attribute
        # read rather than a substitution.
        header_name = auth_header or _AUTH_HEADER
        template = auth_template or _AUTH_TEMPLATE
        headers = {
            header_name: template.replace("{key}", api_key),
            "content-type": "application/json",
        }
        headers.update(extra_headers)
        self._headers = headers

        self._log_effective_endpoint()

    def _log_effective_endpoint(self) -> None:
        """Record where extraction will send transcripts: authority only.

        The path and query are omitted deliberately. A gateway URL is
        operator-supplied and routinely carries a token in one or the other, and
        the question this line answers -- which endpoint is in use -- is answered
        by the authority on its own.
        """
        parsed = urlsplit(self._url)
        authority = parsed.hostname or "?"
        if parsed.port is not None:
            authority = f"{authority}:{parsed.port}"
        _log.info("extraction.openai_endpoint: %s://%s", parsed.scheme, authority)

        if parsed.scheme != "https":
            _log.warning(
                "extraction.openai_insecure_endpoint: %s is not https, so the API key and "
                "every transcript sent for extraction cross the network in the clear. This is "
                "only safe on a loopback or a trusted in-cluster address.",
                parsed.scheme,
            )

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        payload = self._build_payload(request)

        started = time.monotonic()
        try:
            body = await self._post(payload)
        finally:
            PROVIDER_DURATION.observe(time.monotonic() - started)
        duration_ms = int((time.monotonic() - started) * 1000)

        usage = _parse_usage(body)
        record_tokens(usage)
        claims = _parse_claims(body)

        record_call(OUTCOME_OK, self.provider_id)
        return ExtractionResult(
            claims=claims,
            usage=usage,
            model_id=str(body.get("model") or request.model_id),
            duration_ms=duration_ms,
        )

    # -- transport -------------------------------------------------------------

    def _build_payload(self, request: ExtractionRequest) -> dict[str, Any]:
        """Assemble the call. Instructions and data never share a turn.

        The two halves come from the kit, which takes the request rather than a
        delimiter, so the bodies are wrapped in the same string the staging path
        later checks the output against.

        The system half becomes the `system` message and the data half the `user`
        message. Concatenating them into one turn is how an event body becomes an
        instruction by being placed where instructions live.
        """
        system, data = assemble_prompt(request, tool_name=_TOOL_NAME)

        return {
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
                        "name": _TOOL_NAME,
                        "description": "Record the claims supported by the transcript.",
                        "parameters": request.output_schema,
                    },
                }
            ],
            # Naming the tool, rather than "auto", is what makes prose output
            # impossible instead of merely unlikely.
            "tool_choice": {"type": "function", "function": {"name": _TOOL_NAME}},
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send the call and read the body under the kit's cap.

        `follow_redirects=False`, stated per-request so it holds for an injected
        client too. httpx strips `Authorization` when a redirect crosses origins,
        which covers the default auth header here -- but not a custom one, and
        this adapter's whole point is that the header is configurable. Declining
        redirects outright is the rule that holds whatever it is set to.
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

        The classification is the kit's, so both adapters answer "retry this?"
        identically. Response bodies never reach the error message: an auth
        failure body can echo request material, and the reason string reaches
        logs.
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


# -- response parsing ---------------------------------------------------------


def _parse_usage(body: dict[str, Any]) -> TokenUsage:
    """This family's usage block, mapped onto the shared builder.

    Only the field names are adapter-specific. What a usage record may look like
    -- all counts or none, unknown never spelled as zero -- is the kit's, so the
    two adapters cannot drift on the one thing a spend total is built from.

    The cached figure lives a level down in `prompt_tokens_details`, and is
    absent entirely on the many endpoints that speak this format without
    implementing prompt caching. Absent means no cache was read, which is a real
    zero rather than an unknown -- so it never poisons an otherwise complete
    record.
    """
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return TokenUsage.unknown()

    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None

    return build_token_usage(
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        cached,
    )


def _parse_claims(body: dict[str, Any]) -> tuple[CandidateClaim, ...]:
    """Read the forced tool call. Anything else is malformed, not salvageable.

    Prose in place of a tool call is refused rather than parsed. That refusal is
    the point: the model was given one way to answer and text is not it.

    No boundary check happens here. It has one home, on the staging path, where
    every provider's output passes through the same check.
    """
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
            f"model did not call {_TOOL_NAME} (finish_reason={finish!r}); free-form output is "
            "refused rather than parsed"
        )

    named = [c for c in calls if isinstance(c, dict) and _function_name(c) == _TOOL_NAME]
    if not named:
        raise ProviderMalformedError(f"model called a tool other than {_TOOL_NAME}")
    if len(named) > 1:
        raise ProviderMalformedError(
            f"model called {_TOOL_NAME} {len(named)} times; one call was required and merging "
            "them would invent a claim set nobody produced"
        )

    # Arguments arrive as a JSON *string*, not an object -- the one structural
    # difference from the other adapter's tool block, and the reason a shared
    # parser for both would have to branch here anyway.
    raw_arguments = named[0].get("function", {}).get("arguments")
    if not isinstance(raw_arguments, str):
        raise ProviderMalformedError("tool call arguments were not a JSON string")
    try:
        payload = json.loads(raw_arguments)
    except ValueError as exc:
        raise ProviderMalformedError("tool call arguments were not valid JSON") from exc

    if not isinstance(payload, dict):
        raise ProviderMalformedError("tool call arguments were not an object")

    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        raise ProviderMalformedError("tool input had no claims array")

    return tuple(_to_candidate(item) for item in raw_claims)


def _function_name(call: dict[str, Any]) -> str | None:
    function = call.get("function")
    return function.get("name") if isinstance(function, dict) else None


def _to_candidate(item: object) -> CandidateClaim:
    if not isinstance(item, dict):
        raise ProviderMalformedError(f"claim entry was {type(item).__name__}, not an object")

    subject = item.get("subject_reference")
    predicate = item.get("predicate")
    event_ids = item.get("event_ids")

    if not isinstance(subject, str) or not subject.strip():
        raise ProviderMalformedError("claim had no subject_reference")
    if not isinstance(predicate, str) or not predicate.strip():
        raise ProviderMalformedError("claim had no predicate")
    if not isinstance(event_ids, list) or not all(isinstance(e, str) for e in event_ids):
        raise ProviderMalformedError("claim event_ids was not a list of strings")
    if "value" not in item:
        raise ProviderMalformedError("claim had no value")

    confidence = item.get("confidence")
    excerpt = item.get("excerpt")

    return CandidateClaim(
        subject_reference=subject.strip(),
        predicate=predicate.strip(),
        value=item["value"],
        evidence_event_ids=tuple(event_ids),
        excerpt=excerpt if isinstance(excerpt, str) else None,
        provider_confidence=float(confidence) if isinstance(confidence, int | float) else None,
    )


def build_from_env(
    env: dict[str, str],
    *,
    timeout_s: float = 60.0,
    base_url: str = "",
    auth_header: str = "",
    auth_template: str = "",
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> OpenAICompatibleExtractionProvider:
    """Construct from an env mapping, accepting the conventional key names."""
    key = (env.get("EXTRACTION_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
    if not key:
        msg = (
            "extraction provider 'openai' was selected but neither EXTRACTION_API_KEY nor "
            "OPENAI_API_KEY is set. Use EXTRACTION_PROVIDER=local for a key-free provider, "
            "or leave it unset for no extraction at all."
        )
        raise ValueError(msg)
    return OpenAICompatibleExtractionProvider(
        key,
        timeout_s=timeout_s,
        base_url=base_url,
        auth_header=auth_header,
        auth_template=auth_template,
        extra_headers=extra_headers,
    )


__all__ = ["DEFAULT_MODEL", "OpenAICompatibleExtractionProvider", "build_from_env"]
