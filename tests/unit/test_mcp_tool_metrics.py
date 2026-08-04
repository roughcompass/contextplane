"""MCP tool instrumentation, and the tool contract it must not disturb.

Which tools are actually called is the cheapest high-information number this
service can publish, and it was previously unknown. Instrumenting at the
registration seam buys that for the whole surface at once — but a wrapper sits
between every caller and every handler, so the cost of getting it subtly wrong
is paid by every tool rather than one.

The four properties asserted below are the ones a wrapper silently breaks: name,
description, argument schema including which arguments are required, and async
behaviour. Each is derived from the function object rather than declared, so a
wrapper that fails to forward one changes the tool's public shape without
changing any code that looks like it defines the tool.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP
from prometheus_client import REGISTRY

from registry.api.routers.mcp import create_registry_mcp_server, install_tool_metrics


async def _probe(slug: str, limit: int = 10) -> str:
    """Probe tool used to compare the wrapped and unwrapped contracts."""
    return f"{slug}:{limit}"


def _tools(server: FastMCP) -> dict:
    return {t.name: t for t in asyncio.run(server.list_tools())}


@pytest.fixture(scope="module")
def contracts() -> tuple[object, object]:
    """The same function registered with and without instrumentation."""
    plain = FastMCP(name="plain")
    plain.tool()(_probe)

    wrapped = FastMCP(name="wrapped")
    install_tool_metrics(wrapped)
    wrapped.tool()(_probe)

    return _tools(plain)["_probe"], _tools(wrapped)["_probe"]


def test_the_tool_name_survives(contracts) -> None:
    plain, wrapped = contracts
    assert wrapped.name == plain.name == "_probe"


def test_the_description_still_comes_from_the_original_docstring(contracts) -> None:
    plain, wrapped = contracts
    assert wrapped.description == plain.description
    assert "Probe tool" in (wrapped.description or "")


def test_the_argument_schema_is_unchanged(contracts) -> None:
    plain, wrapped = contracts
    assert wrapped.inputSchema == plain.inputSchema
    assert set(wrapped.inputSchema["properties"]) == {"slug", "limit"}


def test_which_arguments_are_required_is_unchanged(contracts) -> None:
    """Called out separately because it is the half that degrades quietly.

    A wrapper whose signature collapses to (*args, **kwargs) yields a schema
    with no required arguments. Callers keep working, so nothing fails — the
    server just stops rejecting calls that omit `slug`, and the error moves from
    the protocol boundary into the handler.
    """
    plain, wrapped = contracts
    assert wrapped.inputSchema.get("required") == plain.inputSchema.get("required") == ["slug"]


@pytest.mark.asyncio
async def test_the_tool_still_runs_and_returns_its_value() -> None:
    server = FastMCP(name="callable")
    install_tool_metrics(server)
    server.tool()(_probe)

    result = await server.call_tool("_probe", {"slug": "abc"})
    assert "abc:10" in str(result)


@pytest.mark.asyncio
async def test_a_call_moves_both_the_counter_and_the_histogram() -> None:
    server = FastMCP(name="measured")
    install_tool_metrics(server)
    server.tool()(_probe)

    before_calls = _sample("mcp_tool_calls_total", tool="_probe", status="2xx")
    before_time = _sample("mcp_tool_duration_seconds_count", tool="_probe")

    await server.call_tool("_probe", {"slug": "abc"})

    assert _sample("mcp_tool_calls_total", tool="_probe", status="2xx") == before_calls + 1
    assert _sample("mcp_tool_duration_seconds_count", tool="_probe") == before_time + 1


@pytest.mark.asyncio
async def test_a_raising_tool_is_recorded_as_a_failure_and_still_raises() -> None:
    async def _broken() -> str:
        """A tool that fails."""
        raise RuntimeError("tool exploded")

    server = FastMCP(name="broken")
    install_tool_metrics(server)
    server.tool()(_broken)

    before = _sample("mcp_tool_calls_total", tool="_broken", status="5xx")
    with pytest.raises(Exception, match="tool exploded"):
        await server.call_tool("_broken", {})

    # Recorded as a failure, and the exception is not swallowed — instrumentation
    # that turned a broken tool into a silent one would be worse than none.
    assert _sample("mcp_tool_calls_total", tool="_broken", status="5xx") == before + 1


def test_every_tool_on_the_real_server_is_instrumented() -> None:
    """The reason this lives at the registration seam rather than in handlers.

    Asserted over the whole surface: a tool added later is instrumented because
    it cannot be registered any other way, not because someone remembered.
    """
    server = create_registry_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        annotation_service=MagicMock(),
        workspace_service=MagicMock(),
    )
    # The tool manager's own objects, not the protocol-level ones list_tools()
    # returns: only these carry the underlying function to inspect.
    tools = {t.name: t for t in server._tool_manager.list_tools()}  # noqa: SLF001
    # 26 with the services this test passes. `list_notifications` registers only
    # when a notification service is supplied, so the surface is 27 in the app.
    # A floor rather than an equality: this test is about instrumentation
    # coverage, and the tool catalog is pinned by its own conformance test.
    assert len(tools) >= 26, f"expected the full tool surface, found {len(tools)}"

    uninstrumented = [name for name, tool in tools.items() if not _is_instrumented(tool.fn)]
    assert not uninstrumented, f"tools registered without instrumentation: {uninstrumented}"


def _is_instrumented(fn: object) -> bool:
    # The wrapper is the only thing that sets __wrapped__ on these handlers.
    return hasattr(fn, "__wrapped__")


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0
