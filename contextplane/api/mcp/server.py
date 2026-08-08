"""Builds the FastMCP server and its Starlette ASGI sub-app.

    contextplane_mcp_server = create_registry_mcp_server(...)
    mcp_router = create_mcp_app(server=contextplane_mcp_server)
    app.mount("/mcp", mcp_router)

This is the in-process binding pattern. No sidecar, no stdio transport,
no separate process.

Auth design
-----------
FastMCP tool handlers do not run inside FastAPI's Depends machinery,
so the Bearer token is extracted from the raw ASGI scope at SSE-connect
time and stashed in ContextVars (see ``contextplane.api.mcp.context``). Each
tool call reads those vars and runs ``context._resolve_tenant``, which
mirrors the REST middleware pipeline: JWT validation → entitlement-service
grant resolution → JIT actor upsert → ``TenantContext``. The SSE handshake
itself is unauthenticated (it just opens the event stream); authentication
is per-tool-call.

Transport
---------
Uses SSE (Server-Sent Events) transport, the only HTTP transport
available in mcp<2.0. The Starlette sub-app exposes:
  GET  /mcp/sse        — SSE connection endpoint
  POST /mcp/messages/  — client→server message channel

Tool registration
------------------
Every tool lives as a module-level function in ``contextplane.api.mcp.tools``,
grouped by domain (catalog, retrieval, workspace, memory, memory_curation,
notifications, arc). Each of those modules exposes a ``register(mcp_server, ...)`` that
binds that module's construction-time dependencies and decorates its
functions onto the server. This module just builds the FastMCP instance,
installs the metrics wrapper, and calls each module's ``register`` in turn.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from mcp.types import AnyFunction
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from contextplane.api.mcp import context
from contextplane.api.mcp.tools import arc as arc_tools
from contextplane.api.mcp.tools import catalog as catalog_tools
from contextplane.api.mcp.tools import memory as memory_tools
from contextplane.api.mcp.tools import memory_curation as memory_curation_tools
from contextplane.api.mcp.tools import notifications as notifications_tools
from contextplane.api.mcp.tools import retrieval as retrieval_tools
from contextplane.api.mcp.tools import workspace as workspace_tools
from contextplane.arc.service.preflight import new_connection_id
from contextplane.metrics import observe_mcp_tool
from contextplane.service.catalog.core import CatalogService
from contextplane.service.catalog.includes import IncludeService
from contextplane.service.platform.notifications import NotificationService
from contextplane.service.retrieval import RetrievalService
from contextplane.service.workspace import WorkspaceService
from contextplane.types import Clock, SystemClock
from contextplane.usage.recording import record_mcp_usage
from contextplane.usage.results import clear_mcp_result_count, set_mcp_result_count

_log = logging.getLogger(__name__)


def install_tool_metrics(server: FastMCP) -> None:
    """Instrument every tool the server registers after this call.

    Rebinding the decorator factory instruments the whole tool surface by
    construction. The alternative — a timing block inside each handler — is a
    rule that must be remembered once per tool and again for every tool added
    later, and forgetting it fails silently: the tool works, it simply never
    appears in the metric, so the surface looks smaller than it is. That is the
    exact failure this instrumentation exists to detect, so it must not be
    reproducible by the instrumentation itself.

    Must be called *before* the first tool is defined. Tools registered earlier
    keep the original decorator and are invisible.

    ``functools.wraps`` is what keeps the tool contract intact, and it is not
    incidental. A tool's name, description, and argument schema are all derived
    from the function object: the server reads ``__name__``, ``__doc__``, and
    the signature. ``wraps`` copies the first two along with ``__annotations__``,
    and sets ``__wrapped__`` so ``inspect.signature`` reports the original
    parameters rather than the wrapper's ``(*args, **kwargs)``. Lose any one of
    them and every caller sees a tool of a different shape — including which
    arguments are required.
    """
    original = server.tool

    def instrumented_tool(*args: object, **kwargs: object) -> Callable[[AnyFunction], AnyFunction]:
        # `original` (FastMCP.tool) has its own concrete keyword signature
        # (name, title, description, ...); this wrapper exists precisely to
        # forward whatever a caller passes without knowing that signature,
        # so re-typing args/kwargs to match it here would mean duplicating
        # -- and staying in lockstep with -- the SDK's own signature.
        register = original(*args, **kwargs)  # type: ignore[arg-type]

        def decorator(fn: AnyFunction) -> AnyFunction:
            @functools.wraps(fn)
            async def wrapper(*a: object, **kw: object) -> object:
                started = time.perf_counter()
                status = "2xx"
                # Reset to unset before the tool body runs. The ContextVar lives
                # on a reused asyncio Task, so without this a tool that never
                # reports a count would otherwise read back whatever the
                # previous call on that task last set.
                count_token = set_mcp_result_count(None)
                try:
                    with observe_mcp_tool(fn.__name__):
                        return await fn(*a, **kw)
                except Exception:
                    status = "5xx"
                    raise
                finally:
                    # The usage tier. Identity was set by `_resolve_tenant` during
                    # the call; the outcome is only knowable here. Enqueue-only,
                    # and it swallows its own failures — a tool must not fail
                    # because recording it did.
                    record_mcp_usage(
                        context._request_app.get(),
                        tool=fn.__name__,
                        status_class=status,
                        seconds=time.perf_counter() - started,
                    )
                    clear_mcp_result_count(count_token)

            return register(wrapper)

        return decorator

    # Deliberate override, not an accidental shadow: FastMCP does not expose
    # a supported way to intercept tool registration, so this replaces the
    # bound method on the instance with the instrumented version above.
    server.tool = instrumented_tool  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Factory: build a FastMCP server closed over service instances
# ---------------------------------------------------------------------------


def create_registry_mcp_server(
    retrieval: RetrievalService,
    catalog: CatalogService,
    session_factory: async_sessionmaker[AsyncSession],
    workspace_service: WorkspaceService,
    clock: Clock | None = None,
    notifications: NotificationService | None = None,
    includes: IncludeService | None = None,
) -> FastMCP:
    """Return a FastMCP instance with the registered registry tools.

    Args:
        retrieval: RetrievalService instance (search, list, dependencies,
            reverse traversal, blast-radius).
        catalog: CatalogService instance (single-entity lookup).
        session_factory: SQLAlchemy async session factory for auth DB calls.
        workspace_service: Pre-built WorkspaceService for the six workspace
            MCP tools. Registered unconditionally — missing wiring is a
            startup error, not a silent no-op.
        clock: Clock implementation; defaults to SystemClock.
        notifications: NotificationService for the ``list_notifications`` tool.
            When ``None``, the tool is not registered.
        includes: IncludeService instance for bounded sub-resource expansion
            (``?include=components,depends_on,external_ids,interface``).
            When ``None``, the ``include`` parameter is accepted but silently
            ignored — expansion returns ``None`` for all sub-resources.
    """
    _clock = clock or SystemClock()

    mcp_server = FastMCP(
        name="capability-registry",
        instructions=(
            "This MCP server exposes tools for the Capability Catalog registry. "
            "The registry manages two distinct resource types: catalog entities "
            "(capabilities, interfaces, components) and workspaces. Workspaces "
            "are collaborative notebooks/memory owned by the registry — they store "
            "structured entries such as decisions, notes, and saved queries that "
            "belong to the registry workflow. Workspaces are not VS Code or any IDE "
            "concept; they have no relation to development environments. Use "
            "create_workspace / add_workspace_entry / search_workspace_entries for "
            "registry notebook operations, and search_capabilities / get_capability "
            "for catalog lookups."
        ),
    )

    # Instrument every tool defined below. Must precede the first registration.
    install_tool_metrics(mcp_server)

    # whoami + single-entity catalog lookups.
    catalog_tools.register(
        mcp_server,
        catalog=catalog,
        session_factory=session_factory,
        clock=_clock,
        includes=includes,
    )

    # ARC preflight + challenge/receipt tools.
    arc_tools.register(mcp_server, session_factory=session_factory, clock=_clock)

    # Search, traversal, and listing over the catalog.
    retrieval_tools.register(
        mcp_server,
        retrieval=retrieval,
        catalog=catalog,
        session_factory=session_factory,
        clock=_clock,
    )

    # list_notifications — registers only when a notification service is wired.
    notifications_tools.register(
        mcp_server,
        notifications=notifications,
        session_factory=session_factory,
        clock=_clock,
    )

    # Workspace CRUD + search.
    workspace_tools.register(
        mcp_server,
        workspace_service=workspace_service,
        session_factory=session_factory,
        clock=_clock,
    )

    # Claim retrieval + session memory.
    memory_tools.register(mcp_server, session_factory=session_factory, clock=_clock)

    # Curation queue, promotion review, confirmation, history, capability
    # requests, and direct claim assertion -- the agent-facing twin of
    # api/routers/memory_curation.py. Every service this module needs comes
    # off the app's typed container at call time (see the module docstring),
    # so nothing beyond session_factory/clock is bound here.
    memory_curation_tools.register(mcp_server, session_factory=session_factory, clock=_clock)

    return mcp_server


# ---------------------------------------------------------------------------
# ASGI sub-app factory
# ---------------------------------------------------------------------------


def _extract_bearer(scope: dict[str, Any]) -> str:
    """Pull the Bearer token from the ASGI scope headers (bytes pairs)."""
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for name, value in headers:
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer":
                return token.strip()
    return ""


def create_mcp_app(server: FastMCP, parent_app: FastAPI | None = None) -> ASGIApp:
    """Build a Starlette ASGI sub-app from a FastMCP server.

    Mounts the MCP server in-process:

        contextplane_mcp_server = create_registry_mcp_server(...)
        mcp_router = create_mcp_app(server=contextplane_mcp_server, parent_app=app)
        app.mount("/mcp", mcp_router)

    Transport: SSE (mcp<2.0 only exposes SSE HTTP transport; StreamableHTTP
    arrives in mcp>=2.0 — upgrade when the version constraint allows).

    Routes exposed under the ``/mcp`` prefix:
        GET  /mcp/sse        — SSE connection (client initiates session)
        POST /mcp/messages/  — client→server JSON-RPC messages

    Auth: the Bearer token is extracted from the SSE request headers and
    stored in a ContextVar before handing off to the MCP server. Each
    tool call reads the ContextVar and resolves it through the
    entitlement-resolved auth path (see ``contextplane.api.mcp.context._resolve_tenant``).

    ``parent_app`` is the FastAPI app this sub-app will be mounted under.
    A mounted sub-app's ``request.app`` is the sub-app itself — whose
    ``state`` is empty — so the tool handlers can't reach
    ``app.state.settings`` / ``claim_resolver`` / ``oidc_cache`` via
    ``request.app``. Capturing the parent app at construction time and
    storing it in the ``_request_app`` ContextVar is how the tool
    handlers find their dependencies.
    """
    sse_transport = SseServerTransport("/messages/")

    # There is deliberately no third racer here for "the server is shutting
    # down", and it is worth saying why, because it is the obvious fix and it
    # does not work. This stream is what makes shutdown hang: the response
    # never completes, so the connection is never idle, and the server waits
    # for a client that has no reason to leave. But the shutdown notification
    # an application can observe — the lifespan shutdown event — is only sent
    # *after* that wait finishes. Anything wired to it fires once the stream is
    # already being torn down, so it would read as a fix while changing
    # nothing. The bound that actually ends the wait is the server's
    # --timeout-graceful-shutdown, which cancels this task; the handler below
    # already treats cancellation as a clean end.
    async def _poll_disconnect(request: Request) -> None:
        """Return as soon as the client closes the connection.

        Polls ``request.is_disconnected()`` in a tight loop so the caller can
        race this against the MCP server run and cancel it on disconnect.
        Starlette's ``is_disconnected()`` is non-blocking (it peeks at the
        receive channel with an immediately-cancelled CancelScope), so the
        loop itself is O(1) per iteration and yields to the event loop on
        each ``asyncio.sleep(0)`` call.
        """
        while not await request.is_disconnected():
            await asyncio.sleep(0.5)

    async def handle_sse(request: Request) -> None:
        # Extract Bearer token + X-Tenant-ID header from the SSE request
        # scope; store both plus the parent app reference in ContextVars
        # so the tool handlers + _resolve_tenant can read them. Inside a
        # mounted sub-app ``request.app`` is the sub-app whose state is
        # empty — the parent app captured at construction time is what
        # carries ``state.settings`` / ``state.claim_resolver`` /
        # ``state.oidc_cache``. Fall back to request.app for standalone
        # use where a parent isn't supplied.
        raw_token = _extract_bearer(dict(request.scope))
        token_var_token = context._request_token.set(raw_token)
        app_ref = parent_app if parent_app is not None else request.app
        app_var_token = context._request_app.set(app_ref)

        # X-Tenant-ID is optional; an absent header means "auto-select
        # if the caller has exactly one tenant grant, otherwise reject".
        x_tenant_id = ""
        for name, value in request.scope.get("headers", []):
            if name.lower() == b"x-tenant-id":
                x_tenant_id = value.decode("latin-1").strip()
                break
        tenant_var_token = context._request_x_tenant_id.set(x_tenant_id)

        # One identity per connection, minted here because this is where a
        # connection actually begins. Its preflight record is dropped in the
        # `finally` below, so a disconnect invalidates it — a record that
        # outlived its connection would be a preflight for a caller nobody
        # is on the other end of.
        connection_id = new_connection_id()
        connection_var_token = context._request_connection_id.set(connection_id)
        try:
            async with sse_transport.connect_sse(request.scope, request.receive, request._send) as streams:
                # Race the MCP server against a disconnect watchdog.  Without
                # this, a client that drops the SSE connection leaves the server
                # socket in CLOSE_WAIT forever because server._mcp_server.run()
                # never returns — it is blocked waiting for the next JSON-RPC
                # message on the POST channel, which will never arrive.
                mcp_task = asyncio.ensure_future(
                    server._mcp_server.run(
                        streams[0],
                        streams[1],
                        server._mcp_server.create_initialization_options(),
                    )
                )
                disconnect_task = asyncio.ensure_future(_poll_disconnect(request))
                try:
                    done, pending = await asyncio.wait(
                        {mcp_task, disconnect_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        except Exception as discard_exc:  # noqa: BLE001 - task is already being discarded post-cancel; the winning task's own exception (below) is what surfaces to the caller, this is just cleanup
                            _log.debug("mcp sse: discarded task raised during cancellation cleanup: %s", discard_exc)
                    # Re-raise any exception from the MCP run task so errors
                    # are not silently swallowed.
                    for task in done:
                        if task is mcp_task and not task.cancelled():
                            exc = task.exception()
                            if exc is not None:
                                raise exc
                except asyncio.CancelledError:
                    mcp_task.cancel()
                    disconnect_task.cancel()
                    raise
        finally:
            preflight = getattr(context._services(app_ref), "arc_preflight", None)
            if preflight is not None:
                preflight.invalidate(connection_id)
            context._request_token.reset(token_var_token)
            context._request_app.reset(app_var_token)
            context._request_x_tenant_id.reset(tenant_var_token)
            context._request_connection_id.reset(connection_var_token)

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
    )
    return starlette_app


__all__ = [
    "create_registry_mcp_server",
    "create_mcp_app",
    "install_tool_metrics",
]
