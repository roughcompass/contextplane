"""Nothing that carries a credential reaches a log, a repr, or an error string.

The extraction credential and the extra-header value are the two secrets this
subsystem holds, and both have a route to somewhere durable. `Settings` is
printed on a startup crash. Adapter construction logs its endpoint. A failing
call raises, and the reason string is logged by the drain.

Every one of those is a place where a value that was handled correctly for its
whole life gets written out at the last moment, so each is asserted here rather
than argued from the types. The `SecretStr` declarations make the repr safe; the
tests below are what would notice if a later change made a plain `str` of one of
them on the way past.
"""

from __future__ import annotations

import datetime
import uuid

import httpx
import pytest
from pydantic import SecretStr

from registry.config import Settings
from registry.extraction.adapter_kit import describe_headers, transport_failure_message
from registry.extraction.anthropic_provider import AnthropicExtractionProvider
from registry.extraction.openai_provider import OpenAICompatibleExtractionProvider
from registry.extraction.provider import ExtractionRequest, ProviderError
from registry.extraction.strategies import OBSERVATION, STRATEGIES
from registry.service.memory.session_events import SessionEvent

_KEY = "sk-super-secret-do-not-log-123"
_HEADER_VALUE = "gw-token-do-not-log-456"
_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


def _settings(**overrides: object) -> Settings:
    url = "postgresql+asyncpg://x/y"
    base: dict[str, object] = {
        "database_url": url,
        "pgbouncer_url": url,
        "scheduler_jobstore_url": url,
        "extraction_api_key": SecretStr(_KEY),
        "extraction_extra_headers": SecretStr(f"X-Gateway-Token:{_HEADER_VALUE}"),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _request() -> ExtractionRequest:
    definition = STRATEGIES[OBSERVATION.strategy_id]
    return ExtractionRequest(
        events=(
            SessionEvent(
                event_id=uuid.uuid4(),
                session_id="s1",
                seq=1,
                kind="user_message",
                body="the deploy runs on Tuesdays",
                tool_name=None,
                metadata={},
                created_at=_NOW,
            ),
        ),
        strategy_id=OBSERVATION.strategy_id,
        system_prompt=definition.system_prompt,
        output_schema=definition.output_schema,
        model_id=definition.default_model_id,
        max_output_tokens=definition.max_output_tokens,
        permitted_predicates=definition.permitted_predicates,
        requested_at=_NOW,
    )


# --- The settings repr --------------------------------------------------------


def test_the_settings_repr_does_not_contain_the_key() -> None:
    """`Settings` is printed whole on a startup crash, so a plain-`str` field
    here puts the credential in whatever collects that output."""
    assert _KEY not in repr(_settings())


def test_the_settings_repr_does_not_contain_an_extra_header_value() -> None:
    """Gateways routinely authenticate with a second header, so this variable
    carries credentials in practice whether or not it does in a given
    deployment."""
    assert _HEADER_VALUE not in repr(_settings())


@pytest.mark.parametrize("render", [repr, str])
def test_neither_rendering_of_settings_leaks(render: object) -> None:
    """`repr` and `str` are different methods and a logger may call either."""
    rendered = render(_settings())  # type: ignore[operator]
    assert _KEY not in rendered
    assert _HEADER_VALUE not in rendered


def test_dumping_settings_does_not_leak_either() -> None:
    """A config-echo endpoint or a debug dump goes through these, not `repr`."""
    settings = _settings()
    assert _KEY not in str(settings.model_dump())
    assert _KEY not in settings.model_dump_json()
    assert _HEADER_VALUE not in settings.model_dump_json()


def test_the_parsed_header_pairs_still_carry_the_real_value() -> None:
    """The counter-check. Redaction that also broke the value would pass every
    assertion above while making the feature useless."""
    pairs = dict(_settings().extraction_extra_header_pairs())
    assert pairs["X-Gateway-Token"] == _HEADER_VALUE


# --- The startup log ----------------------------------------------------------


@pytest.mark.parametrize(
    "provider_cls",
    [AnthropicExtractionProvider, OpenAICompatibleExtractionProvider],
)
def test_construction_logs_the_endpoint_without_the_credential(
    provider_cls: type, caplog: pytest.LogCaptureFixture
) -> None:
    """Both adapters log where they will send transcripts. The credential is in
    hand at that moment, which is exactly when it is easiest to include."""
    with caplog.at_level("DEBUG"):
        provider_cls(
            _KEY,
            base_url="https://gw.internal:8443/v1/messages",
            extra_headers=(("X-Gateway-Token", _HEADER_VALUE),),
        )

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged, "construction logged nothing at all, so this asserts nothing"
    assert _KEY not in logged
    assert _HEADER_VALUE not in logged


@pytest.mark.parametrize(
    "provider_cls",
    [AnthropicExtractionProvider, OpenAICompatibleExtractionProvider],
)
def test_a_url_carrying_userinfo_does_not_reach_the_log(
    provider_cls: type, caplog: pytest.LogCaptureFixture
) -> None:
    """A base URL is operator-supplied and `user:pass@host` is a legal way to
    write one. The endpoint line reports the authority, and the authority is
    the host and port -- not the userinfo in front of them."""
    with caplog.at_level("DEBUG"):
        provider_cls(_KEY, base_url="https://someone:hunter2@gw.internal/v1/messages")

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "hunter2" not in logged
    assert "gw.internal" in logged, "the line must still say which endpoint, or it is not worth logging"


# --- A failed request ---------------------------------------------------------


@pytest.mark.parametrize(
    "provider_cls",
    [AnthropicExtractionProvider, OpenAICompatibleExtractionProvider],
)
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "authentication"),
        (403, "authentication"),
        # Named by condition rather than by number, deliberately: the drain acts
        # on "rate limited", and the status is the less useful half of that.
        (429, "rate limited"),
        (500, "provider error"),
        (400, "request rejected"),
    ],
)
@pytest.mark.asyncio
async def test_a_failed_call_never_puts_the_credential_in_its_reason(
    provider_cls: type, status: int, expected: str
) -> None:
    """The drain logs the reason string, so it is a durable surface.

    An auth-failure body is the worst case and the most likely to be echoed:
    a gateway rejecting a key often quotes it back.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": f"key {_KEY} is invalid; header {_HEADER_VALUE} rejected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = provider_cls(
            _KEY,
            client=client,
            extra_headers=(("X-Gateway-Token", _HEADER_VALUE),),
        )
        with pytest.raises(ProviderError) as caught:
            await provider.extract(_request())

    reason = str(caught.value)
    assert _KEY not in reason
    assert _HEADER_VALUE not in reason
    assert expected in reason, "the reason must still say what happened"


@pytest.mark.parametrize(
    "provider_cls",
    [AnthropicExtractionProvider, OpenAICompatibleExtractionProvider],
)
@pytest.mark.asyncio
async def test_a_transport_failure_names_the_error_type_not_the_request(
    provider_cls: type,
) -> None:
    """httpx exceptions stringify to include the request URL, and the URL is
    operator-supplied. Naming the exception class instead is what keeps a
    credentialed gateway address out of the message."""

    def handler(_: httpx.Request) -> httpx.Response:
        msg = f"connection refused to https://someone:{_KEY}@gw.internal"
        raise httpx.ConnectError(msg)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = provider_cls(_KEY, client=client)
        with pytest.raises(ProviderError) as caught:
            await provider.extract(_request())

    reason = str(caught.value)
    assert _KEY not in reason
    assert "ConnectError" in reason, "the reason must still name what failed"


# --- The shared helper --------------------------------------------------------


def test_the_helper_reports_header_names_only() -> None:
    """Which headers were sent is the useful half of the diagnosis -- a gateway
    rejection is usually a missing one. The values add nothing to that and are
    the one thing that must not be written down."""
    described = describe_headers({"X-Gateway-Token": _HEADER_VALUE, "Authorization": f"Bearer {_KEY}"})

    assert described == "Authorization, X-Gateway-Token"
    assert _HEADER_VALUE not in described
    assert _KEY not in described


def test_the_helper_has_no_way_to_pass_a_response_body() -> None:
    """Redaction inherited rather than repeated: an adapter cannot interpolate
    a body through this helper even deliberately, because there is no parameter
    for one. A convention has to be remembered by every adapter written later;
    a missing parameter does not."""
    import inspect

    parameters = set(inspect.signature(transport_failure_message).parameters)

    assert parameters == {"summary", "headers"}


def test_the_helper_leaves_a_message_alone_when_no_headers_are_given() -> None:
    assert transport_failure_message("rate limited") == "rate limited"


def test_the_helper_appends_names_when_headers_are_given() -> None:
    message = transport_failure_message(
        "authentication rejected (HTTP 401)",
        headers={"Authorization": f"Bearer {_KEY}"},
    )

    assert message == "authentication rejected (HTTP 401) (headers sent: Authorization)"
    assert _KEY not in message
