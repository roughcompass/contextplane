"""Unit tests for the plain claim-retrieval MCP tools (`query_claims`,
`search_claims`): specifically the exception-to-`ToolError` translation at
their persona/limit/top_k validation catch sites.

`ClaimQuery.__post_init__` and `ClaimServingService.retrieve` both raise
before touching the database -- the persona/range checks run before any
`session.execute` -- so these tests drive the *real* `ClaimServingService`
through a `session_factory` that is configured but never actually called,
rather than mocking the service itself. That distinction is load-bearing:
`claim_serving.py`'s four raise sites moved from a bare `ValueError` to this
codebase's `ValidationError` (see its own module docstring), and a test that
mocked the raise directly would stay green even if one of these two tools'
catch sites fell out of step with that move -- exactly the failure mode a
mock-based test cannot catch.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from contextplane.api.mcp.context import _request_app, _request_token
from contextplane.api.mcp.server import create_registry_mcp_server
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.types import Embedder
from tests.helpers.clock import FakeClock
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 8, 5, 12, 0, tzinfo=datetime.UTC)
_PATCH_TARGET = "contextplane.api.mcp.context._resolve_tenant"


def _build_mcp() -> Any:
    clock = FakeClock(_NOW)
    return create_registry_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        workspace_service=MagicMock(),
        clock=clock,
    )


def _fake_app(services: Any) -> Any:
    app = MagicMock()
    app.state.services = services
    return app


async def _call(mcp: Any, tool: str, args: dict[str, Any], *, services: Any) -> Any:
    """Same shape `tests/unit/test_memory_curation_mcp_tools.py` already
    uses: set the per-request ContextVars, call the tool, tear down."""
    token_cv = _request_token.set("fake-test-token")
    app_cv = _request_app.set(_fake_app(services))
    try:
        return await mcp.call_tool(tool, args)
    finally:
        _request_token.reset(token_cv)
        _request_app.reset(app_cv)


@pytest.mark.asyncio
async def test_query_claims_translates_an_unknown_persona_through_the_real_service() -> None:
    """`ClaimQuery.__post_init__` raises before `ClaimServingService.query`
    is ever called, so no `claim_serving` service needs to be configured on
    the fake container -- the raise happens in `query_claims` itself,
    building the spec."""
    mcp = _build_mcp()
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=tenant_context())):
        with pytest.raises(ToolError, match="unknown persona"):
            await _call(mcp, "query_claims", {"persona": "l2"}, services=SimpleNamespace())


@pytest.mark.asyncio
async def test_query_claims_translates_an_out_of_range_limit_through_the_real_service() -> None:
    mcp = _build_mcp()
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=tenant_context())):
        with pytest.raises(ToolError, match="limit must be"):
            await _call(mcp, "query_claims", {"limit": 999}, services=SimpleNamespace())


@pytest.mark.asyncio
async def test_search_claims_translates_an_unknown_persona_through_the_real_service() -> None:
    """`search_claims` reads `claim_serving`/`embedder` off the container
    before calling `retrieve`, so both are configured here -- with a real
    `ClaimServingService` over a session factory that is never actually
    invoked, because `retrieve`'s persona check raises before any query."""
    mcp = _build_mcp()
    claim_serving = ClaimServingService(MagicMock(), clock=FakeClock(_NOW))
    services = SimpleNamespace(claim_serving=claim_serving, embedder=MagicMock(spec=Embedder))
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=tenant_context())):
        with pytest.raises(ToolError, match="unknown persona"):
            await _call(mcp, "search_claims", {"q": "who owns auth", "persona": "l2"}, services=services)


@pytest.mark.asyncio
async def test_search_claims_translates_an_out_of_range_top_k_through_the_real_service() -> None:
    mcp = _build_mcp()
    claim_serving = ClaimServingService(MagicMock(), clock=FakeClock(_NOW))
    services = SimpleNamespace(claim_serving=claim_serving, embedder=MagicMock(spec=Embedder))
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=tenant_context())):
        with pytest.raises(ToolError, match="top_k must be"):
            await _call(mcp, "search_claims", {"q": "who owns auth", "top_k": 999}, services=services)
