"""Cross-cutting HTTP concerns that apply to every route, not just one router.

Four things live here because none of them is a domain endpoint and all of
them wrap the *whole* app rather than one router: the structured error
envelope (every handler's exceptions funnel through it), the middleware
stack (rate limiting, request metrics, request-id correlation), OTel
request instrumentation, and the three operator probe endpoints
(``/healthz``, ``/readyz``, ``/metrics``). Named `http_app` rather than
`http` so it reads unambiguously next to the standard-library `http`
package instead of shadowing it in an importer's namespace.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from registry.api.errors import coerce_to_envelope
from registry.api.middleware.metrics import MetricsMiddleware
from registry.api.middleware.ratelimit import RateLimitMiddleware
from registry.api.middleware.request_id import RequestIdMiddleware
from registry.config import Settings
from registry.exceptions import NotFoundError as _NotFoundError

_log = logging.getLogger(__name__)


def _install_error_envelope(app: FastAPI) -> None:
    """Wrap every error response into the structured envelope.

    Three handlers cover the surface:

    1. ``HTTPException`` (any router) → ``{"errors": [{path, code, message}]}``.
       Routers that ``raise HTTPException(detail="...")`` get auto-wrapped;
       routers that ``raise build_error(...)`` get the path+code they
       provided.
    2. ``RequestValidationError`` (FastAPI's own 422 for malformed bodies /
       query params / path params) → each Pydantic error becomes one
       ErrorItem, with ``loc`` joined as a JSON-Pointer-ish ``path``.
    3. Generic ``Exception`` → 500 with ``code=internal_error``. We don't
       leak the exception message to the client; we log it server-side.
    """

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(_request: object, exc: StarletteHTTPException) -> JSONResponse:
        envelope = coerce_to_envelope(exc.status_code, exc.detail)
        return JSONResponse(status_code=exc.status_code, content=envelope, headers=exc.headers)

    # Service-layer typed exceptions — map to HTTP status codes here so any
    # service method that raises NotFoundError/PermissionError surfaces as the
    # right status without every router needing its own try/except. Router-level
    # catches (e.g. via map_catalog_error) still take precedence when present.
    @app.exception_handler(_NotFoundError)
    async def _not_found_handler(_request: object, exc: _NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"errors": [{"path": None, "code": "not_found", "message": str(exc)}]},
        )

    @app.exception_handler(PermissionError)
    async def _permission_handler(_request: object, exc: PermissionError) -> JSONResponse:
        # Visibility chokepoint denials surface as 403 with no detail about
        # the owner tenant — the chokepoint's own message contains the
        # required tenant guidance for the caller. The body intentionally
        # echoes the raised text only when configured by callers; tests
        # asserting against cross-tenant probe responses must verify that
        # the owner-tenant UUID and entity name are not leaked here.
        return JSONResponse(
            status_code=403,
            content={"errors": [{"path": None, "code": "forbidden", "message": "forbidden"}]},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(_request: object, exc: RequestValidationError) -> JSONResponse:
        items = []
        for err in exc.errors():
            loc = err.get("loc") or ()
            # Skip the conventional first segment ("body"/"query"/"path") in the
            # JSON Pointer so $.name reads correctly.
            parts = [str(p) for p in loc[1:]] if len(loc) > 1 else [str(p) for p in loc]
            path = "$" + ("." + ".".join(parts) if parts else "")
            items.append(
                {
                    "path": path,
                    "code": str(err.get("type", "validation_error")),
                    "message": str(err.get("msg", "")),
                }
            )
        return JSONResponse(status_code=422, content={"errors": items})

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(_request: object, exc: Exception) -> JSONResponse:
        _log.exception("unhandled exception in request handler", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "errors": [
                    {"path": None, "code": "internal_error", "message": "internal server error"},
                ],
            },
        )


def register_middleware(
    app: FastAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Install the ASGI middleware stack: rate limiting, metrics, request-id.

    Registration order is load-bearing, and it is the reverse of what it
    reads like: `add_middleware` inserts at position 0, so the *last*
    registration is the outermost. The resulting stack, outermost first, is
    request-id -> metrics -> rate-limit.

    Metrics must sit outside the rate limiter because RateLimitMiddleware
    sends a 429 itself without ever calling downstream. Nested inside, the
    request counter would record nothing while the service sheds load, so the
    error-rate panel would go flat at exactly the moment someone is watching
    it. Request-id sits outside both so health probes, 401s, and 429s all
    carry a correlation id.

    Rate limiting is mounted as ASGI middleware (not a FastAPI dependency) so
    it covers every route automatically — including routes added in the
    future — without requiring per-router wiring. The middleware skips public
    paths (/healthz, /readyz, /metrics, /webhooks) and unauthenticated
    requests, both of which have no tenant context to key the bucket on.
    """
    app.add_middleware(
        RateLimitMiddleware,
        settings=settings,
        session_factory=session_factory,
    )
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestIdMiddleware)


def instrument(app: FastAPI) -> None:
    """Wrap the app with OTel's FastAPI auto-instrumentation."""
    FastAPIInstrumentor.instrument_app(app)


def register_probes(
    app: FastAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Register the three operator probe endpoints: healthz, readyz, metrics."""

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - any DB failure means "not ready"; the response itself is the signal
            _log.warning("readyz: db check failed", exc_info=True)
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content="db unreachable")
        return Response(status_code=status.HTTP_200_OK, content="ok")

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        """Prometheus exposition. Requires a bearer credential."""
        # Deliberately a one-line docstring: this one becomes the endpoint's
        # public OpenAPI description, and the reasoning below is for whoever
        # maintains the handler, not for whoever reads the API spec.
        #
        # The endpoint is not innocuous. It publishes process-global counters —
        # the full route table, entitlement-failure counts, rate-limit
        # rejections, and which MCP tools exist with how often each is called.
        # That is a map of the service's surface and of how often its
        # authorization checks fail.
        #
        # An unset credential returns 503 rather than serving openly. Failing
        # closed makes a misconfigured deployment visible — the scraper reports
        # the target down and someone fixes it — whereas failing open produces
        # a deployment that looks fine and quietly publishes to whoever asks.
        expected = settings.metrics_bearer_token
        if not expected:
            return Response(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content="metrics endpoint is not configured: METRICS_BEARER_TOKEN is unset",
                media_type="text/plain",
            )

        supplied = ""
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() == "bearer":
            supplied = token.strip()

        # Constant-time, and compared even when nothing was supplied: returning
        # early on an absent header would make "no credential" measurably faster
        # than "wrong credential".
        if not secrets.compare_digest(supplied, expected):
            # No payload on the failure path. A body carrying even part of the
            # exposition would hand an unauthenticated caller the thing the
            # credential exists to withhold.
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content="",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
