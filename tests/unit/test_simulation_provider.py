"""The generation seam: prompt assembly, both adapters, and the two refusals.

E24-T3. What is asserted here is the contract rather than the vendors: that
served material is delimited as data, that a model answering in prose fails
rather than being parsed, that usage is reported or explicitly unknown and never
zero, and that the two configurations ADR 0025 and ADR 0026 refuse are refused
with the information an operator needs to fix them.

The adapters are tested against transports rather than mocks of themselves,
through `httpx.MockTransport`, because the thing most likely to be wrong about an
adapter is the shape it puts on the wire.
"""

from __future__ import annotations

import datetime
import json

import httpx
import pytest

from contextplane.config import Settings
from contextplane.extraction.provider import USAGE_REPORTED, USAGE_UNKNOWN, ProviderError, ProviderMalformedError
from contextplane.extraction.response_adapters import (
    ANTHROPIC_DEFAULT_MODEL,
    OPENAI_DEFAULT_MODEL,
    AnthropicResponseProvider,
    OpenAICompatibleResponseProvider,
)
from contextplane.extraction.response_factory import (
    GENERATION_PROVIDERS,
    JudgeFamilyRefused,
    assert_families_differ,
    build_judge_provider,
    build_response_provider,
    default_model_for,
    resolved_model,
)
from contextplane.extraction.response_provider import (
    RESPONSE_SCHEMA,
    RESPONSE_TOOL_NAME,
    ResponseRequest,
    ServedBlockView,
    ServedItemView,
    SimulationUnavailable,
    assemble_response_prompt,
    render_context_as_data,
)

_NOW = datetime.datetime(2026, 8, 25, 12, 0, tzinfo=datetime.UTC)
_DSN = "postgresql+asyncpg://unused/unused"


def _settings(**overrides: object) -> Settings:
    """A Settings with only the field under test varied."""
    return Settings(database_url=_DSN, **overrides)  # type: ignore[arg-type]


def _request(*, instructions: tuple[str, ...] = (), disposition: str = "declared_known") -> ResponseRequest:
    return ResponseRequest(
        blocks=(
            ServedBlockView(
                items=(
                    ServedItemView(
                        block="workspace",
                        item_key="c1",
                        payload_json=json.dumps({"goal": "drain the queue"}),
                        receipt_item_id="rid-1",
                    ),
                ),
                name="workspace",
                reason=None,
                state="success",
            ),
            ServedBlockView(items=(), name="observed_claims", reason="the arm timed out", state="failed"),
            ServedBlockView(items=(), name="canonical", reason=None, state="empty"),
        ),
        instruction_disposition=disposition,
        instructions=instructions,
        max_output_tokens=512,
        model_id="test-model",
        prompt="how do I drain the queue?",
        requested_at=_NOW,
    )


def _anthropic_body(
    *, answer: str = "Drain it.", assertions: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "model": "claude-test",
        "content": [
            {
                "type": "tool_use",
                "name": RESPONSE_TOOL_NAME,
                "input": {
                    "answer": answer,
                    "assertions": assertions
                    if assertions is not None
                    else [{"text": "The queue drains via the runbook.", "cited_receipt_item_ids": ["rid-1"]}],
                },
            }
        ],
        "usage": {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 10},
    }


def _openai_body(*, assertions: list[dict[str, object]] | None = None) -> dict[str, object]:
    payload = {
        "answer": "Drain it.",
        "assertions": assertions
        if assertions is not None
        else [{"text": "The queue drains via the runbook.", "cited_receipt_item_ids": ["rid-1"]}],
    }
    return {
        "model": "gpt-test",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [{"function": {"name": RESPONSE_TOOL_NAME, "arguments": json.dumps(payload)}}]
                },
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 10}},
    }


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Prompt assembly: served material is data, instructions are instructions
# ---------------------------------------------------------------------------


def test_served_items_are_wrapped_in_the_requests_own_boundary() -> None:
    request = _request()
    system, data = assemble_response_prompt(request)
    assert f"<{request.boundary}>" in data
    assert f"</{request.boundary}>" in data
    assert request.boundary in system


def test_the_prompt_travels_in_the_data_turn_not_the_instruction_turn() -> None:
    """A prompt is caller-supplied text; the system turn is where instructions live."""
    request = _request()
    system, data = assemble_response_prompt(request)
    assert request.prompt in data
    assert request.prompt not in system


def test_a_failed_block_says_so_rather_than_being_omitted() -> None:
    """An agent told a block is empty may say the material does not exist; one told it failed must not."""
    rendered = render_context_as_data(_request().blocks, "BOUND")
    assert "state: failed" in rendered
    assert "the arm timed out" in rendered
    assert "state: empty" in rendered


def test_an_empty_block_is_rendered_with_no_items_rather_than_dropped() -> None:
    rendered = render_context_as_data(_request().blocks, "BOUND")
    assert rendered.count("(no items)") == 2


def test_the_instruction_delta_reaches_the_system_turn_and_the_disposition_with_it() -> None:
    system, _ = assemble_response_prompt(_request(instructions=("prefer the newer runbook",)))
    assert "prefer the newer runbook" in system
    assert "declared_known" in system


def test_declaring_nothing_is_distinguishable_from_declaring_an_empty_set() -> None:
    """ADR 0020's third assumption, on the prompt rather than only in the record."""
    undeclared, _ = assemble_response_prompt(_request(disposition="not_declared"))
    empty, _ = assemble_response_prompt(_request(disposition="declared_known"))
    assert "not_declared" in undeclared
    assert "declared_known" in empty
    assert undeclared != empty


def test_a_request_with_no_boundary_cannot_be_assembled() -> None:
    request = _request()
    broken = ResponseRequest(
        blocks=request.blocks,
        boundary="",
        instruction_disposition=request.instruction_disposition,
        instructions=request.instructions,
        max_output_tokens=request.max_output_tokens,
        model_id=request.model_id,
        prompt=request.prompt,
        requested_at=request.requested_at,
    )
    with pytest.raises(ValueError, match="containment boundary"):
        assemble_response_prompt(broken)


def test_the_schema_requires_a_citation_list_on_every_assertion() -> None:
    item = RESPONSE_SCHEMA["properties"]["assertions"]["items"]  # type: ignore[index]
    assert item["required"] == ["text", "cited_receipt_item_ids"]
    assert item["additionalProperties"] is False


# ---------------------------------------------------------------------------
# The adapters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_anthropic_adapter_forces_the_tool_and_returns_citations() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_anthropic_body())

    async with _client(handler) as client:
        provider = AnthropicResponseProvider("k", client=client)
        result = await provider.respond(_request())

    assert seen["tool_choice"] == {"type": "tool", "name": RESPONSE_TOOL_NAME}
    assert result.answer == "Drain it."
    assert result.cited() == frozenset({"rid-1"})
    assert result.usage.source == USAGE_REPORTED
    assert result.usage.total_tokens == 160


@pytest.mark.asyncio
async def test_the_openai_adapter_forces_the_tool_and_returns_citations() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_openai_body())

    async with _client(handler) as client:
        provider = OpenAICompatibleResponseProvider("k", client=client)
        result = await provider.respond(_request())

    assert seen["tool_choice"] == {"type": "function", "function": {"name": RESPONSE_TOOL_NAME}}
    assert result.cited() == frozenset({"rid-1"})
    assert result.usage.cached_prompt_tokens == 10


@pytest.mark.asyncio
async def test_an_assertion_citing_nothing_is_kept_rather_than_dropped() -> None:
    """The state the whole improvement surface is built on."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_anthropic_body(assertions=[{"text": "invented", "cited_receipt_item_ids": []}])
        )

    async with _client(handler) as client:
        result = await AnthropicResponseProvider("k", client=client).respond(_request())

    assert len(result.assertions) == 1
    assert result.assertions[0].cited_receipt_item_ids == ()
    assert result.cited() == frozenset()


@pytest.mark.parametrize(
    "body",
    [
        {"content": [{"type": "text", "text": "here is my answer"}]},
        {"content": []},
        {"content": "not a list"},
    ],
)
@pytest.mark.asyncio
async def test_prose_instead_of_a_tool_call_is_refused_rather_than_parsed(body: dict[str, object]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with _client(handler) as client:
        with pytest.raises(ProviderMalformedError):
            await AnthropicResponseProvider("k", client=client).respond(_request())


@pytest.mark.asyncio
async def test_an_openai_model_that_answers_in_prose_is_refused() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}]})

    async with _client(handler) as client:
        with pytest.raises(ProviderMalformedError, match=RESPONSE_TOOL_NAME):
            await OpenAICompatibleResponseProvider("k", client=client).respond(_request())


@pytest.mark.asyncio
async def test_a_malformed_citation_list_fails_rather_than_becoming_no_citations() -> None:
    """Recording it as citing nothing would be a specific and wrong finding."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_anthropic_body(assertions=[{"text": "x", "cited_receipt_item_ids": "rid-1"}]))

    async with _client(handler) as client:
        with pytest.raises(ProviderMalformedError, match="list of strings"):
            await AnthropicResponseProvider("k", client=client).respond(_request())


@pytest.mark.asyncio
async def test_a_provider_that_reports_no_usage_says_unknown_never_zero() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        body = _anthropic_body()
        del body["usage"]
        return httpx.Response(200, json=body)

    async with _client(handler) as client:
        result = await AnthropicResponseProvider("k", client=client).respond(_request())

    assert result.usage.source == USAGE_UNKNOWN
    assert result.usage.total_tokens is None


@pytest.mark.asyncio
async def test_an_auth_failure_never_echoes_the_response_body() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "key sk-secret-value is invalid"})

    async with _client(handler) as client:
        with pytest.raises(ProviderError) as caught:
            await AnthropicResponseProvider("k", client=client).respond(_request())

    assert "sk-secret-value" not in str(caught.value)
    assert caught.value.is_retriable is False


@pytest.mark.asyncio
async def test_a_rate_limit_is_retriable_and_a_bad_request_is_not() -> None:
    async def outcome(status: int) -> ProviderError:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={})

        async with _client(handler) as client:
            with pytest.raises(ProviderError) as caught:
                await AnthropicResponseProvider("k", client=client).respond(_request())
        return caught.value

    assert (await outcome(429)).is_retriable is True
    assert (await outcome(400)).is_retriable is False


def test_an_adapter_cannot_be_built_without_a_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        AnthropicResponseProvider("   ")


# ---------------------------------------------------------------------------
# The two refusals
# ---------------------------------------------------------------------------


def test_a_candidate_and_judge_from_one_family_are_refused_and_both_are_named() -> None:
    with pytest.raises(JudgeFamilyRefused) as caught:
        assert_families_differ(
            candidate_model="claude-a",
            candidate_provider="anthropic",
            judge_model="claude-b",
            judge_provider="anthropic",
        )
    message = str(caught.value)
    assert "claude-a" in message
    assert "claude-b" in message
    assert "JUDGE_PROVIDER" in message
    assert caught.value.is_retriable is False


def test_different_families_are_permitted() -> None:
    assert_families_differ(
        candidate_model="claude-a", candidate_provider="anthropic", judge_model="gpt-b", judge_provider="openai"
    )


def test_no_judge_configured_is_not_a_family_collision() -> None:
    """The deterministic three stay available with no judge at all."""
    assert_families_differ(
        candidate_model="claude-a", candidate_provider="anthropic", judge_model="", judge_provider="noop"
    )


def test_an_unconfigured_deployment_gets_none_rather_than_a_no_op() -> None:
    assert build_response_provider(_settings(simulation_provider="noop")) is None
    assert build_judge_provider(_settings(judge_provider="noop")) is None


def test_a_selected_provider_with_no_key_is_refused_at_construction() -> None:
    with pytest.raises(SimulationUnavailable, match="SIMULATION_API_KEY"):
        build_response_provider(_settings(simulation_provider="anthropic"), env={})


def test_a_provider_that_cannot_generate_is_refused_by_name() -> None:
    """`local` extracts with pattern rules and cannot answer a prompt."""
    with pytest.raises(SimulationUnavailable, match="cannot generate"):
        build_response_provider(_settings(simulation_provider="local"), env={"SIMULATION_API_KEY": "k"})


def test_the_generation_selectors_are_a_subset_of_the_extraction_ones() -> None:
    from contextplane.extraction.provider_registry import BUILT_IN_PROVIDERS

    assert GENERATION_PROVIDERS < BUILT_IN_PROVIDERS


def test_a_configured_provider_is_built_for_each_family() -> None:
    simulation = build_response_provider(_settings(simulation_provider="anthropic"), env={"SIMULATION_API_KEY": "k"})
    judge = build_judge_provider(_settings(judge_provider="openai"), env={"JUDGE_API_KEY": "k"})
    assert simulation is not None
    assert judge is not None
    assert (simulation.provider_id, judge.provider_id) == ("anthropic", "openai")


def test_the_default_model_is_the_adapters_and_a_pin_overrides_it() -> None:
    assert default_model_for("anthropic") == ANTHROPIC_DEFAULT_MODEL
    assert default_model_for("openai") == OPENAI_DEFAULT_MODEL
    assert default_model_for("local") == ""
    assert resolved_model(selector="anthropic", pinned="  ") == ANTHROPIC_DEFAULT_MODEL
    assert resolved_model(selector="anthropic", pinned="claude-pinned") == "claude-pinned"


def test_all_three_selectors_validate_against_the_same_registry() -> None:
    settings = _settings(simulation_provider="OPENAI", judge_provider="Anthropic", extraction_provider="LOCAL")
    assert (settings.simulation_provider, settings.judge_provider) == ("openai", "anthropic")
    with pytest.raises(ValueError, match="provider"):
        _settings(judge_provider="not-a-provider")
