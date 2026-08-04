"""Carrying identity from where it is resolved to where the outcome is known.

The requirement says to record usage at the two places actor identity exists.
Taken literally that does not work, and the reason is worth stating rather than
working around silently: both of those places run *before* the route handler, so
neither can see the status class, the latency, or how many rows came back. A row
written there would have its identity fields populated and every analytically
interesting field empty.

The two facts live at different points in the request:

    identity   resolved by the tenant-context dependency (REST) or the
               tenant-resolution helper (MCP), before the handler runs
    outcome    known by the metrics middleware (REST) or the tool wrapper (MCP),
               after the handler returns

So identity is stashed here on the way in, and the event is emitted from the
instrumentation that already computes route template, status class and duration.
The invariant the requirement actually protects — one write path, so the
closed-vocabulary and no-content rules cannot be bypassed — is preserved: one
identity source per surface, one emit point per surface, and a conformance gate
asserting nothing else inserts into the table.

**Only tenant and actor are carried, never the token.** The tenant middleware
already keeps the *enriched* claims off request state precisely because they hold
the raw bearer token, and its comment says why: it would leave a usable credential
lying around for anything downstream to pick up. That reasoning applies here with
more force, because this value exists specifically to be read by later middleware.
"""

from __future__ import annotations

import dataclasses
import uuid
from contextvars import ContextVar
from typing import Any

__all__ = [
    "UsageIdentity",
    "clear_mcp_identity",
    "read_mcp_identity",
    "read_request_identity",
    "set_mcp_identity",
    "stash_request_identity",
]

#: Attribute name on `request.state`. Matches the `oidc_claims` precedent in the
#: tenant middleware — a named attribute rather than a dict key, so a typo is an
#: AttributeError rather than a silent miss.
_REQUEST_ATTR = "usage_identity"


@dataclasses.dataclass(frozen=True, slots=True)
class UsageIdentity:
    """Who the caller is, and nothing else about them.

    Deliberately not the `TenantContext`. That object carries roles, grants and
    the resolved identity string, and stashing it whole would put more on request
    state than this needs — where the next reader would reasonably assume it is
    the authoritative context and start making decisions from it.
    """

    tenant_id: uuid.UUID
    #: `None` for a call that never authenticated. Recorded rather than skipped:
    #: "how many callers could not authenticate" is a real question, and dropping
    #: those rows would change the denominator of every rate.
    actor_id: uuid.UUID | None


# ---------------------------------------------------------------------------
# REST — request.state, following the tenant middleware's own precedent
# ---------------------------------------------------------------------------


def stash_request_identity(request: Any, identity: UsageIdentity) -> None:
    """Attach identity for the metrics middleware to find on the way out.

    Never raises. This runs inside the auth pipeline, and a failure to stash a
    measurement must not be able to fail authentication.
    """
    try:
        setattr(request.state, _REQUEST_ATTR, identity)
    except Exception:  # pragma: no cover - request.state is always settable
        pass


def read_request_identity(scope: dict[str, Any]) -> UsageIdentity | None:
    """Read what the dependency stashed, from an ASGI scope.

    Takes the scope rather than a `Request` because the caller is pure-ASGI
    middleware. Starlette keeps request state in `scope["state"]`, so this reads
    the same place `request.state` writes without constructing a `Request`.

    `None` means no identity was resolved — an unauthenticated request, or one on
    a route that does not depend on tenant context. Both are recordable.
    """
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    value = state.get(_REQUEST_ATTR)
    return value if isinstance(value, UsageIdentity) else None


# ---------------------------------------------------------------------------
# MCP — a ContextVar, beside the three the transport already keeps
# ---------------------------------------------------------------------------
#
# The MCP transport does not run inside FastAPI's dependency machinery, so there
# is no request object to hang this on. It already threads its token, app and
# tenant selector through ContextVars for the same reason; this is a fourth.

_mcp_identity: ContextVar[UsageIdentity | None] = ContextVar("_usage_mcp_identity", default=None)


def set_mcp_identity(identity: UsageIdentity) -> object:
    """Set identity for the current MCP call. Returns the reset token."""
    return _mcp_identity.set(identity)


def read_mcp_identity() -> UsageIdentity | None:
    return _mcp_identity.get()


def clear_mcp_identity(token: object) -> None:
    """Restore the previous value.

    Reset rather than set-to-None: ContextVars in an async server are per-task and
    tasks are reused, so leaving a value bound means the next call handled by that
    task inherits it — and attributes one tenant's usage to another. The same trap
    the request-id middleware guards against.
    """
    try:
        _mcp_identity.reset(token)  # type: ignore[arg-type]
    except (ValueError, TypeError):  # pragma: no cover - token from another context
        _mcp_identity.set(None)
