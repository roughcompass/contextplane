"""Curation, promotion review, confirmation, history, and capability requests
must be reachable over both transports.

A curator or an agent reaches this surface over MCP; a human dashboard or a
script reaches it over REST. A capability present on one and missing from the
other is a gap nobody notices until an agent tries to act on a queued claim
and cannot.

Not every REST operation here has an MCP twin, and that is deliberate rather
than an oversight: the thirteen tools are a coordinated, bounded surface
(raise/list/triage a capability request, list/link/discard a queued claim,
list/review a proposal, reverse a promotion, confirm/adjudicate/trace a
claim, assert one directly) rather than one tool per REST path. `get`ting a
single capability request, its transition history, and linking it to a
promotion have REST routes with no MCP counterpart; `believed_at`'s
time-travel read is REST-only too. The REST-path check below only pins that
those routes have not silently vanished from the REST surface itself.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

# The de facto MCP snapshot: the thirteen tool names this surface owns.
_MEMORY_CURATION_TOOLS = {
    "assert_claim",
    "list_curation_queue",
    "link_claim_subject",
    "discard_claim",
    "list_promotion_proposals",
    "review_promotion_proposal",
    "reverse_promotion",
    "confirm_claim",
    "adjudicate_claim",
    "get_claim_history",
    "raise_capability_request",
    "list_capability_requests",
    "triage_capability_request",
}

# Every path api/routers/memory_curation.py registers, across its two
# routers (`router` and the PATCH-carrying `mutation_router`).
_MEMORY_CURATION_REST_PATHS = {
    "/v1/memory/curation-queue",
    "/v1/memory/claims/{claim_id}:link",
    "/v1/memory/claims/{claim_id}:discard",
    "/v1/memory/promotion-proposals",
    "/v1/memory/promotion-proposals/{proposal_id}",
    "/v1/memory/promotions/{promotion_id}:reverse",
    "/v1/memory/claims/{claim_id}:confirm",
    "/v1/memory/claims/{claim_id}:adjudicate",
    "/v1/memory/claims/{claim_id}/history",
    "/v1/memory/claims/believed",
    "/v1/memory/capability-requests",
    "/v1/memory/capability-requests/{request_id}",
    "/v1/memory/capability-requests/{request_id}/history",
    "/v1/memory/capability-requests/{request_id}:link-promotion",
    "/v1/memory/claims",
}

# The three list tools whose REST twins expose `(cursor, page_size)` keyset
# pagination. A tool missing either parameter would silently lose the
# ability to page past its first `page_size` rows.
_PAGINATED_TOOLS = {"list_curation_queue", "list_promotion_proposals", "list_capability_requests"}


@pytest.fixture(scope="module")
def mcp_tools() -> dict[str, object]:
    from registry.api.mcp.server import create_registry_mcp_server

    server = create_registry_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        workspace_service=MagicMock(),
    )
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_every_memory_curation_tool_exists_over_mcp(mcp_tools: dict[str, object]) -> None:
    missing = _MEMORY_CURATION_TOOLS - set(mcp_tools)
    assert not missing, f"missing from the MCP surface: {sorted(missing)}"


def test_every_memory_curation_operation_exists_over_rest() -> None:
    from registry.api.routers import memory_curation

    paths = {r.path for r in memory_curation.router.routes} | {r.path for r in memory_curation.mutation_router.routes}
    missing = _MEMORY_CURATION_REST_PATHS - paths
    assert not missing, f"missing from the REST surface: {sorted(missing)}"


def test_paginated_tools_accept_cursor_and_page_size(mcp_tools: dict[str, object]) -> None:
    """REST/MCP pagination parity: a list tool without both parameters would
    strand a caller on the first page with no way to ask for the next one."""
    for name in sorted(_PAGINATED_TOOLS):
        schema = getattr(mcp_tools[name], "inputSchema", {}) or {}
        properties = schema.get("properties") or {}
        assert "cursor" in properties, f"{name} is missing cursor"
        assert "page_size" in properties, f"{name} is missing page_size"


def test_assert_claim_requires_evidence(mcp_tools: dict[str, object]) -> None:
    """`assert_claim`'s evidence argument is required, matching the REST
    route's own `Field(min_length=1)` -- a claim with no provenance is not
    something either surface may stage."""
    schema = getattr(mcp_tools["assert_claim"], "inputSchema", {}) or {}
    assert "evidence" in (schema.get("required") or [])
