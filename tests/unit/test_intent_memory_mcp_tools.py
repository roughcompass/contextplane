"""What the task-memory MCP tools do with a refusal the service raises.

The REST route and this surface reach the same `IntentCheckpointService`, so
neither has to re-derive a decision. What each *does* with a refusal is its own,
though, and the two answers differ on purpose: the route maps to a status code,
this one returns an error string. An exception escaping here reaches the agent
as a transport fault it cannot act on, when the thing to do is edit the text and
append again.

Admission is the case that matters. It was added to the service rather than to
either transport precisely because a transport-level scan is one a second
transport can be written without — which is how this write path acquired two
surfaces and no scan at all. Proving the service refuses is E13-T5's integration
test; proving this surface *reports* the refusal is here, because a tool that
raises where it should answer is a scan the agent cannot see.
"""

from __future__ import annotations

import datetime
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from contextplane.api.mcp.context import _request_app, _request_token
from contextplane.api.mcp.server import create_contextplane_mcp_server
from contextplane.exceptions import ValidationError
from tests.helpers.clock import FakeClock
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 8, 5, 12, 0, tzinfo=datetime.UTC)
_PATCH_TARGET = "contextplane.api.mcp.context._resolve_tenant"
_INTENT = str(uuid.uuid4())

_REFUSAL = (
    "this checkpoint carries content of a prohibited class (credit_card) in "
    "intent_checkpoint.body; remove it and append again"
)


def _build_mcp() -> Any:
    return create_contextplane_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        workspace_service=MagicMock(),
        clock=FakeClock(_NOW),
    )


def _fake_app(services: Any) -> Any:
    app = MagicMock()
    app.state.services = services
    return app


async def _call(mcp: Any, tool: str, args: dict[str, Any], *, services: Any) -> Any:
    token_cv = _request_token.set("fake-test-token")
    app_cv = _request_app.set(_fake_app(services))
    try:
        return await mcp.call_tool(tool, args)
    finally:
        _request_token.reset(token_cv)
        _request_app.reset(app_cv)


def _payload(result: Any) -> dict[str, Any]:
    """The tool's JSON body, whichever envelope the server wraps it in."""
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list):
        result = result[0]
    text = result if isinstance(result, str) else getattr(result, "text", None)
    if text is None:
        text = json.dumps(result)
    return dict(json.loads(text))


def _refusing_services() -> SimpleNamespace:
    """A container whose append refuses the way admission does."""
    checkpoints = MagicMock()
    checkpoints.append_checkpoint = AsyncMock(side_effect=ValidationError(_REFUSAL))
    grants = MagicMock()
    grants.assert_participant = AsyncMock(return_value=None)
    return SimpleNamespace(intent_checkpoints=checkpoints, intent_grants=grants)


@pytest.mark.asyncio
async def test_a_refused_append_is_reported_as_an_error_the_agent_can_act_on() -> None:
    """Not raised. An agent reads the string, edits the text and retries; a
    `ToolError` gives it a failure with nothing to do about it."""
    mcp = _build_mcp()
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=tenant_context())):
        result = await _call(
            mcp,
            "append_intent_checkpoint",
            {"intent_id": _INTENT, "goal": "charge 4111111111111111", "idempotency_key": "k1"},
            services=_refusing_services(),
        )

    body = _payload(result)
    assert "prohibited class" in body["error"]
    assert "intent_checkpoint.body" in body["error"], "the agent has to be told which field to fix"
    assert "checkpoint_id" not in body, "nothing was stored, so nothing may be named as stored"


@pytest.mark.asyncio
async def test_the_refusal_does_not_say_what_the_content_was() -> None:
    """The message names the class and the field. Echoing the matched substring
    back would copy the credential into a second place — the tool result, and
    from there into whatever transcript the agent keeps."""
    mcp = _build_mcp()
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=tenant_context())):
        result = await _call(
            mcp,
            "append_intent_checkpoint",
            {"intent_id": _INTENT, "goal": "charge 4111111111111111", "idempotency_key": "k2"},
            services=_refusing_services(),
        )

    assert "4111111111111111" not in json.dumps(_payload(result))
