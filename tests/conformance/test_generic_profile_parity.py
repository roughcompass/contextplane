"""The generic profile surface says the same thing over REST and over MCP.

Two transports over one set of rules is two chances for the rules to diverge, and
the divergence that matters is not a missing endpoint — that is obvious the first
time somebody calls it. It is a transport that quietly accepts something the other
refuses: an MCP tool that takes an `authority` argument, or a REST body that
defaults an intent the tool requires. Either one turns "the agent surface is
safe" into a claim about one transport rather than about the platform.

**The pairing is declared, not derived.** A test that discovered the mapping by
matching names would agree with whatever shipped, including a tool paired with the
wrong route. `_PAIRS` below is written down, so a route that loses its tool fails
here rather than quietly dropping out of the comparison.

**Two exclusions are deliberate and are asserted as such.** Profile administration
and ownership transitions are REST-only administrative surfaces by design. Listing
them as *expected* exclusions rather than leaving them out means a third surface
appearing unpaired is a failure, and means somebody removing the exclusion has to
say so — the list cannot silently absorb a new gap.

**The canonical shortcut is planted, not argued about.** The safety property the
whole generic surface rests on is that an ordinary agent cannot write canonical
data. This file constructs a tool call that tries — asking for the canonical
effect without an approval — and requires both a refusal and no side effect. A
refusal alone would not settle it: a path that raised *after* writing would still
raise.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from contextplane.api.mcp.tools import entities as entity_tools
from contextplane.api.mcp.tools import relationships as relationship_tools
from contextplane.entities.write_intent import (
    INTENT_AUTHORIZED_APPROVAL,
    PROFILE_WRITE_INTENTS,
    RESERVED_AUTHORITY_FIELDS,
)

_SNAPSHOTS = Path(__file__).parent / "snapshots"

#: The declared REST/MCP pairing for the generic profile surface.
#:
#: Only the *write* operations pair. A read has no equivalent hazard — an agent
#: reading a row it is entitled to see is not a governance question — and pairing
#: reads would mean inventing tools nobody asked for just to satisfy a symmetry
#: this file made up.
_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("POST", "/v1/entities", "assert_entity"),
    ("POST", "/v1/relationships", "assert_relationship"),
)

#: REST operations on the generic surface with no tool, and why. Each entry is a
#: decision somebody made, not a gap somebody tolerated.
_DELIBERATE_REST_ONLY: dict[tuple[str, str], str] = {
    ("GET", "/v1/entities/{entity_id}"): "a read; no governance hazard, so no tool is invented for symmetry",
    ("PATCH", "/v1/entities/{entity_id}"): (
        "an update routes exactly as a create does; the tool covers the routing, and a second tool would be "
        "two places for one rule to drift"
    ),
    ("GET", "/v1/entities:resolve"): "a read",
    ("POST", "/v1/entities/{entity_id}:validate-readiness"): "a read that computes; writes nothing",
    ("GET", "/v1/relationships/{relationship_id}"): "a read",
    ("PATCH", "/v1/relationships/{relationship_id}"): "an update, routed as a create is",
    ("POST", "/v1/relationships:query"): "a bounded read",
}

#: Nothing on either transport may take these — the credential scopes the call.
_FORBIDDEN_PARAMS = frozenset({"tenant_id", "actor_id", "tenant_slug", "on_behalf_of"})


@pytest.fixture(scope="module")
def mcp_tools() -> dict[str, Any]:
    """Every registered tool, from the real factory."""
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
    """Every generic-surface REST operation, read off the routers themselves.

    From the routers rather than the committed spec: a stale spec would let this
    file agree with a surface that no longer exists.
    """
    from contextplane.api.routers import entities as entity_router
    from contextplane.api.routers import relationships as relationship_router

    found: set[tuple[str, str]] = set()
    for module in (entity_router, relationship_router):
        for route in module.router.routes:
            path = getattr(route, "path", "")
            for method in getattr(route, "methods", set()) or set():
                if method in {"HEAD", "OPTIONS"}:
                    continue
                found.add((method, path))
    return found


# --- the declared pairing -------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "tool"), _PAIRS, ids=lambda v: str(v))
def test_each_paired_rest_operation_is_served(
    method: str, path: str, tool: str, rest_operations: set[tuple[str, str]]
) -> None:
    assert (method, path) in rest_operations, f"{method} {path} is declared but not served"


@pytest.mark.parametrize(("method", "path", "tool"), _PAIRS, ids=lambda v: str(v))
def test_each_paired_mcp_tool_is_registered(method: str, path: str, tool: str, mcp_tools: dict[str, Any]) -> None:
    assert tool in mcp_tools, f"{tool} is declared as the twin of {method} {path} but is not registered"


def test_every_generic_write_operation_is_paired_or_deliberately_excluded(
    rest_operations: set[tuple[str, str]],
) -> None:
    """No operation may sit outside both the pairing and the exclusion list.

    This is the test that makes the exclusions load-bearing. A new route added
    without a tool and without a stated reason fails here, which is the moment to
    decide rather than six months later when an agent finds it.
    """
    paired = {(method, path) for method, path, _ in _PAIRS}
    unaccounted = rest_operations - paired - set(_DELIBERATE_REST_ONLY)

    assert not unaccounted, (
        f"these generic-surface operations are neither paired with a tool nor listed as deliberately "
        f"REST-only: {sorted(unaccounted)}"
    )


def test_every_declared_exclusion_still_exists(rest_operations: set[tuple[str, str]]) -> None:
    """An exclusion for a route nobody serves reads as more coverage than there is."""
    stale = set(_DELIBERATE_REST_ONLY) - rest_operations

    assert not stale, f"these exclusions name operations that are no longer served: {sorted(stale)}"


def test_administrative_surfaces_are_excluded_by_decision_not_by_omission() -> None:
    """Profile administration and ownership transitions are REST-only by design.

    Asserted as a statement about the pairing rather than about those routers, so
    that adding an `assert_profile_binding` tool later fails this test and forces
    the decision to be revisited rather than drifting.
    """
    tools = {tool for _, _, tool in _PAIRS}

    assert not any("profile" in tool or "binding" in tool or "ownership" in tool for tool in tools), (
        "profile administration and ownership transitions are deliberately REST-only administrative "
        "surfaces; pairing one with a tool is a decision to make explicitly, not by adding it here"
    )


# --- information equivalence ----------------------------------------------------------


@pytest.mark.parametrize("tool_fn", [entity_tools.assert_entity, relationship_tools.assert_relationship])
def test_intent_is_a_required_argument_with_no_default(tool_fn: Any) -> None:
    """A default would route an agent's write somewhere it did not choose.

    Checked on the signature rather than by calling, because the hazard is the
    *shape* of the tool: a default is visible to every caller and would be used by
    the ones who read the signature and stopped there.
    """
    parameter = inspect.signature(tool_fn).parameters["intent"]

    assert parameter.default is inspect.Parameter.empty, f"{tool_fn.__name__} defaults its intent"


@pytest.mark.parametrize("tool_fn", [entity_tools.assert_entity, relationship_tools.assert_relationship])
def test_no_tool_takes_a_caller_asserted_authority_field(tool_fn: Any) -> None:
    """The REST body is screened for these; the tool schema must not offer them.

    A tool argument named `authority` would be the same defect the REST refusal
    exists to prevent, arriving through the door nobody thought to guard.
    """
    taken = set(inspect.signature(tool_fn).parameters)
    offered = taken & set(RESERVED_AUTHORITY_FIELDS)

    assert not offered, f"{tool_fn.__name__} accepts caller-asserted authority: {sorted(offered)}"


@pytest.mark.parametrize("tool_fn", [entity_tools.assert_entity, relationship_tools.assert_relationship])
def test_no_tool_takes_a_caller_supplied_identity(tool_fn: Any) -> None:
    """The credential scopes the call on both transports."""
    taken = set(inspect.signature(tool_fn).parameters)
    offered = taken & _FORBIDDEN_PARAMS

    assert not offered, f"{tool_fn.__name__} accepts caller-supplied identity: {sorted(offered)}"


@pytest.mark.parametrize("tool_fn", [entity_tools.assert_entity, relationship_tools.assert_relationship])
def test_each_tool_reports_the_effect_its_write_had(tool_fn: Any) -> None:
    """A tool that returned only success would let an observation read as canonical.

    The REST response carries `effect`; the tool's JSON must too, or the two
    transports are not information-equivalent about the one thing that matters
    most on the agent surface.
    """
    source = inspect.getsource(tool_fn)

    assert '"effect"' in source, f"{tool_fn.__name__} does not report which effect its write had"
    assert '"canonical"' in source, f"{tool_fn.__name__} does not say whether the write was canonical"


def test_both_transports_share_one_intent_vocabulary() -> None:
    """Three intents, defined once. A fourth on one transport is a divergence."""
    assert set(PROFILE_WRITE_INTENTS) == {"observation", "request", "authorized_approval"}


# --- the planted canonical shortcut ---------------------------------------------------


@pytest.mark.asyncio
async def test_an_unauthenticated_tool_call_writes_nothing() -> None:
    """A tool call with no credential is refused before it reaches the database.

    The session factory handed in fails loudly if it is used at all, so this
    distinguishes "refused" from "refused after writing" — a path that raised
    afterwards would still raise, and a test asserting only the raise would call
    that a pass.

    This is the *outer* half of the shortcut defence. The inner half — that an
    authenticated caller still cannot reach the canonical effect without an
    approval — is asserted below against the routing decision itself, because
    reaching it through the tool would need a resolved tenant this file
    deliberately does not build.
    """

    class _ExplodingFactory:
        def __call__(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("reached the database")

    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as refused:
        await entity_tools.assert_entity(
            INTENT_AUTHORIZED_APPROVAL,
            "core:capability",
            "shortcut",
            session_factory=_ExplodingFactory(),  # type: ignore[arg-type]
            clock=MagicMock(),
        )

    assert "reached the database" not in str(refused.value)


def test_the_canonical_effect_is_unreachable_without_a_verified_approval() -> None:
    """The planted shortcut: ask for the canonical route holding ordinary authority.

    Both transports route through this one decision, so refusing it here refuses
    it on both. Two shapes are tried — an approval intent under an *observation*
    authority, and an approval intent with no reference at all — because a check
    that only compared the intent would let the first through, and one that only
    required a reference would let the second.
    """
    from contextplane.entities.write_intent import (
        AUTHORITY_OBSERVED_EVIDENCE,
        AUTHORITY_VERIFIED_APPROVAL,
        ProfileWriteAuthority,
        RefusedProfileWrite,
        route_profile_write,
    )

    with pytest.raises(RefusedProfileWrite):
        route_profile_write(
            INTENT_AUTHORIZED_APPROVAL,
            authority=ProfileWriteAuthority(actor_id="agent-1", origin=AUTHORITY_OBSERVED_EVIDENCE),
            approval_reference="borrowed-review",
        )

    with pytest.raises(RefusedProfileWrite):
        ProfileWriteAuthority(actor_id="agent-1", origin=AUTHORITY_VERIFIED_APPROVAL)


# --- snapshots ------------------------------------------------------------------------


def test_the_generic_surface_snapshot_matches_what_is_served(
    rest_operations: set[tuple[str, str]], mcp_tools: dict[str, Any]
) -> None:
    """A committed inventory of the surface, so a change to it shows up in review.

    Regenerate deliberately by deleting the file and re-running; the diff is the
    point. Sorted, so the file is a function of the surface and not of iteration
    order.
    """
    snapshot = _SNAPSHOTS / "generic_profile_surface.json"
    current = {
        "rest_operations": sorted(f"{method} {path}" for method, path in rest_operations),
        "paired_tools": sorted(tool for _, _, tool in _PAIRS),
        "deliberate_rest_only": sorted(f"{method} {path}" for method, path in _DELIBERATE_REST_ONLY),
    }

    if not snapshot.exists():
        snapshot.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    committed = json.loads(snapshot.read_text(encoding="utf-8"))
    assert (
        committed == current
    ), "the generic profile surface changed; review the diff and re-commit the snapshot if intended"


def test_the_paired_tool_schemas_match_their_committed_snapshot(mcp_tools: dict[str, Any]) -> None:
    """A committed schema for each paired tool, so an argument change shows up in review.

    Its own file rather than an entry in `mcp_tools.json`: that snapshot is
    ARC-scoped by construction — it filters to `arc_`-prefixed tools — so these
    tools are correctly absent from it, and adding them would make it describe two
    unrelated surfaces at once.

    The schema, not just the name. A tool that grew an `authority` argument would
    keep its name and pass any check that only looked at the catalogue's keys,
    which is exactly the divergence this file exists to catch.
    """
    snapshot_path = _SNAPSHOTS / "generic_profile_tools.json"
    live = {
        tool: {
            "description": mcp_tools[tool].description,
            "input_schema": mcp_tools[tool].inputSchema,
        }
        for _, _, tool in _PAIRS
    }

    if not snapshot_path.exists():
        snapshot_path.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    committed = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert committed == live, "a paired tool's schema changed; review the diff and re-commit the snapshot if intended"
