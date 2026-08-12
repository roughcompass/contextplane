"""Every surface published by the task-memory, context-resolve and receipt slices
must exist on both transports, with the same semantics.

Each slice published its own REST router and its own MCP tool module. Parity
across all of them is one property, and proving it inside each slice does not
work: **a slice's own parity test compares the operations that exist on both
sides, so an operation missing from one transport is invisible to it.** That is
not a hypothetical: on its first run this file found two receipt reads served
over REST with no tool behind them, and they were published rather than
excused.

**The mapping below is declared, not derived.** A test that discovered the
pairing by matching names would agree with whatever the code happens to do, and
agree just as readily after somebody drops a tool. Writing the pairs out means
adding a route without its tool fails here, which is the only failure mode this
task exists to catch.

**Auth is not a parameter on either transport.** A tool taking a tenant or actor
identifier would let a caller name somebody else and read their material; the
credential is the only thing that may scope a call. That check is separate from
the pairing because it fails differently: the pairing catches an absent
capability, this catches a present one that is too powerful.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

# --------------------------------------------------------------------------
# The surfaces, paired by hand
# --------------------------------------------------------------------------

#: (REST method, REST path template, MCP tool name) for every operation the
#: three slices publish. One row per operation, not per route: a path serving
#: GET and POST is two capabilities and needs two tools.
_PAIRS: tuple[tuple[str, str, str], ...] = (
    # Task memory — participants
    ("GET", "/v1/intents/{intent_id}/participants", "list_intent_participants"),
    ("POST", "/v1/intents/{intent_id}/participants", "grant_intent_participation"),
    ("DELETE", "/v1/intents/{intent_id}/participants/{actor_id}", "revoke_intent_participation"),
    # Task memory — the checkpoint chain
    ("POST", "/v1/intents/{intent_id}/checkpoints", "append_intent_checkpoint"),
    ("GET", "/v1/intents/{intent_id}/checkpoints/{checkpoint_id}", "get_intent_checkpoint"),
    ("GET", "/v1/checkpoints/by-digest/{digest}", "get_intent_checkpoint_by_digest"),
    # Context resolution
    ("POST", "/v1/context/resolve", "registry_resolve_context"),
    # Receipts and resume
    ("GET", "/v1/receipts/by-reference", "find_receipts_by_reference"),
    ("GET", "/v1/receipts/{receipt_id}", "get_context_receipt"),
    ("GET", "/v1/receipts/{receipt_id}/exclusions", "get_receipt_exclusions"),
    ("GET", "/v1/receipts/{receipt_id}/references", "get_receipt_references"),
    ("POST", "/v1/context/resume", "resume_context"),
)

#: REST operations in these slices that have **no MCP tool**. A defect list, not
#: a configuration knob: every entry would be a capability a REST caller has and
#: an agent does not, on surfaces whose contract says both transports behave
#: equivalently. The test below asserts it is empty.
#:
#: It is empty, and it was not. `GET /v1/receipts/{receipt_id}` and
#: `GET /v1/receipts/{receipt_id}/references` were served over REST with no tool
#: behind them, so an agent could resolve context, receive a receipt id, and have
#: no way to open the evidence that id named. Publishing the two tools emptied
#: this rather than an allowlist entry silencing it.
_UNPAIRED_REST: tuple[tuple[str, str], ...] = ()

#: Path prefixes owned by these slices. Used to decide which REST operations are
#: in scope, so an unrelated router appearing later does not fail this file.
_IN_SCOPE_PREFIXES = (
    "/v1/tasks/",
    "/v1/checkpoints/",
    "/v1/context/",
    "/v1/receipts/",
)

#: ARC publishes its own receipts under a separate prefix with its own parity
#: suite. Excluded by name rather than by prefix arithmetic, so adding an ARC
#: route cannot silently widen or narrow this file's scope.
_OTHER_SLICE_PREFIXES = ("/v1/arc/",)

#: Nothing on either transport may take these. The credential scopes the call.
_FORBIDDEN_TOOL_PARAMS = frozenset({"tenant_id", "actor_id", "tenant_slug", "on_behalf_of"})


@pytest.fixture(scope="module")
def mcp_tools() -> dict[str, object]:
    """Every registered tool, from the real factory.

    Mocks stand in for the services because this file asks what the surface
    *is*, not what it returns — and a factory that needs a live database to
    enumerate its own tools would make this check impossible to run early.
    """
    from contextplane.api.mcp.server import create_contextplane_mcp_server

    server = create_contextplane_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        workspace_service=MagicMock(),
    )
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


@pytest.fixture(scope="module")
def rest_operations() -> set[tuple[str, str]]:
    """Every in-scope REST operation, read off the routers themselves.

    Taken from the router objects rather than from a constructed app, because
    `create_app()` needs a `database_url` and this file asks a question about
    shape that should be answerable without a database. Taken from the routers
    rather than from the committed `openapi.json` for the opposite reason: a
    stale spec would let this file agree with a surface that no longer exists.

    The mount itself is proved by each slice's own integration suite, which
    fails with `404` if `wiring/routes.py` stops naming a router.
    """
    from contextplane.api.routers import context as context_router
    from contextplane.api.routers import receipts as receipts_router
    from contextplane.api.routers import intent_memory as task_memory_router

    found: set[tuple[str, str]] = set()
    for module in (task_memory_router, context_router, receipts_router):
        for route in module.router.routes:
            # `route.path` already carries the router's prefix; concatenating it
            # again yields `/v1/v1/...`, which matches nothing and fails as a
            # missing route rather than as the bug it is.
            path = getattr(route, "path", "")
            if any(path.startswith(prefix) for prefix in _OTHER_SLICE_PREFIXES):
                continue
            if not any(path.startswith(prefix) for prefix in _IN_SCOPE_PREFIXES):
                continue
            for method in getattr(route, "methods", set()) or set():
                if method in {"HEAD", "OPTIONS"}:
                    continue
                found.add((method, path))
    return found


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "tool"), _PAIRS, ids=lambda v: str(v))
def test_each_paired_rest_operation_exists(
    method: str, path: str, tool: str, rest_operations: set[tuple[str, str]]
) -> None:
    """The REST half of every declared pair is mounted.

    Fails if a router stops being included, which is the failure three tasks in
    this phase shipped and no gate caught.
    """
    assert (method, path) in rest_operations, f"{method} {path} is declared but not served"


@pytest.mark.parametrize(("method", "path", "tool"), _PAIRS, ids=lambda v: str(v))
def test_each_paired_mcp_tool_is_registered(method: str, path: str, tool: str, mcp_tools: dict[str, object]) -> None:
    """The MCP half of every declared pair is registered.

    MCP registration is static and explicit, so a tool module nobody names is
    unreachable code that reviews perfectly.
    """
    assert tool in mcp_tools, f"{tool} pairs with {method} {path} but is not registered"


def test_no_rest_operation_in_these_slices_lacks_a_tool(rest_operations: set[tuple[str, str]]) -> None:
    """Every in-scope REST operation is either paired or a known defect.

    This is the completeness half, and it is what a per-slice parity test cannot
    do: comparing the operations that exist on both transports says nothing
    about one that exists on only a single transport.
    """
    paired = {(method, path) for method, path, _ in _PAIRS}
    unaccounted = rest_operations - paired - set(_UNPAIRED_REST)

    assert not unaccounted, (
        "REST operations with no MCP tool and no recorded exemption: "
        f"{sorted(unaccounted)}. Publish the tool, or add the pair above."
    )


def test_the_unpaired_defect_list_is_empty() -> None:
    """The gap this file was written to find, asserted as a defect.

    `GET /v1/receipts/{receipt_id}` and `GET /v1/receipts/{receipt_id}/references`
    are served over REST and have no MCP tool. An agent can find receipts by
    reference and read one's exclusions, but cannot read the receipt itself or
    the references it bound — on a surface whose contract says the two transports
    behave equivalently.

    Kept as an assertion rather than a passing allowlist because an allowlist
    would let a release gate go green over a contract that is not met. The two
    tools were published instead, which is the only thing that actually closes
    it, and this assertion is what will notice if a future route arrives without
    its tool.
    """
    assert not _UNPAIRED_REST, (
        "REST operations with no MCP equivalent: "
        f"{sorted(_UNPAIRED_REST)}. Both transports must publish the same capabilities."
    )


def test_every_in_scope_tool_pairs_with_a_rest_operation(mcp_tools: dict[str, object]) -> None:
    """The other direction: no tool without a route.

    A capability an agent has and a human cannot audit over HTTP is the same
    divergence read from the other side.
    """
    paired_tools = {tool for _, _, tool in _PAIRS}
    slice_tools = {
        name
        for name in mcp_tools
        if any(token in name for token in ("task_participant", "intent_checkpoint", "receipt", "resume_context"))
        or name in {"registry_resolve_context", "list_intent_participants", "grant_intent_participation"}
    }
    # ARC's own context/receipt tools have their own parity suite.
    slice_tools -= {name for name in slice_tools if name.startswith("arc_")}

    unaccounted = slice_tools - paired_tools
    assert not unaccounted, f"MCP tools with no paired REST operation: {sorted(unaccounted)}"


# --------------------------------------------------------------------------
# Shared semantics
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "tool"), _PAIRS, ids=lambda v: str(v))
def test_no_tool_accepts_an_identity_parameter(method: str, path: str, tool: str, mcp_tools: dict[str, object]) -> None:
    """A caller may not name whose material to read.

    The credential scopes every call on both transports. A tool taking
    `tenant_id` or `actor_id` would let one agent read another's tasks, and no
    layer beneath would refuse it — the parameter would look like ordinary
    addressing.

    `actor_id` in a *path* is exempt on purpose: revoking a participant names
    the actor being revoked, which is the object of the operation rather than
    the identity of the caller.
    """
    if tool not in mcp_tools:
        pytest.skip(f"{tool} is not registered; the pairing test above owns that failure")

    schema = getattr(mcp_tools[tool], "inputSchema", None) or {}
    params = set(schema.get("properties", {}))

    offending = params & _FORBIDDEN_TOOL_PARAMS
    if tool in {"grant_intent_participation", "revoke_intent_participation"}:
        # Both name the actor whose participation is being changed. That is the
        # object of the operation, not the identity of the caller -- and the
        # service still checks the *caller's* task role before honouring it.
        offending -= {"actor_id"}

    assert not offending, f"{tool} accepts {sorted(offending)}; the credential is what scopes a call"


@pytest.mark.parametrize(("method", "path", "tool"), _PAIRS, ids=lambda v: str(v))
def test_every_tool_documents_what_it_returns(method: str, path: str, tool: str, mcp_tools: dict[str, object]) -> None:
    """An agent chooses a tool from its description, so the description is the surface.

    A tool whose docstring does not say what comes back is one an agent has to
    call to find out, which on a write path is not a safe way to find out.
    """
    if tool not in mcp_tools:
        pytest.skip(f"{tool} is not registered; the pairing test above owns that failure")

    description = (getattr(mcp_tools[tool], "description", "") or "").lower()
    assert description.strip(), f"{tool} has no description"
    assert "returns" in description, f"{tool} does not say what it returns"
