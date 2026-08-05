"""Per-request MCP state and the helpers every tool module shares.

The SSE transport does not run inside FastAPI's ``Depends`` machinery and
does not pass a request object into tool handlers, so per-request state
(the bearer token, the FastAPI app reference, the selected tenant, the
connection identity, the validated OIDC claims) is threaded through
``ContextVar``s that ``create_mcp_app``'s ``handle_sse`` populates before
the MCP server runs a tool call. Every tool module in
``registry.api.mcp.tools`` reads that state through this module rather than
importing the ContextVars by name, so a test that patches
``registry.api.mcp.context._resolve_tenant`` (or any other name here) reaches
every tool that calls it, regardless of which tools module the call lives in.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import CatalogError, NotFoundError, TenantIsolationError
from registry.service.governance.temporal import normalize_utc
from registry.service.memory.claim_serving import ClaimServingService, ServedClaim
from registry.service.memory.session_events import MemoryService, SessionEvent
from registry.types import Clock, Embedder, TemporalFilter, TenantContext
from registry.usage.identity import UsageIdentity, set_mcp_identity
from registry.wiring.container import Services

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-request token holder — written by handle_sse before MCP server runs.
# ---------------------------------------------------------------------------

_request_token: ContextVar[str] = ContextVar("_mcp_request_token", default="")

# Per-request app reference — written by handle_sse so tool handlers can
# reach app.state.claim_resolver / app.state.settings / app.state.oidc_cache
# without threading them through every call signature. The MCP transport
# does not pass a request object into tool handlers, so this is the only
# way to get at app-scoped state from a tool body.
_request_app: ContextVar[Any] = ContextVar("_mcp_request_app", default=None)

# Per-request selected tenant identifier — populated by handle_sse from an
# optional ``X-Tenant-ID`` SSE header so multi-tenant callers can pin
# their session to one tenant. Empty string = unset; the resolver returns
# a single tenant context if the caller has exactly one grant.
_request_x_tenant_id: ContextVar[str] = ContextVar("_mcp_request_x_tenant_id", default="")

# The server-assigned identity of one live MCP connection. ARC's preflight
# record is keyed by this and never by anything the caller sends: a
# caller-supplied session string is one the caller can guess, and guessing
# another connection's key would mean adopting its preflight.
_request_connection_id: ContextVar[str] = ContextVar("_mcp_request_connection_id", default="")

# The validated JWT claims, kept so ARC can read the issuer that was already
# checked against the allowlist rather than parsing the token itself.
_request_oidc_claims: ContextVar[dict[str, Any] | None] = ContextVar("_mcp_request_oidc_claims", default=None)


# ---------------------------------------------------------------------------
# Auth helper (mirrors tenant middleware, uses tokens.py directly)
# ---------------------------------------------------------------------------


async def _resolve_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> TenantContext:
    """Resolve the per-request Bearer token to a TenantContext via the
    entitlement-resolved auth pipeline.

    The MCP transport doesn't run inside FastAPI's Depends machinery,
    so this function reads three ContextVars set by ``handle_sse``:
    ``_request_token`` (raw JWT from the SSE Authorization header),
    ``_request_app`` (the FastAPI app — used to reach
    ``app.state.claim_resolver`` and the OIDC cache), and
    ``_request_x_tenant_id`` (optional tenant selector).

    Steps mirror the REST middleware:
    1. Read the bearer token from the ContextVar.
    2. Validate the JWT via ``validate_oidc_token``.
    3. Enrich claims with the raw token + X-Tenant-ID for the resolver.
    4. Call ``claim_resolver.resolve(claims)``.
    5. Apply the X-Tenant-ID selection rules.
    6. Idempotent actor upsert; build TenantContext.

    Raises ``ToolError`` (the MCP-level equivalent of HTTPException) on
    any failure path. The plaintext token is never logged.
    """
    raw_token = _request_token.get()
    if not raw_token:
        raise ToolError("missing bearer token")

    app = _request_app.get()
    if app is None:
        raise ToolError("MCP auth not initialised (no app reference on context)")

    # Deliberately *not* routed through `_services(app)` / the typed
    # container: `wire_auth_context` builds this trio inside `lifespan`,
    # after the container has already been assembled, and several test
    # harnesses (and, in principle, an operator rotating credentials)
    # replace `app.state.claim_resolver` on an already-running app. The
    # container is a frozen snapshot taken once at startup, so reading
    # this trio through it would keep serving whatever resolver existed
    # at that instant — silently ignoring a later, legitimate swap. The
    # REST middleware (`registry.api.middleware.tenant`) reads the same
    # three names the same way for the same reason.
    settings = getattr(app.state, "settings", None)
    if settings is None:
        raise ToolError("MCP auth not initialised (settings missing on app.state)")

    resolver = getattr(app.state, "claim_resolver", None)
    if resolver is None:
        raise ToolError(
            "entitlement-resolver not configured — set ENTITLEMENT_SERVICE_URL "
            "and restart with the entitlement-resolved auth path enabled"
        )

    oidc_cache = getattr(app.state, "oidc_cache", None)

    # Import here to avoid a top-level circular: middleware imports from
    # registry.api.auth.oidc which imports from registry.config which …
    from registry.api.auth.oidc import validate_oidc_token
    from registry.auth.entitlements import client as ent_client
    from registry.auth.entitlements.actor_store import (
        DisabledTenantError,
        upsert_entitlement_actor,
    )
    from registry.exceptions import CatalogError as _CatalogError
    from registry.types import TenantMembership

    # Step 2 — JWT validation.
    try:
        claims, resolved_identity = await validate_oidc_token(raw_token, settings, cache=oidc_cache)
        # Retained for consumers that need the *validated* issuer
        # specifically, mirroring what the REST middleware puts on
        # request.state. Decoding the token a second time would be a second
        # place for the two to disagree about who the caller is.
        _request_oidc_claims.set(dict(claims))
    except _CatalogError as exc:
        raise ToolError("authentication required") from exc

    # Step 3 — enrich claims for the resolver fetcher.
    x_tenant_id = _request_x_tenant_id.get() or None
    enriched_claims = dict(claims)
    enriched_claims["__raw_token"] = raw_token
    if x_tenant_id:
        enriched_claims["__x_tenant_id"] = x_tenant_id

    # Step 4 — delegate to the resolver.
    try:
        resolved = await resolver.resolve(enriched_claims)
    except ent_client.EntitlementAuthError as exc:
        raise ToolError(f"authentication required ({exc.status_code})") from exc
    except ent_client.EntitlementNotFoundError as exc:
        raise ToolError("access denied") from exc
    except ent_client.EntitlementRateLimitError as exc:
        raise ToolError("entitlement service rate-limited") from exc
    except ent_client.EntitlementMalformedError as exc:
        raise ToolError("entitlement service returned malformed response") from exc
    except ent_client.EntitlementServiceError as exc:
        raise ToolError("entitlement service unavailable") from exc

    if not resolved.tenant_grants:
        raise ToolError("access denied")

    # Step 5 — tenant selection. MCP sessions are long-lived and tool
    # handlers don't have per-call headers, so the X-Tenant-ID set on
    # the SSE connect is reused across every tool call. If unset, we
    # auto-select when the caller has exactly one grant; otherwise we
    # error explicitly so the caller knows to set the header.
    if x_tenant_id:
        selected = next(
            (g for g in resolved.tenant_grants if g.tenant_external_id == x_tenant_id),
            None,
        )
        if selected is None:
            raise ToolError("X-Tenant-ID does not match any granted tenant")
    elif len(resolved.tenant_grants) == 1:
        selected = resolved.tenant_grants[0]
    else:
        raise ToolError("multiple tenants granted; set X-Tenant-ID on the SSE connection")

    # Step 6 — actor upsert + TenantContext build.
    display_name = (
        resolved.audit_identity.preferred_username if resolved.audit_identity is not None else resolved_identity
    )
    try:
        async with session_factory() as session, session.begin():
            actor_id = await upsert_entitlement_actor(session, selected.tenant_id, resolved_identity, display_name)
    except DisabledTenantError as exc:
        raise ToolError("access denied") from exc

    tenant_memberships = [
        TenantMembership(
            tenant_id=g.tenant_id,
            tenant_slug=g.tenant_external_id,
            roles=frozenset({g.catalog_role}),
        )
        for g in resolved.tenant_grants
    ]

    ctx = TenantContext(
        tenant_id=selected.tenant_id,
        actor_id=actor_id,
        roles=[selected.catalog_role],
        oidc_subject=resolved_identity,
        tenant_memberships=tenant_memberships,
    )

    # Leave identity where the tool wrapper can find it. This is the only point in
    # an MCP call where the caller is known; the outcome is only known once the
    # tool returns. A ContextVar rather than a request attribute because this
    # transport does not run inside FastAPI's dependency machinery — the same
    # reason the token, app and tenant selector are already threaded that way.
    set_mcp_identity(UsageIdentity(tenant_id=ctx.tenant_id, actor_id=ctx.actor_id))
    return ctx


def _validated_issuer() -> str:
    """The issuer from the claims validation already accepted.

    ARC authorizes deployment-wide operations on an exact
    `{issuer, subject}` pair, so the issuer has to be the validated one --
    not one re-derived here, and not a configured default that would compare
    equal to an allowlist entry the caller never actually presented.
    """
    issuer = (_request_oidc_claims.get() or {}).get("iss")
    if not isinstance(issuer, str) or not issuer:
        raise ToolError("the credential carries no validated issuer")
    return issuer


def _parse_as_of(as_of: str | None) -> TemporalFilter:
    """Parse optional ISO-8601 as_of string into TemporalFilter.

    Raises ToolError on naive (timezone-unaware) datetimes.
    """
    if as_of is None:
        return TemporalFilter(as_of=None)
    try:
        dt = datetime.fromisoformat(as_of)
        return TemporalFilter(as_of=normalize_utc(dt))
    except (ValueError, TypeError) as exc:
        raise ToolError(f"as_of must be a timezone-aware ISO-8601 datetime: {exc}") from exc


def _map_catalog_error(exc: CatalogError) -> ToolError:
    if isinstance(exc, NotFoundError):
        return ToolError(f"not found: {exc}")
    if isinstance(exc, TenantIsolationError):
        return ToolError("not found")
    return ToolError(str(exc))


# ---------------------------------------------------------------------------
# JSON serialisation helpers (dataclasses → plain dicts)
# ---------------------------------------------------------------------------


def _serialize(obj: object) -> object:
    """Recursively convert dataclass fields and UUIDs to JSON-safe types."""
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    if isinstance(obj, dict):
        return {(_serialize(k) if isinstance(k, uuid.UUID) else k): _serialize(v) for k, v in obj.items()}
    # dataclasses.is_dataclass (rather than the equivalent hasattr check) is
    # a type guard: it is what lets the attribute access below type-check
    # against `obj: object` instead of needing an `Any` escape hatch here.
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize(getattr(obj, k)) for k in obj.__dataclass_fields__}
    return obj


def _served_claim(claim: ServedClaim) -> dict[str, Any]:
    """Every field a caller needs to check the claim rather than trust it.

    The citation payload is not optional and not summarised. An agent that
    cannot resolve a claim back to what was said has no way to tell a recorded
    observation from a plausible invention.
    """
    return {
        "claim_id": str(claim.claim_id),
        "subject_entity_id": str(claim.subject_entity_id),
        "predicate": claim.predicate,
        "value": claim.value,
        "claim_category": claim.claim_category,
        "confidence": claim.confidence,
        "authority": claim.authority,
        "valid_from": claim.valid_from.isoformat(),
        "valid_to": claim.valid_to.isoformat() if claim.valid_to else None,
        "as_of": claim.as_of.isoformat(),
        "human_confirmed": claim.human_confirmed,
        "citations": [{"kind": c.kind, "ref": c.ref, "excerpt": c.excerpt} for c in claim.citations],
        "label": claim.label,
        "trust": claim.trust,
        "trust_note": claim.trust_note,
    }


def _memory_event(event: SessionEvent) -> dict[str, Any]:
    return {
        "event_id": str(event.event_id),
        "session_id": event.session_id,
        "seq": event.seq,
        "kind": event.kind,
        "body": event.body,
        "tool_name": event.tool_name,
        "metadata": event.metadata,
        "created_at": event.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# app.state accessors — services that live on the FastAPI app's typed
# container rather than being threaded into create_registry_mcp_server as
# constructor params.
# ---------------------------------------------------------------------------


def _services(app: object) -> Services | None:
    """The typed service container for *app*, or ``None`` before it exists.

    Every accessor below reaches into `app.state` through this one function
    rather than doing it inline. The MCP transport threads `app` through a
    ContextVar rather than FastAPI's `Depends` machinery (see the module
    docstring), so nothing guarantees `app` is the fully wired application
    by the time a tool runs — a test harness may hand this module a
    stripped stand-in, which is why *app* itself is typed as `object` here
    rather than `FastAPI`. Reading without a default here would turn that
    into an `AttributeError` several frames inside a tool body instead of
    the `ToolError` a caller can act on.
    """
    return getattr(getattr(app, "state", None), "services", None)


def _arc_state(name: str) -> object:
    """One named ARC service off the container, resolved dynamically by field name.

    The return type is genuinely a function of *name* (``arc_preflight`` ->
    `PreflightRegistry`, ``arc_challenges`` -> `ChallengeService`,
    ``arc_receipt_reader`` -> `ReceiptReader`, ...) -- callers narrow with
    `typing.cast` at the point where they know which one they asked for,
    the same way `getattr` itself can't statically know either.
    """
    app = _request_app.get()
    service = getattr(_services(app), name, None)
    if service is None:
        raise ToolError("ARC is not configured on this deployment")
    return service


def _memory_service() -> MemoryService:
    app_ref = _request_app.get()
    services = _services(app_ref)
    service = services.memory if services is not None else None
    if service is None:
        raise ToolError("session memory is not configured on this deployment")
    return service


def _claim_serving() -> ClaimServingService:
    app_ref = _request_app.get()
    services = _services(app_ref)
    service = services.claim_serving if services is not None else None
    if service is None:
        raise ToolError("claim retrieval is not configured on this deployment")
    return service


def _embedder() -> Embedder:
    app_ref = _request_app.get()
    services = _services(app_ref)
    embedder = services.embedder if services is not None else None
    if embedder is None:
        raise ToolError("semantic retrieval is not configured on this deployment")
    return embedder


# ---------------------------------------------------------------------------
# Tool-registration helper
# ---------------------------------------------------------------------------


def _bind_tool(fn: Callable[..., Any], **deps: object) -> Callable[..., Any]:
    """Bind construction-time dependencies onto a tool function without
    changing the schema FastMCP builds from it.

    Every ``tools/*.py`` function is a plain module-level coroutine so it can
    be imported and awaited directly in a unit test. But the services it
    needs (``catalog``, ``retrieval``, ``session_factory``, …) are per-server
    construction args to ``create_registry_mcp_server``, not per-request
    state — two different ``create_registry_mcp_server`` calls in the same
    process (as the test suite does routinely, one FastMCP instance per test)
    must not share them. So they can't be closed over by the module-level
    function itself, and they can't ride a ContextVar the way the per-request
    auth state does, because a ContextVar set at construction time would leak
    across servers built back-to-back.

    This is the seam that reconciles the two: it returns a wrapper whose
    ``__signature__`` is the original function's signature with the bound
    parameter names removed, so the tool's argument schema is exactly what it
    was minus the parameters this call supplies. ``__name__`` and ``__doc__``
    are copied across explicitly — a plain ``functools.wraps`` would also copy
    the *original*, untrimmed signature onto the wrapper via ``__wrapped__``,
    putting ``catalog``/``session_factory``/etc. back into the schema as
    required arguments.
    """
    sig = inspect.signature(fn, eval_str=True)
    visible = [p for name, p in sig.parameters.items() if name not in deps]
    bound_sig = sig.replace(parameters=visible)

    async def wrapper(*args: object, **kwargs: object) -> object:
        bound = bound_sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return await fn(*bound.args, **bound.kwargs, **deps)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__signature__ = bound_sig  # type: ignore[attr-defined]
    return wrapper
