"""MCP tool implementations, grouped by domain.

Each module here (``catalog``, ``retrieval``, ``workspace``, ``memory``,
``notifications``, ``arc``) holds a set of plain, module-level async
functions — one per MCP tool — plus a ``register(mcp_server, ...)`` that
binds that module's construction-time dependencies and decorates the
functions onto a FastMCP server. See ``contextplane.api.mcp.server`` for the
call site and ``contextplane.api.mcp.context`` for the per-request state and
service accessors every tool shares.
"""

from __future__ import annotations
