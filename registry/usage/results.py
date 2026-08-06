"""Carrying how many rows a call answered with, from where it is counted to
where the event is written.

Every serving handler counts its own result set at the point it builds a
response, and that is also the only point that knows what "the result set"
even means for that operation - a search's matches, a list's page, a
traversal's reached nodes. Neither the metrics middleware nor the MCP tool
wrapper that later assemble the `UsageEvent` know any of that; they only know
the route, the status class, and how long the call took.

So the count is stashed here on the way out of the handler, next to the
identity that is stashed on the way in by :mod:`registry.usage.identity`. Same
shape, same reason: the two halves of one usage row are known at different
points in the request, and something has to carry a value between them
without forcing the middleware to re-derive it.

**Unset is not the same as zero.** A handler that stashes `0` answered
"nothing" - a search with no matches, a page with no rows - and that is a
served call the answer-availability metrics need to see, not a gap in the
data. A handler that never calls the stash function here, because the
operation it serves has no result-set semantics (a single-resource GET, a
mutation), leaves the column `NULL`. `NULL` means "not applicable"; folding it
into `0` would make every such endpoint look like a search that always
misses.

**REST and MCP carry the value differently, for the reason identity.py's own
split does.** A REST handler runs inside the same request `Request` the
metrics middleware later inspects, so the value rides `request.state` exactly
like the stashed identity. An MCP tool has no request object - the transport
runs the tool body and the wrapper that emits the usage event on the same
reused asyncio Task, so the value has to ride a `ContextVar` instead.

**The MCP `ContextVar` is reset at the start of every call, not only at the
end.** Tasks are reused, so a tool that never sets a count would otherwise
read back whatever the previous call on that task last set - attributing one
tool's result count to a completely different tool that returns nothing
countable. The wrapper resets to unset before the tool body runs and again on
the way out, the same discipline `clear_mcp_identity` uses and for the same
reason.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from fastapi import Request
from starlette.types import Scope

_log = logging.getLogger(__name__)

__all__ = [
    "clear_mcp_result_count",
    "read_mcp_result_count",
    "read_result_count",
    "set_mcp_result_count",
    "stash_result_count",
]

#: Attribute name on `request.state`. A named attribute, matching the
#: `usage_identity` precedent, so a typo surfaces as an AttributeError rather
#: than a silently-missed dict key.
_REQUEST_ATTR = "usage_result_count"


# ---------------------------------------------------------------------------
# REST - request.state, following identity.py's own precedent
# ---------------------------------------------------------------------------


def stash_result_count(request: Request, n: int) -> None:
    """Attach the result count for the metrics middleware to find on the way out.

    Never raises. A handler calls this after its service returns and before
    it builds the response, and a failure to stash a measurement must not be
    able to fail the request it is measuring.
    """
    try:
        setattr(request.state, _REQUEST_ATTR, n)
    except Exception as exc:  # pragma: no cover  # noqa: BLE001 - request.state is always settable
        _log.debug("stash_result_count: setattr failed: %s", exc)


def read_result_count(scope: Scope) -> int | None:
    """Read what a handler stashed, from an ASGI scope.

    Takes the scope rather than a `Request` for the same reason
    `read_request_identity` does: the caller is pure-ASGI middleware, and
    Starlette's `request.state` is backed by `scope["state"]`, so this reads
    the same place without constructing a `Request`.

    `None` means no handler on this route stashed a count - an operation with
    no result-set semantics, or one that raised before it had a result set to
    count. Both leave the column `NULL`, not `0`.
    """
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    value = state.get(_REQUEST_ATTR)
    return value if isinstance(value, int) else None


# ---------------------------------------------------------------------------
# MCP - a ContextVar, beside the one identity.py already keeps
# ---------------------------------------------------------------------------

_mcp_result_count: ContextVar[int | None] = ContextVar("_usage_mcp_result_count", default=None)


def set_mcp_result_count(n: int | None) -> object:
    """Set the result count for the current MCP call. Returns the reset token.

    The tool wrapper calls this twice: once with `None` before the tool body
    runs, to guarantee a tool that sets nothing cannot inherit a stale value
    from the last call this asyncio Task served, and once with the real count
    from inside a tool that has one.
    """
    return _mcp_result_count.set(n)


def read_mcp_result_count() -> int | None:
    return _mcp_result_count.get()


def clear_mcp_result_count(token: object) -> None:
    """Restore the value that preceded the wrapper's own entry reset.

    Reset rather than set-to-None, for the reason `clear_mcp_identity` resets
    rather than sets to None: a plain set would still leave a binding on this
    Task for whatever runs on it next, and reset is what actually lets go of
    the value this call introduced.
    """
    try:
        _mcp_result_count.reset(token)  # type: ignore[arg-type]
    except (ValueError, TypeError):  # pragma: no cover - token from another context
        _mcp_result_count.set(None)
