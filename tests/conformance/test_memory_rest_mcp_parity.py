"""Session memory must be reachable over both transports, with the same shape.

An agent reaches this substrate over MCP; a human or a script reaches it over
REST. A capability present on one and missing from the other is a gap nobody
notices until an agent cannot resume — which is the one thing this phase exists
to make possible.

The second gate here is the one that matters more: no tool may accept an actor
identifier. A session carries no visibility setting and no sharing mode, so the
credential is the only thing scoping it. A tool taking an `actor_id` would let
one agent read another's conversation, and nothing downstream would catch it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

# Every operation the substrate exposes, in both directions.
_MEMORY_TOOLS = {
    "list_sessions",
    "record_session_event",
    "list_session_events",
    "get_session_event",
    "delete_session_event",
}

_MEMORY_REST_PATHS = {
    "/v1/memory/sessions",
    "/v1/memory/sessions/{session_id}/events",
    "/v1/memory/sessions/{session_id}/events/{event_id}",
}


@pytest.fixture(scope="module")
def mcp_tools() -> dict[str, object]:
    from registry.api.routers.mcp import create_registry_mcp_server

    server = create_registry_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        annotation_service=MagicMock(),
        workspace_service=MagicMock(),
    )
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_every_memory_operation_exists_over_mcp(mcp_tools: dict[str, object]) -> None:
    missing = _MEMORY_TOOLS - set(mcp_tools)
    assert not missing, f"missing from the MCP surface: {sorted(missing)}"


def test_every_memory_operation_exists_over_rest() -> None:
    from registry.api.routers import memory

    paths = {f"/v1/memory{r.path.removeprefix('/v1/memory')}" for r in memory.router.routes}  # type: ignore[attr-defined]
    missing = _MEMORY_REST_PATHS - paths
    assert not missing, f"missing from the REST surface: {sorted(missing)}"


def test_no_memory_tool_accepts_an_actor_identifier(mcp_tools: dict[str, object]) -> None:
    """The control is the omission, so something has to check the omission.

    A session has no visibility setting and no sharing mode: the actor on the
    credential is the only thing scoping it. A tool that accepted an actor id
    would be a way to read a colleague's conversation, and unlike every other
    read path in this system there is no visibility filter downstream that
    would refuse it.
    """
    for name in sorted(_MEMORY_TOOLS):
        schema = getattr(mcp_tools[name], "inputSchema", {}) or {}
        for parameter in schema.get("properties") or {}:
            assert "actor" not in parameter.lower(), f"{name} accepts {parameter!r}"


def test_recording_an_event_warns_that_metadata_is_not_scanned(mcp_tools: dict[str, object]) -> None:
    """An agent reads the tool description and nothing else.

    Metadata is indexed and filterable, which is exactly why it is not PII
    scanned or encrypted. An agent that puts a customer email in a metadata
    value has put it where the scanner never looks, and the only place it could
    have learned otherwise is here.
    """
    description = (getattr(mcp_tools["record_session_event"], "description", "") or "").lower()
    assert "not scanned" in description or "not sensitive" in description or "sensitive" in description
