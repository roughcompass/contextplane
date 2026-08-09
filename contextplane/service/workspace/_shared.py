"""Shared plumbing used by more than one workspace concern.

Splitting workspace.py into core / entries / search / purge modules would
otherwise force a choice between putting shared pieces in whichever concern
module happens to need them first (so every other concern imports across a
boundary it doesn't otherwise cross) or writing them twice and letting the
copies drift. This module is the third option: a leaf with no imports back
into the package, so every concern module can depend on it without a cycle.

``AuditWriter`` is the single-method protocol every concern's audit calls are
typed against — matching the shape used across the rest of the service layer.

``VALID_OWNER_KINDS`` and ``VALID_ENTRY_KINDS`` are the closed vocabularies
that back the CHECK constraints on ``workspaces.owner_kind`` and
``workspace_entries.kind``; core.py and entries.py each validate against one.

``_effective_roles`` reads the actor's role set off the already
entitlement-resolved ``TenantContext`` — core.py's CRUD methods, entries.py's
write gate, and search.py's cross-workspace visibility predicate all call it
rather than each re-deriving the same frozenset.

``_WorkspaceState`` declares the instance attributes every concern's methods
read off ``self`` — the session factory, the (currently unused pending the
encryption retrofit) VisibilityService reference, the audit writer, and the
clock. Declaring them once here is what lets each concern module define its
slice of ``WorkspaceService``'s methods without re-declaring the same four
attributes for mypy's benefit; the class is never instantiated on its own —
``WorkspaceService.__init__`` (in ``workspace/__init__.py``) is the only place
that assigns them.

``_decode_id_cursor``/``_encode_id_cursor`` wrap ``pagination.py``'s
``decode_cursor``/``encode_cursor`` for the one payload shape every list
endpoint here uses: ``{"id": "<uuid>"}``. Workspace listing, entry listing,
and cross-workspace search all keyset-paginate on a single UUID column, so
all three used to carry their own copy of "base64-decode, pull out `id`,
parse the UUID" — three copies of the same six lines, differing only in the
local variable name. One wrapper here is what keeps a change to that shape
(or to what counts as a malformed cursor) from needing three simultaneous
edits.

Nothing here is meant to be imported from outside this package. A caller
reaching into ``_shared`` directly instead of through ``WorkspaceService`` has
stepped around the facade the split exists to keep thin.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.pagination import InvalidCursorError, decode_cursor, encode_cursor
from contextplane.types import Clock, TenantContext

if TYPE_CHECKING:
    from contextplane.service.governance.visibility import VisibilityService

# Closed vocabulary — matches CHECK constraint on workspaces.owner_kind.
VALID_OWNER_KINDS: frozenset[str] = frozenset({"actor", "tenant"})

# Closed vocabulary — matches CHECK constraint on workspace_entries.kind.
VALID_ENTRY_KINDS: frozenset[str] = frozenset({"note", "decision", "open_question", "saved_query", "saved_view"})

# Maximum page size for list_workspaces; callers above the cap are silently clamped.
_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 50


class AuditWriter(Protocol):
    """Single-method protocol satisfied by any callable with the audit.emit signature."""

    async def emit(
        self,
        ctx: TenantContext,
        *,
        action: str,
        target_type: str,
        target_id: uuid.UUID,
        after: dict[str, Any] | None = None,
    ) -> None: ...


def _effective_roles(ctx: TenantContext) -> frozenset[str]:
    """Return the set of role names the actor holds in the current tenant.

    Reads from the entitlement-resolved ``TenantContext`` that the
    middleware built; no DB round-trip. Revocation takes effect on the
    next JWT (the entitlement service is consulted per token); within
    a request, the role set is fixed.
    """
    return frozenset(ctx.roles)


class _WorkspaceState:
    """Instance attributes every workspace concern's methods read off ``self``.

    Not instantiated on its own. ``WorkspaceService.__init__`` (in
    ``workspace/__init__.py``) is the only place that assigns these; every
    concern mixin (``core.py``, ``entries.py``, ``search.py``, ``purge.py``)
    inherits from this class so its methods type-check without redeclaring
    the same four attributes.
    """

    _session_factory: async_sessionmaker[AsyncSession]
    # Stored for forward-compat with the encryption retrofit; no concern
    # module calls it yet. All workspace content is plaintext at rest today.
    _visibility_svc: VisibilityService
    _audit_writer: AuditWriter
    _clock: Clock


def _decode_id_cursor(cursor: str | None) -> uuid.UUID | None:
    """Decode a keyset cursor of shape ``{"id": "<uuid>"}`` into the UUID it encodes.

    Delegates to ``decode_cursor(strict=True)`` so any malformed or
    tampered token raises ``InvalidCursorError`` — the same failure mode for
    every list endpoint in this package. ``decode_cursor`` only guarantees
    valid base64+JSON; it does not know this package's payload shape, so a
    payload missing ``id`` or carrying a non-UUID value is still this
    function's problem to reject, and it does so the same way: raising
    ``InvalidCursorError`` rather than letting a malformed value reach the
    database as a query parameter that silently matches nothing.
    """
    if cursor is None:
        return None
    payload = decode_cursor(cursor, strict=True)
    try:
        return uuid.UUID(payload["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("invalid cursor") from exc


def _encode_id_cursor(entity_id: uuid.UUID) -> str:
    """Encode *entity_id* as the opaque keyset cursor ``_decode_id_cursor`` reads back."""
    return encode_cursor({"id": str(entity_id)})


__all__ = [
    "VALID_ENTRY_KINDS",
    "VALID_OWNER_KINDS",
    "AuditWriter",
    "InvalidCursorError",
    "_DEFAULT_PAGE_SIZE",
    "_MAX_PAGE_SIZE",
    "_WorkspaceState",
    "_decode_id_cursor",
    "_effective_roles",
    "_encode_id_cursor",
]
