"""The workspace subdomain: personal and team scratch space, plus its RTBF purge.

Four concerns live here. ``core`` owns workspace-level CRUD and the
role-based perceivability chokepoint (``get_workspace``) every other concern
calls through before touching content. ``entries`` owns entry-level CRUD and
the PII-scan dispatch every entry write runs before a row lands. ``search``
owns the cross-workspace full-text search — a different visibility scope
than ``get_workspace`` answers, so it re-expresses the same role predicate as
a SQL CTE rather than calling into ``core`` per candidate workspace (see its
module docstring). ``purge`` owns the GDPR/CCPA right-to-be-forgotten hard
delete, kept separate because it is the one place in this package where
physical deletion — not soft-invalidation — is correct.

Unlike this repo's other subdomain packages, ``WorkspaceService`` is
re-exported from here (nothing else is). Every router, worker, and MCP tool
that touches workspaces imports ``WorkspaceService`` by name from
``registry.service.workspace`` — that import path is this package's public
contract. A caller wanting ``WorkspaceRef``, ``WorkspaceNotFound``,
``WorkspaceEntryRef``, ``SearchResult``, or ``PurgeResult`` imports the
concern module that owns it directly (``registry.service.workspace.core``,
``.entries``, ``.search``, ``.purge``) — the same way a catalog caller
imports ``registry.service.catalog.entity`` directly. Nothing below the
facade is re-exported, and reaching for it through here is reaching past the
facade this split exists to keep thin.

``WorkspaceService`` is not a facade that delegates to four separate service
objects the way ``CatalogService`` delegates to ``EntityService`` and
``FactService``. Every concern's methods run inside the same session-per-call
pattern and the same audit writer, and entries.py's write path depends
directly on core.py's perceivability chokepoint — composing four objects
would have meant threading that dependency through a constructor argument
instead of an ordinary method call. So the split here is by method, not by
object: each concern module defines a mixin holding its slice of
``WorkspaceService``'s methods, and this class is their combination.
``WorkspaceService.get_workspace`` (or ``.search_workspaces``, or any other
method) is still the exact function object defined in the concern module that
owns it; nothing wraps it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.service.governance.visibility import VisibilityService
from registry.service.workspace._shared import AuditWriter
from registry.service.workspace.core import _CoreMethods
from registry.service.workspace.entries import _EntryMethods
from registry.service.workspace.purge import _PurgeMethods
from registry.service.workspace.search import _SearchMethods
from registry.types import Clock

__all__ = ["WorkspaceService"]


class WorkspaceService(_EntryMethods, _SearchMethods, _PurgeMethods, _CoreMethods):
    """Service for workspaces and their entries: CRUD, search, and RTBF purge.

    get_workspace is the workspace-level visibility chokepoint — every
    service method that touches workspace content must call it first. Bypassing
    it is how cross-actor content leaks happen.

    No EncryptionService parameter — workspaces are plaintext-only at rest;
    content encryption is a retrofit layer that does not exist yet.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        visibility_svc: VisibilityService,
        audit_writer: AuditWriter,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._visibility_svc = visibility_svc
        self._audit_writer = audit_writer
        self._clock = clock
