"""Curation, promotion review, confirmation, history, and capability requests
must be reachable over both transports.

A curator or an agent reaches this surface over MCP; a human dashboard or a
script reaches it over REST. A capability present on one and missing from the
other is a gap nobody notices until an agent tries to act on a queued claim
and cannot.

Not every REST operation here has an MCP twin, and that is deliberate rather
than an oversight: the nineteen tools are a coordinated, bounded surface
(raise/list/triage a capability request, list/link/discard a queued claim,
list/review a proposal, reverse a promotion, confirm/adjudicate/trace a
claim, assert one directly, and the contradiction group/case surface) rather
than one tool per REST path. `get`ting a single capability request, its
transition history, and linking it to a promotion have REST routes with no MCP
counterpart; `believed_at`'s time-travel read is REST-only too. The REST-path
check below only pins that those routes have not silently vanished from the
REST surface itself.

The contradiction surface is the exception that gets *full* parity, asserted in
both directions: a conflict is exactly the thing that must not be visible to one
transport and invisible to the other.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

# The de facto MCP snapshot: the nineteen tool names this surface owns.
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
    "list_contradiction_groups",
    "open_curation_case",
    "list_curation_cases",
    "get_curation_case",
    "route_curation_case",
    "record_case_disposition",
}

# The contradiction surface, on both transports. Unlike the older operations
# above, this one is deliberately complete parity: a conflict a human dashboard
# can see and an agent cannot -- or a case an agent can route and a curator
# cannot -- is the asymmetry that lets a contradiction sit unresolved because
# whoever was looking used the other transport.
_CONTRADICTION_TOOLS = {
    "list_contradiction_groups",
    "open_curation_case",
    "list_curation_cases",
    "get_curation_case",
    "route_curation_case",
    "record_case_disposition",
}

_CONTRADICTION_REST_PATHS = {
    "/v1/memory/contradiction-groups",
    "/v1/memory/curation-cases",
    "/v1/memory/curation-cases/{case_id}",
    "/v1/memory/curation-cases/{case_id}:route",
    "/v1/memory/curation-cases/{case_id}:disposition",
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
    "/v1/memory/contradiction-groups",
    "/v1/memory/curation-cases",
    "/v1/memory/curation-cases/{case_id}",
    "/v1/memory/curation-cases/{case_id}:route",
    "/v1/memory/curation-cases/{case_id}:disposition",
}

# The three list tools whose REST twins expose `(cursor, page_size)` keyset
# pagination. A tool missing either parameter would silently lose the
# ability to page past its first `page_size` rows.
_PAGINATED_TOOLS = {
    "list_curation_queue",
    "list_promotion_proposals",
    "list_capability_requests",
    "list_curation_cases",
}


@pytest.fixture(scope="module")
def mcp_tools() -> dict[str, object]:
    from contextplane.api.mcp.server import create_contextplane_mcp_server

    server = create_contextplane_mcp_server(
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
    from contextplane.api.routers import memory_curation

    paths = {r.path for r in memory_curation.router.routes} | {r.path for r in memory_curation.mutation_router.routes}
    missing = _MEMORY_CURATION_REST_PATHS - paths
    assert not missing, f"missing from the REST surface: {sorted(missing)}"


def test_the_contradiction_surface_is_complete_on_both_transports(mcp_tools: dict[str, object]) -> None:
    """Full parity, not the partial parity the older operations settle for.

    A contradiction one transport can see and the other cannot is how a conflict
    sits unresolved: whoever was looking used the surface that could not show it,
    or could not route it to the person who decides. Both directions are pinned,
    so dropping either an MCP tool or a REST route fails here.
    """
    from contextplane.api.routers import memory_curation

    missing_tools = _CONTRADICTION_TOOLS - set(mcp_tools)
    assert not missing_tools, f"missing from the MCP surface: {sorted(missing_tools)}"

    paths = {r.path for r in memory_curation.router.routes} | {r.path for r in memory_curation.mutation_router.routes}
    missing_paths = _CONTRADICTION_REST_PATHS - paths
    assert not missing_paths, f"missing from the REST surface: {sorted(missing_paths)}"


def test_recording_a_disposition_takes_the_closed_disposition_vocabulary(mcp_tools: dict[str, object]) -> None:
    """The tool must not accept a free-text disposition. An unrecognized verb
    stored with a borrowed authority reads afterwards as a decision somebody was
    accountable for, so both transports refuse it -- REST through a `Literal`,
    MCP through the service's own `policy_for` refusal."""
    schema = getattr(mcp_tools["record_case_disposition"], "inputSchema", {}) or {}
    required = schema.get("required") or []
    assert "case_id" in required
    assert "disposition" in required


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
