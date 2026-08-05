"""Request correlation: one id per request, echoed to the client and bound to logs.

Every request gets an id, including the ones that never reach a route handler —
health probes, 401s from the auth layer, 429s from the rate limiter. That is why
this is the outermost middleware rather than a dependency: a dependency only runs
for requests that got far enough to have dependencies, which excludes exactly the
requests an operator is trying to correlate when something is wrong.
"""

from __future__ import annotations

import re
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["REQUEST_ID_HEADER", "RequestIdMiddleware", "sanitize_request_id"]

REQUEST_ID_HEADER = "x-request-id"

# Cap and charset for an inbound id. A client-supplied header lands in a JSON log
# line and in a response header; without this, a value carrying newlines or ANSI
# control characters lets a caller forge log entries or corrupt a terminal
# reading them. Unbounded length is the same problem at a different scale — the
# id is repeated on every line the request emits.
_MAX_LENGTH = 128
_ALLOWED = re.compile(r"\A[A-Za-z0-9._-]+\Z")


def sanitize_request_id(raw: str | None) -> str:
    """The id to use, given whatever the client sent.

    Rejection is silent and total: a value that fails is replaced by a fresh one
    rather than truncated or stripped. Salvaging a hostile value would keep an
    attacker-chosen prefix in the logs, and a request id has no meaning worth
    preserving — its only job is to be unique and to match across surfaces.
    """
    if raw and len(raw) <= _MAX_LENGTH and _ALLOWED.match(raw):
        return raw
    return str(uuid.uuid4())


class RequestIdMiddleware:
    """Assigns a request id, binds it for logging, and echoes it on the response.

    The bind is a context-manager rather than a bare ``bind_contextvars`` so the
    variable is unbound when the request ends. Context variables in an ASGI
    server are per-task, and tasks are reused; leaving the id bound means the
    next request handled by that task logs the previous request's id until it
    happens to bind its own.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        inbound: str | None = None
        for name, value in scope.get("headers", ()):
            if name.lower() == REQUEST_ID_HEADER.encode():
                inbound = value.decode("latin-1")
                break

        request_id = sanitize_request_id(inbound)
        header = (REQUEST_ID_HEADER.encode(), request_id.encode())

        async def send_wrapper(message: Message) -> None:
            if message.get("type") == "http.response.start":
                # Replace rather than append: a downstream handler that set its
                # own id would otherwise produce two headers with different
                # values, and which one a client reads is unspecified.
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != REQUEST_ID_HEADER.encode()]
                headers.append(header)
                message = {**message, "headers": headers}
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            await self._app(scope, receive, send_wrapper)
