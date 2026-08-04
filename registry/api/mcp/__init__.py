"""MCP server for registry.

Exposes the registry's catalog, retrieval, workspace, session-memory, claim,
and ARC operations as MCP tools over the Anthropic MCP SDK (FastMCP), mounted
as a Starlette ASGI sub-application under ``/mcp``. See
``registry.api.mcp.server`` for ``create_registry_mcp_server`` /
``create_mcp_app`` and ``registry.api.mcp.tools`` for the tool
implementations, grouped one module per domain.
"""

from __future__ import annotations
