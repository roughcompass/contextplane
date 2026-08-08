"""The Anthropic adapter: a real model, optional and never required.

Nothing in the registry needs this to be configured. It exists so a deployment
that wants model-quality extraction can have it; local development and CI run on
the rules provider, and a deployment that configures nothing runs the no-op. The
API key is read once at construction and never logged, never echoed in an error,
and never included in a metric label.

**Tool-use, not prose parsing.** The schema is passed as a tool the model must
call, so the output arrives as a validated JSON object rather than text somebody
has to parse. That is the containment requirement expressed in the transport: a
model that returns prose instead of calling the tool has failed, and failing is
the correct outcome — a best-effort parse of prose is how instruction text ends
up in a stored value.

**Event bodies go in the user turn as delimited data; the strategy goes in the
system prompt.** Never the other way round, and never concatenated. The system
prompt is the only place instructions come from, so a body cannot become one by
being placed where instructions live.

**Usage is reported, not estimated.** The API returns exact counts, including
cache reads, so this adapter never guesses — and it never substitutes zero for a
field the API omitted.
"""

from __future__ import annotations

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

# Vendor defaults, not constants. Each one is what this adapter uses when the
# operator has configured nothing, which is what keeps an existing deployment
# working unchanged after this file learned to point somewhere else.
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_AUTH_HEADER = "x-api-key"
# Bare credential, no scheme prefix -- what this vendor's header expects. An
# OpenAI-shaped gateway wants `Bearer {key}`, which is why the template is a
# setting rather than something spelled into the header construction below.
_AUTH_TEMPLATE = "{key}"

#: This adapter's own default model, declared here rather than in the strategy
#: table or in configuration. A model id is a property of the provider that has
#: to serve it: a small fast model is right for extraction, which is bounded and
#: schema-constrained and runs per session batch, so a larger model's cost is
#: paid on every event rather than once. Naming it in shared code seeded one
#: vendor's id into every strategy regardless of which provider would serve it.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# The tool the model is required to call. Naming it after what it does rather
# than after the API mechanism, because the name appears in the model's context.
_TOOL_NAME = "record_claims"


class AnthropicExtractionProvider:
    """Extraction backed by the Anthropic Messages API, or anything shaped like it."""

    provider_id = "anthropic"
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
            msg = "an API key is required to construct the Anthropic provider"
            raise ValueError(msg)
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client = client
        self._url = base_url or _API_URL

        # Built once, here, so the credential is interpolated in exactly one
        # place. `str.replace`, never `str.format`: a template is operator-
        # supplied, and `format` would treat any other brace in it as a field
        # to expand -- against a value carrying a credential that is an
        # arbitrary-attribute-read, not a string substitution.
        header_name = auth_header or _AUTH_HEADER
        template = auth_template or _AUTH_TEMPLATE
        headers = {
            header_name: template.replace("{key}", api_key),
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        # Extra headers land last so a gateway can override `anthropic-version`.
        # It cannot override the auth header, `content-type`, or the hop-by-hop
        # names -- the settings parser refuses those outright, which is where
        # that rule belongs, since it has to hold for every adapter.
        headers.update(extra_headers)
        self._headers = headers

        self._log_effective_endpoint()

    def _log_effective_endpoint(self) -> None:
        """Record where extraction will actually send transcripts.

        Scheme, host and port only. The path and query are omitted deliberately:
        a gateway URL is operator-supplied and routinely carries a token in one
        or the other, and this line exists to answer "which endpoint is this
        talking to" -- which the authority answers on its own.
        """
        parsed = urlsplit(self._url)
        authority = parsed.hostname or "?"
        if parsed.port is not None:
            authority = f"{authority}:{parsed.port}"
        _log.info("extraction.anthropic_endpoint: %s://%s", parsed.scheme, authority)

        if parsed.scheme != "https":
            _log.warning(
                "extraction.anthropic_insecure_endpoint: %s is not https, so the API key and "
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

        Both halves come from the kit, which takes the request rather than a
        delimiter. That is what guarantees the bodies are wrapped in the same
        string the staging path later checks the output against -- an adapter
        passing one of its own would be checked against a string that never
        wrapped anything.
        """
        system, data = assemble_prompt(request, tool_name=_TOOL_NAME)

        return {
            "model": request.model_id,
            "max_tokens": request.max_output_tokens,
            "system": system,
            "tools": [
                {
                    "name": _TOOL_NAME,
                    "description": "Record the claims supported by the transcript.",
                    "input_schema": request.output_schema,
                }
            ],
            # Forcing the tool is what makes prose output impossible rather than
            # merely discouraged.
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
            "messages": [{"role": "user", "content": data}],
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send the call and read the body under a cap.

        Streamed, not read whole. The drain runs inside the tenant-facing API
        process, so a response big enough to exhaust memory takes the API down
        with it -- and once the endpoint is operator-configurable, "the provider
        would not send that" stops being an assumption anyone can make.

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

        The classification itself lives in the kit, so both adapters answer
        "retry this?" the same way. What stays here is the vendor-facing
        wording and the metric label, which is all that was ever adapter-
        specific about it.

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

        # Remaining 4xx: a malformed request on our side. Already counted above
        # with every other non-OK outcome; counting it again here would show two
        # calls where one was made.
        raise ProviderError(f"request rejected (HTTP {response.status_code})", is_retriable=retriable)


# -- response parsing ---------------------------------------------------------


def _parse_usage(body: dict[str, Any]) -> TokenUsage:
    """This vendor's usage block, mapped onto the shared builder.

    What is adapter-specific is only the three field names. The rules about what
    a usage record may look like -- all counts or none, unknown never spelled as
    zero, a missing cache figure being a real zero rather than an unknown -- are
    the kit's, so both adapters cannot drift apart on the one thing a spend total
    is built from.
    """
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return TokenUsage.unknown()

    # Cache reads are reported separately and are part of the input count, not an
    # addition to it.
    return build_token_usage(
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("cache_read_input_tokens"),
    )


def _parse_claims(body: dict[str, Any]) -> tuple[CandidateClaim, ...]:
    """Read the forced tool call. Anything else is malformed, not salvageable.

    Prose in place of a tool call is refused rather than parsed. That refusal is
    the point: the model was given one way to answer, and text is not it.

    No boundary check happens here. It has one home, on the staging path, where
    every provider's output passes through the same check; a second copy that
    took a delimiter as an argument and ignored it is how the check came to be
    unreachable in the first place.
    """
    content = body.get("content")
    if not isinstance(content, list):
        raise ProviderMalformedError("response had no content array")

    tool_inputs = [
        block.get("input")
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == _TOOL_NAME
    ]
    if not tool_inputs:
        stop = body.get("stop_reason")
        raise ProviderMalformedError(
            f"model did not call {_TOOL_NAME} (stop_reason={stop!r}); free-form output is " "refused rather than parsed"
        )
    if len(tool_inputs) > 1:
        raise ProviderMalformedError(
            f"model called {_TOOL_NAME} {len(tool_inputs)} times; one call was required and "
            "merging them would invent a claim set nobody produced"
        )

    payload = tool_inputs[0]
    if not isinstance(payload, dict):
        raise ProviderMalformedError("tool input was not an object")

    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        raise ProviderMalformedError("tool input had no claims array")

    return tuple(_to_candidate(item) for item in raw_claims)


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
) -> AnthropicExtractionProvider:
    """Construct from an env mapping, accepting either key name.

    Two names because the platform's own tooling uses `CLAUDE_API_KEY` while the
    SDK convention is `ANTHROPIC_API_KEY`; a deployment should not have to know
    which one this module happened to pick.

    The transport arguments are pass-through and default to empty, which each
    resolve to this adapter's own vendor default. A caller that supplies none of
    them gets exactly the provider this function returned before they existed.
    """
    key = (env.get("CLAUDE_API_KEY") or env.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        msg = (
            "extraction provider 'anthropic' was selected but neither CLAUDE_API_KEY nor "
            "ANTHROPIC_API_KEY is set. Use EXTRACTION_PROVIDER=local for a key-free provider, "
            "or leave it unset for no extraction at all."
        )
        raise ValueError(msg)
    return AnthropicExtractionProvider(
        key,
        timeout_s=timeout_s,
        base_url=base_url,
        auth_header=auth_header,
        auth_template=auth_template,
        extra_headers=extra_headers,
    )


__all__ = ["DEFAULT_MODEL", "AnthropicExtractionProvider", "build_from_env"]
