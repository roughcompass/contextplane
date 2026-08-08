"""The OpenAI-compatible adapter, and the second thing it is for.

Most of what is tested here is the same contract the other adapter meets, which
is the point: with two real adapters, a vendor assumption cannot sit in the
shared kit without something failing. Where this one genuinely differs from the
other -- tool arguments arriving as a JSON string rather than an object, usage
carrying its cached figure a level down, the credential defaulting to a `Bearer`
`Authorization` header -- it differs on the wire format and nowhere else.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid

import httpx
import pytest

from contextplane.config import EXTRACTION_PROVIDERS, Settings
from contextplane.extraction.contract_suite import NetworkedExtractionProviderContract
from contextplane.extraction.factory import build_provider
from contextplane.extraction.openai_provider import (
    DEFAULT_MODEL,
    OpenAICompatibleExtractionProvider,
    build_from_env,
)
from contextplane.extraction.provider import (
    USAGE_REPORTED,
    USAGE_UNKNOWN,
    ExtractionRequest,
    ProviderError,
    ProviderMalformedError,
)
from contextplane.extraction.strategies import OBSERVATION, STRATEGIES
from contextplane.service.memory.session_events import SessionEvent

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)
_SUBJECT = "11111111-2222-3333-4444-555555555555"


def _event(body: str, *, seq: int = 1) -> SessionEvent:
    return SessionEvent(
        event_id=uuid.uuid4(),
        session_id="s1",
        seq=seq,
        kind="user_message",
        body=body,
        tool_name=None,
        metadata={},
        created_at=_NOW,
    )


def _request(*bodies: str) -> ExtractionRequest:
    definition = STRATEGIES[OBSERVATION.strategy_id]
    return ExtractionRequest(
        events=tuple(_event(b, seq=i + 1) for i, b in enumerate(bodies)),
        strategy_id=OBSERVATION.strategy_id,
        system_prompt=definition.system_prompt,
        output_schema=definition.output_schema,
        model_id=definition.default_model_id,
        max_output_tokens=definition.max_output_tokens,
        permitted_predicates=definition.permitted_predicates,
        requested_at=_NOW,
    )


def _tool_response(claims: list[dict[str, object]], **usage: object) -> httpx.Response:
    """A well-formed chat-completions answer carrying the forced tool call."""
    body: dict[str, object] = {
        "model": "gpt-4o-mini",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "record_claims",
                                # A JSON *string*, which is how this family sends
                                # tool arguments.
                                "arguments": json.dumps({"claims": claims}),
                            },
                        }
                    ],
                },
            }
        ],
    }
    if usage:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _captured() -> tuple[list[httpx.Request], object]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _tool_response([])

    return seen, handler


# --- The wire format ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_schema_is_sent_as_a_tool_the_model_must_call() -> None:
    """Naming the tool in `tool_choice`, rather than leaving it "auto", is what
    makes prose output impossible instead of merely unlikely."""
    seen, handler = _captured()

    async with _client(handler) as client:
        await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))

    payload = json.loads(seen[0].content)
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "record_claims"}}
    assert payload["tools"][0]["function"]["name"] == "record_claims"
    assert payload["tools"][0]["function"]["parameters"] == STRATEGIES[OBSERVATION.strategy_id].output_schema


@pytest.mark.asyncio
async def test_instructions_and_transcript_are_separate_turns() -> None:
    """The system turn is the only place instructions come from. A body placed
    where instructions live is how an event body becomes one."""
    seen, handler = _captured()

    async with _client(handler) as client:
        provider = OpenAICompatibleExtractionProvider("sk-test", client=client)
        await provider.extract(_request("the deploy is on Tuesdays"))

    messages = json.loads(seen[0].content)["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "the deploy is on Tuesdays" in messages[1]["content"]
    assert "the deploy is on Tuesdays" not in messages[0]["content"]


@pytest.mark.asyncio
async def test_the_transcript_is_delimited_with_the_requests_own_boundary() -> None:
    """The staging path checks output against `request.boundary`. An adapter
    that wrapped bodies in a delimiter of its own would be checked against a
    string that never wrapped anything."""
    seen, handler = _captured()
    request = dataclasses.replace(_request("hello"), boundary="BOUNDARY-XYZ")

    async with _client(handler) as client:
        await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(request)

    messages = json.loads(seen[0].content)["messages"]
    assert "<BOUNDARY-XYZ" in messages[1]["content"]
    assert "</BOUNDARY-XYZ>" in messages[1]["content"]


@pytest.mark.asyncio
async def test_a_tool_call_becomes_a_candidate() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _tool_response(
            [
                {
                    "subject_reference": _SUBJECT,
                    "predicate": "request_timeout_seconds",
                    "value": 900,
                    "event_ids": ["e1"],
                    "excerpt": "times out after 900 seconds",
                    "confidence": 0.8,
                }
            ],
            prompt_tokens=120,
            completion_tokens=30,
        )

    async with _client(handler) as client:
        result = await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))

    assert len(result.claims) == 1
    assert result.claims[0].value == 900
    assert result.claims[0].evidence_event_ids == ("e1",)
    assert result.usage.source == USAGE_REPORTED
    assert result.usage.prompt_tokens == 120


# --- Usage, which is where the two wire formats differ most -------------------


@pytest.mark.asyncio
async def test_the_cached_figure_is_read_from_its_nested_home() -> None:
    """This family reports it under `prompt_tokens_details`, unlike the flat
    field the other adapter reads."""

    def handler(_: httpx.Request) -> httpx.Response:
        return _tool_response(
            [],
            prompt_tokens=120,
            completion_tokens=30,
            prompt_tokens_details={"cached_tokens": 40},
        )

    async with _client(handler) as client:
        result = await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))

    assert result.usage.cached_prompt_tokens == 40


@pytest.mark.asyncio
async def test_an_endpoint_that_reports_no_cache_still_reports_usage() -> None:
    """Most endpoints speaking this format implement no prompt caching at all.
    Absent means no cache was read -- a real zero -- so it must not turn an
    otherwise complete record into an unknown."""

    def handler(_: httpx.Request) -> httpx.Response:
        return _tool_response([], prompt_tokens=120, completion_tokens=30)

    async with _client(handler) as client:
        result = await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))

    assert result.usage.source == USAGE_REPORTED
    assert result.usage.cached_prompt_tokens == 0


@pytest.mark.asyncio
async def test_a_missing_usage_block_is_unknown_not_zero() -> None:
    """Zero would make a call that consumed tokens look free, and a spend total
    built from those is wrong in the direction nobody investigates."""

    def handler(_: httpx.Request) -> httpx.Response:
        return _tool_response([])

    async with _client(handler) as client:
        result = await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))

    assert result.usage.source == USAGE_UNKNOWN
    assert result.usage.prompt_tokens is None


# --- Refusing anything that is not the schema ---------------------------------


@pytest.mark.asyncio
async def test_prose_instead_of_a_tool_call_is_refused() -> None:
    """A best-effort parse of prose is how instruction text ends up in a stored
    value. The model was given one way to answer and text is not it."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Sure! Here..."}}],
            },
        )

    async with _client(handler) as client:
        with pytest.raises(ProviderMalformedError, match="did not call"):
            await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))


@pytest.mark.asyncio
async def test_tool_arguments_that_are_not_json_are_refused() -> None:
    """Arguments arrive as a string on this wire format, so "is it JSON" is a
    real question here in a way it is not for the other adapter."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {"type": "function", "function": {"name": "record_claims", "arguments": "not json"}}
                            ]
                        },
                    }
                ],
            },
        )

    async with _client(handler) as client:
        with pytest.raises(ProviderMalformedError, match="not valid JSON"):
            await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))


@pytest.mark.asyncio
async def test_more_than_one_tool_call_is_refused_rather_than_merged() -> None:
    """Merging them would invent a claim set nobody produced."""

    def handler(_: httpx.Request) -> httpx.Response:
        call = {"type": "function", "function": {"name": "record_claims", "arguments": json.dumps({"claims": []})}}
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [call, call]}}],
            },
        )

    async with _client(handler) as client:
        with pytest.raises(ProviderMalformedError, match="one call was required"):
            await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))


@pytest.mark.asyncio
async def test_a_claim_citing_no_evidence_is_refused() -> None:
    """An extraction nobody can trace back to a source is indistinguishable from
    an invention."""

    def handler(_: httpx.Request) -> httpx.Response:
        return _tool_response(
            [{"subject_reference": _SUBJECT, "predicate": "p", "value": 1, "event_ids": "e1"}],
        )

    async with _client(handler) as client:
        with pytest.raises(ProviderMalformedError, match="event_ids"):
            await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))


# --- Error taxonomy, decided once in the kit ----------------------------------


@pytest.mark.parametrize(
    ("status", "retriable"),
    [(401, False), (403, False), (429, True), (500, True), (503, True), (400, False)],
)
@pytest.mark.asyncio
async def test_status_maps_to_the_same_retry_decision_as_the_other_adapter(status: int, retriable: bool) -> None:
    """Not a duplicate of the kit's own test: this is the check that this
    adapter routes through the kit rather than re-deciding locally."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    async with _client(handler) as client:
        with pytest.raises(ProviderError) as caught:
            await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))

    assert caught.value.is_retriable is retriable


@pytest.mark.asyncio
async def test_an_error_body_never_reaches_the_message() -> None:
    """An auth-failure body can echo request material, and the reason string
    reaches logs."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "key sk-secret-leaked is invalid"}})

    async with _client(handler) as client:
        with pytest.raises(ProviderError) as caught:
            await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))

    assert "sk-secret-leaked" not in str(caught.value)


# --- Transport configuration --------------------------------------------------


@pytest.mark.asyncio
async def test_an_unconfigured_adapter_talks_to_the_vendor_with_a_bearer_token() -> None:
    """The default auth shape differs from the other adapter's, which is the one
    vendor difference that has to be right out of the box."""
    seen, handler = _captured()

    async with _client(handler) as client:
        await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))

    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_pointing_it_at_a_gateway_is_a_base_url_and_a_key() -> None:
    """The whole reason this adapter exists: vLLM, Ollama, Groq, LiteLLM and
    most internal gateways are this plus a credential."""
    seen, handler = _captured()

    async with _client(handler) as client:
        provider = OpenAICompatibleExtractionProvider(
            "sk-test", client=client, base_url="https://vllm.internal:8000/v1/chat/completions"
        )
        await provider.extract(_request("x"))

    assert str(seen[0].url) == "https://vllm.internal:8000/v1/chat/completions"


@pytest.mark.asyncio
async def test_the_auth_header_and_its_spelling_are_both_configurable() -> None:
    seen, handler = _captured()

    async with _client(handler) as client:
        provider = OpenAICompatibleExtractionProvider(
            "sk-test", client=client, auth_header="api-key", auth_template="{key}"
        )
        await provider.extract(_request("x"))

    assert seen[0].headers["api-key"] == "sk-test"
    assert "authorization" not in seen[0].headers


@pytest.mark.asyncio
async def test_a_brace_in_the_template_is_not_a_format_field() -> None:
    """`str.format` on an operator-supplied template carrying a credential is an
    arbitrary attribute read, not a substitution."""
    seen, handler = _captured()

    async with _client(handler) as client:
        provider = OpenAICompatibleExtractionProvider(
            "sk-test", client=client, auth_template="Bearer {key} {not_a_field}"
        )
        await provider.extract(_request("x"))

    assert seen[0].headers["authorization"] == "Bearer sk-test {not_a_field}"


@pytest.mark.asyncio
async def test_a_redirect_is_never_followed() -> None:
    """httpx strips `Authorization` cross-origin, which covers the default here
    -- but not a custom auth header, and this adapter's point is that the header
    is configurable. Declining redirects is the rule that holds either way."""
    hops: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.example/steal"})

    async with _client(handler) as client:
        provider = OpenAICompatibleExtractionProvider("sk-test", client=client, auth_header="api-key")
        with pytest.raises(ProviderError):
            await provider.extract(_request("x"))

    assert len(hops) == 1, "the redirect was followed, carrying the credential with it"


def test_the_effective_endpoint_is_logged_without_its_path(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        OpenAICompatibleExtractionProvider("sk-test", base_url="https://gw.internal:8443/v1/tok3n/chat/completions")

    logged = [r.getMessage() for r in caplog.records if "openai_endpoint" in r.getMessage()]
    assert logged
    assert "gw.internal:8443" in logged[0]
    assert "tok3n" not in logged[0]


def test_a_plaintext_endpoint_warns(caplog: pytest.LogCaptureFixture) -> None:
    """Ollama on loopback is a legitimate answer, so this warns rather than
    refuses -- but never silently, because the key and every transcript cross in
    the clear."""
    with caplog.at_level("WARNING"):
        OpenAICompatibleExtractionProvider("sk-test", base_url="http://localhost:11434/v1/chat/completions")

    assert any("insecure_endpoint" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_oversized_response_is_refused_rather_than_buffered() -> None:
    from contextplane.extraction.adapter_kit import MAX_RESPONSE_BYTES

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    async with _client(handler) as client:
        with pytest.raises(ProviderMalformedError, match="cap"):
            await OpenAICompatibleExtractionProvider("sk-test", client=client).extract(_request("x"))


# --- Selection ----------------------------------------------------------------


def test_openai_is_a_legal_selector() -> None:
    assert "openai" in EXTRACTION_PROVIDERS


def test_an_empty_key_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="API key is required"):
        OpenAICompatibleExtractionProvider("   ")


def test_selecting_it_without_a_key_fails_at_startup() -> None:
    """Never a silent fallback to noop: a deployment that asked for a model and
    got nothing would report healthy while producing nothing."""
    with pytest.raises(ValueError, match="EXTRACTION_API_KEY"):
        build_from_env({})


def test_either_conventional_key_name_is_accepted() -> None:
    assert build_from_env({"EXTRACTION_API_KEY": "sk-a"}).provider_id == "openai"
    assert build_from_env({"OPENAI_API_KEY": "sk-b"}).provider_id == "openai"


def test_the_factory_builds_it_from_configuration() -> None:
    url = "postgresql+asyncpg://x/y"
    settings = Settings(
        database_url=url,
        pgbouncer_url=url,
        scheduler_jobstore_url=url,
        extraction_provider="openai",
    )
    provider = build_provider(settings, env={"EXTRACTION_API_KEY": "sk-x"})
    assert provider.provider_id == "openai"


def test_the_adapter_declares_its_own_default_model() -> None:
    """A model id belongs to the provider that has to serve it. Naming one
    vendor's model as a global default is what made the model setting mean
    "whatever the single adapter happened to use"."""
    assert OpenAICompatibleExtractionProvider.default_model_id == DEFAULT_MODEL
    assert not DEFAULT_MODEL.startswith("claude"), "a vendor default must name this vendor's own model"


# --- The shipped contract, at the networked tier ------------------------------


def _echoing_handler(request: httpx.Request) -> httpx.Response:
    """A model that regurgitates the transcript it was shown.

    A benign stand-in passes the containment check with nothing for the boundary
    to leak into, which is a pass that examined nothing. Echoing the delimited
    body back as a claim value makes the check fail unless the body was actually
    wrapped -- at which point a forged delimiter inside it is already inert.
    """
    import re as _re

    payload = json.loads(request.content)
    system = payload["messages"][0]["content"]
    content = payload["messages"][1]["content"]
    boundary = _re.search(r"delimited by <(\S+?)> tags", system).group(1)  # type: ignore[union-attr]

    lines = content.splitlines()
    body: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith(f"<{boundary} "):
            i += 2  # the opening tag spans two lines
            while i < len(lines) and lines[i] != f"</{boundary}>":
                body.append(lines[i])
                i += 1
        i += 1
    echoed = "\n".join(body).strip()

    if not echoed:
        # Nothing in the transcript means nothing to propose. A stand-in that
        # returned a claim here would fail the very rule the suite enforces.
        return _tool_response([])

    return _tool_response(
        [
            {
                "subject_reference": _SUBJECT,
                "predicate": "request_timeout_seconds",
                "value": echoed,
                "event_ids": ["e1"],
                "excerpt": echoed,
                "confidence": 0.5,
            }
        ]
    )


class TestOpenAIProviderContract(NetworkedExtractionProviderContract):
    """The same shipped contract the other adapter meets.

    This is the check that makes the seam plural: the kit cannot grow a detail
    true only of the first adapter without failing here.
    """

    @staticmethod
    def make_provider() -> OpenAICompatibleExtractionProvider:
        return OpenAICompatibleExtractionProvider(
            "sk-contract",
            client=httpx.AsyncClient(transport=httpx.MockTransport(_echoing_handler)),
        )
