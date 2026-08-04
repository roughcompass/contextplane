"""HTTP request metrics: resolving what to label, and the middleware that records it.

The two resolution functions are pure and live here rather than inside the
middleware class because the properties that matter most about them — that a
label value can never be a raw path, and that a health probe can never be
counted as API traffic — are provable without an ASGI harness.
"""

from __future__ import annotations

import time
from typing import Any

from starlette.routing import Match

from registry import metrics
from registry.api.middleware.ratelimit import _BYPASS_PATH_PREFIXES
from registry.usage.recording import record_rest_usage

__all__ = ["MetricsMiddleware", "derive_type", "resolve_route"]

_MCP_MOUNT = "/mcp"
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def resolve_route(scope: dict[str, Any], app: Any) -> str:
    """The route template for a request, never its resolved path.

    FastAPI's ``APIRoute.matches`` sets ``scope["route"]`` on any match, and the
    object carries ``path_format`` — the template with its parameters unfilled.
    Because the ASGI scope is one mutable dict threaded through the whole
    middleware chain, an outer middleware reading it *after* the downstream call
    returns sees it populated. That is the fast path and it covers every API
    route.

    Not everything is an ``APIRoute``. FastAPI's own docs endpoints are plain
    ``Route`` objects and the MCP sub-application is a ``Mount``; neither sets
    ``scope["route"]``, so those fall back to re-matching against the route
    table. The fallback is rare and bounded — a handful of static paths plus
    404s.

    A ``Mount`` resolves to ``/mcp/{path}``, not ``/mcp``: ``Mount.__init__``
    appends ``/{path:path}`` when it builds its own ``path_format``. That is
    still one fixed string, so it is safe as a label, but it means every request
    under the mount shares a single template and the request histogram cannot
    distinguish them. Per-tool timing is what answers MCP latency.

    Anything unresolvable becomes a constant. Returning the raw path would mint
    one time series per URL a client invents, which is a shape an attacker can
    drive on purpose.
    """
    route = scope.get("route")
    template = getattr(route, "path_format", None)
    if isinstance(template, str) and template:
        return template

    for candidate in getattr(app, "routes", ()):
        try:
            match, _ = candidate.matches(scope)
        except Exception:  # pragma: no cover - a route that cannot match is not a label
            continue
        if match is Match.FULL:
            template = getattr(candidate, "path_format", None) or getattr(candidate, "path", None)
            if isinstance(template, str) and template:
                return template
            break

    return metrics.UNKNOWN_ROUTE


def derive_type(scope: dict[str, Any]) -> str:
    """Which traffic bucket a request belongs to.

    Bypass prefixes are checked first, and that ordering is the whole point. The
    dashboard aggregates latency across every route sharing a type, with no
    per-route filter — so if a liveness probe polled every few seconds and the
    metrics scrape every fifteen were classified as ``read``, they would inject a
    high-frequency stream of near-zero durations into the same percentile the
    "read latency" panel reports. The panel would be populated, and wrong, which
    is worse than the blank panel it replaced.
    """
    path = str(scope.get("path", ""))
    if any(path.startswith(prefix) for prefix in _BYPASS_PATH_PREFIXES):
        return "other"
    if path == _MCP_MOUNT or path.startswith(f"{_MCP_MOUNT}/"):
        return "mcp"

    method = str(scope.get("method", "")).upper()
    if method in _READ_METHODS:
        return "read"
    if method in _WRITE_METHODS:
        return "write"
    return "other"


def status_class(status: int | None) -> str:
    """A status code reduced to its class.

    Raw codes are effectively unbounded once proxies and clients are in the path,
    and nothing at this layer alerts on the difference between 502 and 504.
    """
    if status is None:
        return "other"
    bucket = status // 100
    if bucket in (2, 3, 4, 5):
        return f"{bucket}xx"
    return "other"


class MetricsMiddleware:
    """Times and counts every HTTP request.

    Installed *outside* the rate limiter, and that placement is load-bearing for
    one specific case. ``RateLimitMiddleware`` has six exits and five of them —
    non-HTTP scope, limiting disabled, bypassed prefix, no bearer token, and the
    allowed case — call downstream, so instrumentation on either side of it sees
    them. The sixth sends a 429 itself and never calls downstream. Nested inside,
    this middleware would therefore record nothing at all during throttling: the
    error-rate panel would flatten at exactly the moment the service began
    shedding load, which is the moment someone is looking at it.

    Being outside also means the histogram includes the rate limiter's own work,
    which is the honest number — it is time the client waited.

    Nothing here may alter a response. A metric that fails is a metric that
    failed; a request that fails because a metric failed is an outage caused by
    the instrumentation.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        streaming = path in metrics.STREAMING_PATHS
        status_holder: dict[str, int] = {}
        # Response bytes, for the usage tier. Counted from the body chunks rather
        # than read off Content-Length, which a streaming or chunked response does
        # not set. Not counted for streaming paths at all: an SSE connection's total
        # is however long it stayed open, not the size of an answer, and summing the
        # two into one column would make the average meaningless.
        bytes_holder = {"n": 0}

        async def send_wrapper(message: dict[str, Any]) -> None:
            message_type = message.get("type")
            if message_type == "http.response.start":
                status_holder["status"] = int(message.get("status", 0))
            elif message_type == "http.response.body" and not streaming:
                body = message.get("body")
                if isinstance(body, bytes | bytearray):
                    bytes_holder["n"] += len(body)
            await send(message)

        if streaming:
            metrics.sse_connection_opened()

        started = time.perf_counter()
        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            if streaming:
                metrics.sse_connection_closed()
            route = resolve_route(scope, scope.get("app"))
            status = status_class(status_holder.get("status"))
            # The usage tier, recorded here for the same reason the operational
            # tier is: this is the only point that knows both the route template
            # and the outcome. Identity was stashed on the way in by the
            # tenant-context dependency. Enqueue-only; never raises.
            record_rest_usage(
                scope,
                operation=route,
                status_class=status,
                seconds=elapsed,
                payload_bytes=None if streaming else bytes_holder["n"],
            )
            try:
                metrics.observe_request(
                    route=route,
                    method=str(scope.get("method", "")).upper(),
                    status=status,
                    request_type=derive_type(scope),
                    # A streaming connection's lifetime is not a request duration.
                    # Counted, never timed.
                    seconds=None if streaming else elapsed,
                )
            except Exception:  # pragma: no cover - instrumentation never breaks a request
                pass
