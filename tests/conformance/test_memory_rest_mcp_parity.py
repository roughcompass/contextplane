"""Both transports agree, and the tool registry agrees with both.

Started as a memory-only parity check and now carries the surface-wide half
E7-T2 asked for, in this file rather than a second one: two parity gates
disagreeing about what parity means is worse than one covering less, and the
actor-identifier rule below is inherited by the wider check rather than restated
beside it.

Session memory must be reachable over both transports, with the same shape.

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
import json
import pathlib
import re
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
    from contextplane.api.mcp.server import create_contextplane_mcp_server

    server = create_contextplane_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        workspace_service=MagicMock(),
    )
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_every_memory_operation_exists_over_mcp(mcp_tools: dict[str, object]) -> None:
    missing = _MEMORY_TOOLS - set(mcp_tools)
    assert not missing, f"missing from the MCP surface: {sorted(missing)}"


def test_every_memory_operation_exists_over_rest() -> None:
    from contextplane.api.routers import memory

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


# --- E7-T2: the registry agrees with the contract and with the docs -------------


_REGISTRY_PATH = pathlib.Path(__file__).resolve().parents[2] / "contextplane" / "api" / "mcp" / "tool_registry.json"
_SPEC_PATH = pathlib.Path(__file__).resolve().parents[2] / "openapi.json"
_DOCS_PATH = pathlib.Path(__file__).resolve().parents[2] / "docs" / "05-reference" / "02-mcp-tools.md"

#: Tools with no reference section today. Pinned so the number can only fall:
#: documenting one and forgetting this list is a one-line fix, and adding an
#: undocumented tool is a failure rather than a silent widening.
#:
#: Not an exemption list of names, deliberately. A name list invites appending
#: to it; a count makes growth the thing that fails.
_UNDOCUMENTED_EXTENDED_TOOLS = 20


def _registry() -> dict[str, object]:
    return json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))


def _documented_tool_names() -> set[str]:
    """Every tool the reference documents, at any heading depth.

    Both `##` and `###`, because the file groups related tools under a section
    heading and documents each under it -- and a check that only read `##` would
    report the entire session-memory surface as undocumented. It did, on the
    first attempt here, which is why this reads both.
    """
    doc = _DOCS_PATH.read_text(encoding="utf-8")
    return {match.group(1) for match in re.finditer(r"^#{2,4} ([a-z][a-z0-9_]*)\s*$", doc, re.M)}


def test_every_registry_rest_mapping_names_an_operation_the_contract_has() -> None:
    """A core tool names the REST operation it mirrors, and that operation exists.

    The mapping is what makes the registry useful to somebody reconciling the two
    surfaces. A mapping to a path the service does not serve is worse than none:
    it reads as a promise that the same call is available over HTTP.
    """
    spec = json.loads(_SPEC_PATH.read_text(encoding="utf-8"))
    paths = spec["paths"]

    broken: list[str] = []
    checked = 0
    for entry in _registry()["tools"]:  # type: ignore[index]
        rest = entry.get("rest")
        if not rest:
            continue
        checked += 1
        method, _, path = str(rest).partition(" ")
        if path not in paths:
            broken.append(f"{entry['name']}: {rest} -- no such path in the contract")
        elif method.lower() not in paths[path]:
            broken.append(f"{entry['name']}: {rest} -- the contract has no {method} on that path")

    assert checked, "no registry entry carries a REST mapping, so this gate checked nothing"
    assert not broken, "registry entries naming operations the contract does not have:\n" + "\n".join(broken)


#: Tools whose actor parameter names a *patient* rather than a *principal* --
#: the thing being acted on, never the thing claiming to act. Both grant and
#: revoke a second actor's participation in a task, so the parameter is the
#: operation's object and cannot be removed without removing delegation.
#:
#: Pinned for equality rather than as a subset, deliberately. A third tool
#: appearing fails; one of these disappearing also fails. Either is a change
#: somebody should have to make on purpose.
#:
#: The four conditions a tool must meet to be added here are in the test's
#: docstring. Read them before editing this set.
_ACTOR_PARAMETER_IS_A_PATIENT: frozenset[str] = frozenset({"grant_intent_participation", "revoke_intent_participation"})


def test_no_tool_on_any_surface_accepts_an_actor_identifier(mcp_tools: dict[str, object]) -> None:
    """The memory rule above, applied to everything, with one scoped exemption.

    That rule was written for session memory, where the credential is the only
    thing scoping a read. The reasoning is narrower than the risk: any tool that
    lets a caller name an actor is a tool that lets one agent act as another, and
    which surfaces have a visibility filter downstream is not something a
    reviewer should have to check per tool.

    Inherited rather than restated -- the memory test stays because it explains
    why, and this one makes the rule general.

    **Why the exemption is not a hole, and why the rule inverts inside it.** For
    session memory the *absence* of the parameter is the control: there is
    nothing downstream that would refuse a caller who named a colleague. For a
    participation grant the parameter's *presence* is what the owner check
    operates on. A reviewer pattern-matching on the parameter name gets this
    backwards, which is why the conditions are written out rather than implied.

    A tool belongs in the exemption only when all four hold:

    1. No path reads the actor parameter to decide *who is asking*. Authority
       comes from ``ctx.actor_id``, which is minted from the validated JWT and
       which no tool argument can reach.
    2. The caller is authorized at a privilege strictly *above* what the
       operation confers. ``_require_owner`` refuses anyone whose active role on
       that task is not owner -- reading a task does not confer the right to
       widen its audience.
    3. That check lives in a service both transports call, not in an adapter, so
       neither REST nor MCP can skip it.
    4. The operation cannot name the caller. Self-grant is refused by the
       contract dataclass and again by the ``ck_grant_not_self`` check
       constraint.

    This test exempts the *name*. The invariant itself is carried by
    ``tests/integration/test_intent_memory_surfaces.py::
    test_a_participant_who_is_not_an_owner_cannot_widen_the_audience``, which is
    the test that would fail if the exemption ever became one.
    """
    offenders: list[str] = []
    exempt_seen: set[str] = set()
    for name, tool in sorted(mcp_tools.items()):
        schema = getattr(tool, "inputSchema", {}) or {}
        for parameter in schema.get("properties") or {}:
            if "actor" not in parameter.lower():
                continue
            if name in _ACTOR_PARAMETER_IS_A_PATIENT:
                exempt_seen.add(name)
                continue
            offenders.append(f"{name} accepts {parameter!r}")

    assert not offenders, "tools accepting an actor identifier:\n" + "\n".join(offenders)
    # Equality, not subset: an exemption for a tool that no longer takes the
    # parameter is an exemption nobody is checking, and it is the kind of line
    # that survives long enough for the next author to read it as precedent.
    assert exempt_seen == _ACTOR_PARAMETER_IS_A_PATIENT, (
        "the exemption set no longer matches the tools that need it; "
        f"pinned {sorted(_ACTOR_PARAMETER_IS_A_PATIENT)}, found {sorted(exempt_seen)}"
    )


def test_every_tool_a_default_connection_exposes_is_documented() -> None:
    """The core surface is what an agent meets first, so it is the surface that
    must be written down.

    The extended tiers are held to a ratchet below rather than to this bar: the
    gap there is real and pre-existing, and failing the build on it would only
    teach somebody to widen the exemption.
    """
    documented = _documented_tool_names()
    core = {entry["name"] for entry in _registry()["tools"] if entry["tier"] == "core"}  # type: ignore[index]

    missing = sorted(core - documented)

    assert not missing, (
        f"a default connection exposes {sorted(core)} and these have no reference section: {missing}. "
        "An agent's first encounter with this product is the core surface."
    )


def test_nothing_documented_has_been_removed_from_the_server() -> None:
    """A documented tool that no longer registers is a promise the server does
    not keep, and the reader has no way to discover that from the page."""
    registered = {entry["name"] for entry in _registry()["tools"]}  # type: ignore[index]
    documented = _documented_tool_names()

    phantom = sorted(documented - registered)

    assert not phantom, f"documented and not registered: {phantom}"


def test_the_undocumented_extended_surface_does_not_grow() -> None:
    """A ratchet, not a target.

    Twenty extended tools have no reference section. That is a real gap and
    fixing it is not this task; what this refuses is *adding* to it. Documenting
    one and forgetting the constant here fails too, which is the right way round
    -- the failure says "you did better than the pin" and takes a one-line fix.
    """
    registered = _registry()["tools"]
    documented = _documented_tool_names()
    undocumented = sorted(
        entry["name"]
        for entry in registered
        if entry["tier"] != "core" and entry["name"] not in documented  # type: ignore[index]
    )

    assert len(undocumented) <= _UNDOCUMENTED_EXTENDED_TOOLS, (
        f"{len(undocumented)} extended tools are undocumented, up from {_UNDOCUMENTED_EXTENDED_TOOLS}: "
        f"{undocumented}. A new tool arrives with its reference section."
    )
    assert len(undocumented) == _UNDOCUMENTED_EXTENDED_TOOLS, (
        f"{len(undocumented)} extended tools are undocumented, down from "
        f"{_UNDOCUMENTED_EXTENDED_TOOLS}. Lower the constant to lock the improvement in."
    )


def test_recording_an_event_warns_that_metadata_is_not_scanned(mcp_tools: dict[str, object]) -> None:
    """An agent reads the tool description and nothing else.

    Metadata is indexed and filterable, which is exactly why it is not PII
    scanned or encrypted. An agent that puts a customer email in a metadata
    value has put it where the scanner never looks, and the only place it could
    have learned otherwise is here.
    """
    description = (getattr(mcp_tools["record_session_event"], "description", "") or "").lower()
    assert "not scanned" in description or "not sensitive" in description or "sensitive" in description
