"""Workspace CRUD, plus the role-based perceivability chokepoint every other concern calls through.

get_workspace is the workspace-level visibility chokepoint: it uses role-based
access control (via ctx.roles) to determine whether the requesting actor can
perceive the workspace. Bypassing get_workspace is how content leaks happen.
entries.py, search.py, and purge.py each call into this module rather than
re-implementing any part of the perceivability check.

Access model summary:
- Tenant-owned workspaces: any role holder in the owning tenant can perceive.
- Actor-owned workspaces: auditors see all; owners can see their own if they
  hold producer or consumer. No cross-tenant access exists in the role model.

Write gates are enforced by the _assert_can_* helpers immediately after
get_workspace confirms perceivability. Helpers are pure synchronous functions
so they can be unit-tested without a DB session. entries.py's write path
reuses _assert_can_write_entries and its two public aliases directly rather
than re-deriving the same ownership/role table.

No EncryptionService parameter. All workspace content is plaintext at rest —
content encryption is a retrofit layer that does not exist yet.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from registry.audit import actions
from registry.exceptions import RegistryError, ValidationError
from registry.service.workspace._shared import (
    _DEFAULT_PAGE_SIZE,
    VALID_OWNER_KINDS,
    _decode_id_cursor,
    _effective_roles,
    _encode_id_cursor,
    _WorkspaceState,
)
from registry.types import TenantContext

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WorkspaceRef dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceRef:
    """Immutable view of a workspace returned by all service methods.

    encryption_tier is intentionally absent — it exists only to support
    content encryption, a retrofit layer that does not exist yet. Returning
    'none' now creates a forward-compatibility surface that clients begin
    depending on before it carries meaning.

    Fields match WorkspaceResponse shape from the REST contract.
    """

    workspace_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    owner_kind: str
    owner_actor_id: uuid.UUID | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None
    t_invalidated_at: datetime | None


# ---------------------------------------------------------------------------
# Workspace authorization exceptions
# ---------------------------------------------------------------------------


class WorkspaceAuthError(RegistryError):
    """Base for workspace authorization failures.

    Routers map subclasses to HTTP status codes — the router never
    re-evaluates authorization. Raised exclusively by workspace service
    methods, not by the router or middleware.
    """


class WorkspaceNotFound(WorkspaceAuthError):
    """The workspace does not exist or is not perceivable to this actor.

    Router maps to HTTP 404. Raised by get_workspace and by any service
    method that cannot perceive the workspace. Callers must not distinguish
    between these two cases.
    """


class WorkspaceOperationDenied(WorkspaceAuthError):
    """The workspace is perceivable but the requested operation is denied.

    Router maps to HTTP 403. Raised only after get_workspace succeeds
    (perceivability is already confirmed). Never raised for non-perceivable
    workspaces — those always result in WorkspaceNotFound.
    """


# ---------------------------------------------------------------------------
# Role-based authorization helpers
# ---------------------------------------------------------------------------


def _can_perceive_workspace(
    effective_roles: frozenset[str],
    actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
    ws_row: Any,
) -> bool:
    """Return True if the actor can perceive (read) the workspace.

    Pure function — no I/O. Called after _effective_roles(ctx) so the
    role set is already available.

    Decision sequence (all conditions must pass in order):
    1. Workspace must belong to the same tenant as the actor.
    2. Actor must hold at least one role in this tenant.
    3. Workspace must not be soft-deleted (t_invalidated_at is None).
    4. For tenant-owned workspaces: any role holder may perceive.
    5. For actor-owned workspaces:
       - Auditors may perceive any actor workspace (audit carve-out).
       - The owner perceives their own workspace if they hold producer or consumer.
       - All other combinations: not perceivable.

    This function is extracted specifically so it can be exhaustively unit-tested
    without a DB session, one test per authorization matrix cell.
    """
    # Condition 1: tenant boundary
    if ws_row.tenant_id != tenant_id:
        return False

    # Condition 2: actor must hold at least one role
    if not effective_roles:
        return False

    # Condition 3: workspace must not be soft-deleted
    if ws_row.t_invalidated_at is not None:
        return False

    # Condition 4: tenant-owned workspaces are visible to any role holder
    if ws_row.owner_kind == "tenant":
        return True

    # Condition 5: actor-owned workspaces — auditor carve-out and owner check
    if "auditor" in effective_roles:
        return True

    if ws_row.owner_actor_id == actor_id and effective_roles & {"producer", "consumer"}:
        return True

    return False


# ---------------------------------------------------------------------------
# Write-gate helpers — pure functions, no I/O
# ---------------------------------------------------------------------------
# All private helpers use the ordering (effective_roles, actor_id, ws).
# All public aliases use the ordering (actor, workspace, effective_roles)
# and delegate to the corresponding private helper by extracting actor.actor_id.
# All helpers raise WorkspaceOperationDenied on denial and never raise
# WorkspaceNotFound — perceivability must already be confirmed by get_workspace.


def _assert_can_write_entries(
    effective_roles: frozenset[str],
    actor_id: uuid.UUID,
    ws: WorkspaceRef,
) -> None:
    """Raise WorkspaceOperationDenied if the actor may not create or update entries.

    For actor-owned workspaces the actor must be the owner AND hold producer.
    For tenant-owned workspaces the actor must hold admin.
    Archived workspaces reject all entry writes regardless of role or ownership.
    """
    if ws.owner_kind == "actor":
        if ws.archived_at is not None:
            raise WorkspaceOperationDenied("Workspace is archived; entry writes are not permitted.")
        if ws.owner_actor_id != actor_id or "producer" not in effective_roles:
            raise WorkspaceOperationDenied("Only the owning producer may write entries to actor-owned workspaces.")
    else:
        # tenant-owned workspace
        if ws.archived_at is not None:
            raise WorkspaceOperationDenied("Workspace is archived; entry writes are not permitted.")
        if "admin" not in effective_roles:
            raise WorkspaceOperationDenied("Only admins may write entries to tenant-owned workspaces.")


def _assert_can_update_workspace(
    effective_roles: frozenset[str],
    actor_id: uuid.UUID,
    ws: WorkspaceRef,
) -> None:
    """Raise WorkspaceOperationDenied if the actor may not update workspace metadata.

    For actor-owned workspaces the actor must be the owner AND hold producer.
    For tenant-owned workspaces the actor must hold admin.
    Archived workspaces reject metadata updates regardless of role or ownership.
    """
    if ws.owner_kind == "actor":
        if ws.archived_at is not None:
            raise WorkspaceOperationDenied("Workspace is archived; metadata updates are not permitted.")
        if ws.owner_actor_id != actor_id or "producer" not in effective_roles:
            raise WorkspaceOperationDenied("Only the owning producer may update actor-owned workspace metadata.")
    else:
        # tenant-owned workspace
        if ws.archived_at is not None:
            raise WorkspaceOperationDenied("Workspace is archived; metadata updates are not permitted.")
        if "admin" not in effective_roles:
            raise WorkspaceOperationDenied("Only admins may update tenant-owned workspace metadata.")


def _assert_can_delete_workspace(
    effective_roles: frozenset[str],
    actor_id: uuid.UUID,
    ws: WorkspaceRef,
) -> None:
    """Raise WorkspaceOperationDenied if the actor may not soft-delete the workspace.

    Archive state is irrelevant for soft-delete — admins can delete archived tenant
    workspaces and producers can delete archived actor workspaces.
    """
    if ws.owner_kind == "actor":
        if ws.owner_actor_id != actor_id or "producer" not in effective_roles:
            raise WorkspaceOperationDenied("Only the owning producer may delete actor-owned workspaces.")
    else:
        # tenant-owned workspace
        if "admin" not in effective_roles:
            raise WorkspaceOperationDenied("Only admins may delete tenant-owned workspaces.")


def _assert_can_archive_workspace(
    effective_roles: frozenset[str],
    actor_id: uuid.UUID,
    ws: WorkspaceRef,
) -> None:
    """Raise WorkspaceOperationDenied if the actor may not archive/unarchive the workspace.

    Same ownership/role rules as soft-delete. Auditors, consumers, and non-owning
    producers all receive WorkspaceOperationDenied.
    """
    if ws.owner_kind == "actor":
        if ws.owner_actor_id != actor_id or "producer" not in effective_roles:
            raise WorkspaceOperationDenied("Only the owning producer may archive actor-owned workspaces.")
    else:
        # tenant-owned workspace
        if "admin" not in effective_roles:
            raise WorkspaceOperationDenied("Only admins may archive tenant-owned workspaces.")


# ---------------------------------------------------------------------------
# Public aliases — external call surface (actor, workspace, effective_roles ordering)
# ---------------------------------------------------------------------------


def assert_can_create_entry(
    actor: TenantContext,
    workspace: WorkspaceRef,
    effective_roles: frozenset[str],
) -> None:
    """Raise WorkspaceOperationDenied if actor may not create an entry.

    Perceivability must be confirmed by calling get_workspace first.
    Extracts actor_id from actor.actor_id before delegating to the private helper.
    """
    _assert_can_write_entries(
        effective_roles=effective_roles,
        actor_id=actor.actor_id,
        ws=workspace,
    )


def assert_can_update_entry(
    actor: TenantContext,
    workspace: WorkspaceRef,
    effective_roles: frozenset[str],
) -> None:
    """Raise WorkspaceOperationDenied if actor may not update an entry.

    Entry write capability is identical for create and update. Perceivability
    must be confirmed by calling get_workspace first.
    """
    _assert_can_write_entries(
        effective_roles=effective_roles,
        actor_id=actor.actor_id,
        ws=workspace,
    )


def assert_can_soft_delete_workspace(
    actor: TenantContext,
    workspace: WorkspaceRef,
    effective_roles: frozenset[str],
) -> None:
    """Raise WorkspaceOperationDenied if actor may not soft-delete the workspace.

    Perceivability must be confirmed by calling get_workspace first.
    """
    _assert_can_delete_workspace(
        effective_roles=effective_roles,
        actor_id=actor.actor_id,
        ws=workspace,
    )


# ---------------------------------------------------------------------------
# _CoreMethods — workspace CRUD, combined into WorkspaceService in __init__.py
# ---------------------------------------------------------------------------


class _CoreMethods(_WorkspaceState):
    """``WorkspaceService``'s workspace-level CRUD: create/get/list/update/delete."""

    async def create_workspace(
        self,
        ctx: TenantContext,
        name: str,
        owner_kind: str,
        description: str | None = None,
    ) -> WorkspaceRef:
        """Create a new workspace.

        Steps:
        1. Load the actor's effective roles and enforce the creation gate:
           Producer may create actor-owned workspaces; Admin may create
           tenant-owned workspaces. No-role actors and mismatched role/kind
           combinations raise WorkspaceOperationDenied before any DB write.
        2. Fetch the tenant row to check is_regulated. Regulated tenants cannot
           create workspaces while encryption_tier='none' — content encryption is
           a retrofit layer that does not exist yet, so they must wait for it.
           This is a deliberate constraint, not a bug; it is surfaced as an
           actionable 422 so operators understand the blocker.
        3. Validate owner_kind is in the closed vocabulary ('actor', 'tenant').
        4. INSERT the workspace row with encryption_tier='none'.
        5. Emit audit event.
        6. Return WorkspaceRef.

        owner_kind='actor' sets owner_actor_id=ctx.actor_id (personal workspace).
        owner_kind='tenant' sets owner_actor_id=NULL (team workspace).
        """
        now = self._clock.now()
        workspace_id = uuid.uuid4()

        async with self._session_factory() as session, session.begin():
            # Step 1 — role-based creation gate.
            # Load roles before any validation so a no-role actor is rejected
            # before owner_kind is even evaluated — avoiding information leakage
            # about which owner_kind values are valid for a given role.
            effective_roles = _effective_roles(ctx)
            if owner_kind == "actor":
                if "producer" not in effective_roles:
                    raise WorkspaceOperationDenied("Only producers may create actor-owned workspaces.")
            elif owner_kind == "tenant":
                if "admin" not in effective_roles:
                    raise WorkspaceOperationDenied("Only admins may create tenant-owned workspaces.")
            # Unknown owner_kind values are caught in the next step; no-role
            # actors with an unknown kind will fall through to the vocabulary
            # check and receive a 422 (which is acceptable — 403 would also
            # be correct, but 422 is more actionable).

            # Step 2 — regulated-tenant gate.
            tenant_result = await session.execute(
                text("SELECT is_regulated FROM tenants WHERE tenant_id = :tid"),
                {"tid": ctx.tenant_id},
            )
            tenant_row = tenant_result.first()
            if tenant_row is not None and tenant_row.is_regulated:
                raise ValidationError(
                    "Workspace creation is not permitted for regulated tenants at encryption tier 'none'. "
                    "Configure a higher encryption tier before creating workspaces."
                )

            # Step 3 — validate owner_kind vocabulary.
            if owner_kind not in VALID_OWNER_KINDS:
                raise ValidationError(
                    f"Invalid owner_kind {owner_kind!r}. Must be one of: {sorted(VALID_OWNER_KINDS)}."
                )

            owner_actor_id = ctx.actor_id if owner_kind == "actor" else None

            # Step 4 — INSERT workspace row.
            await session.execute(
                text(
                    """
                    INSERT INTO workspaces (
                        workspace_id, tenant_id, name, description,
                        owner_kind, owner_actor_id, encryption_tier,
                        created_at, updated_at, created_by
                    ) VALUES (
                        :workspace_id, :tenant_id, :name, :description,
                        :owner_kind, :owner_actor_id, 'none',
                        :now, :now, :created_by
                    )
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "tenant_id": ctx.tenant_id,
                    "name": name,
                    "description": description,
                    "owner_kind": owner_kind,
                    "owner_actor_id": owner_actor_id,
                    "now": now,
                    "created_by": ctx.actor_id,
                },
            )

        # Step 5 — emit audit event.
        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_CREATED,
            target_type="workspace",
            target_id=workspace_id,
            after={
                "workspace_id": str(workspace_id),
                "tenant_id": str(ctx.tenant_id),
                "owner_kind": owner_kind,
                "name": name,
            },
        )

        _log.info(
            "workspace.created workspace_id=%s tenant=%s owner_kind=%s",
            workspace_id,
            ctx.tenant_id,
            owner_kind,
        )

        # Step 6 — return WorkspaceRef built from the values written.
        return WorkspaceRef(
            workspace_id=workspace_id,
            tenant_id=ctx.tenant_id,
            name=name,
            description=description,
            owner_kind=owner_kind,
            owner_actor_id=owner_actor_id,
            archived_at=None,
            created_at=now,
            updated_at=now,
            created_by=ctx.actor_id,
            t_invalidated_at=None,
        )

    async def get_workspace(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
    ) -> WorkspaceRef:
        """Return a workspace if the caller is authorized to perceive it.

        This is the workspace-level visibility chokepoint. Every service method
        that touches workspace content must call this first. Bypassing it is how
        cross-actor content leaks happen.

        Authorization uses the role-based decision function _can_perceive_workspace:
        the actor must hold at least one role in the workspace's tenant, the
        workspace must not be soft-deleted, and the owner_kind/ownership rules
        must pass. Auditors may perceive any actor-owned workspace in their tenant.

        Raises WorkspaceNotFound when the workspace row does not exist or when the
        actor cannot perceive it — the caller must not distinguish between these two
        cases. WorkspaceNotFound maps to HTTP 404 in the router.
        """
        async with self._session_factory() as session, session.begin():
            ws_result = await session.execute(
                text(
                    """
                    SELECT
                        workspace_id, tenant_id, name, description,
                        owner_kind, owner_actor_id,
                        archived_at, t_invalidated_at,
                        created_at, updated_at, created_by
                    FROM workspaces
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {"workspace_id": workspace_id},
            )
            ws_row = ws_result.first()
            if ws_row is None:
                raise WorkspaceNotFound(f"Workspace {workspace_id} not found.")

            effective_roles = _effective_roles(ctx)
            if not _can_perceive_workspace(effective_roles, ctx.actor_id, ctx.tenant_id, ws_row):
                raise WorkspaceNotFound(f"Workspace {workspace_id} not found.")

        _log.info(
            "workspace.get workspace_id=%s actor=%s tenant=%s",
            workspace_id,
            ctx.actor_id,
            ctx.tenant_id,
        )

        return WorkspaceRef(
            workspace_id=ws_row.workspace_id,
            tenant_id=ws_row.tenant_id,
            name=ws_row.name,
            description=ws_row.description,
            owner_kind=ws_row.owner_kind,
            owner_actor_id=ws_row.owner_actor_id,
            archived_at=ws_row.archived_at,
            created_at=ws_row.created_at,
            updated_at=ws_row.updated_at,
            created_by=ws_row.created_by,
            t_invalidated_at=ws_row.t_invalidated_at,
        )

    async def list_workspaces(
        self,
        ctx: TenantContext,
        include_archived: bool = False,
        cursor: str | None = None,
    ) -> tuple[list[WorkspaceRef], str | None]:
        """List workspaces visible to the calling actor within their tenant.

        Visibility is role-based: the actor must hold at least one role in their
        tenant, and the workspace must satisfy the perceivability conditions:
          - Tenant-owned workspaces are visible to any role holder.
          - Actor-owned workspaces are visible to the owning actor (with producer
            or consumer role) or to auditors.
        Cross-tenant visibility does not exist under the role model.

        Always excludes soft-deleted rows (t_invalidated_at IS NULL).
        Excludes archived rows when include_archived=False (default).

        The visibility predicate is pushed into SQL via EXISTS subqueries against
        actor_roles so the DB can use its index and no Python-layer post-filter
        is needed for correctness or performance.

        Cursor is keyset on workspace_id, decoded/encoded via
        ``_shared._decode_id_cursor``/``_encode_id_cursor`` (opaque
        base64(json({"id": "<uuid>"}))). A malformed cursor raises
        InvalidCursorError; the router maps that to 422.
        """
        cursor_id = _decode_id_cursor(cursor)

        # The actor's role set is fixed for this request — read from the
        # entitlement-resolved TenantContext, not from a DB join. Short-
        # circuit when the caller has no roles: nothing is visible.
        roles = _effective_roles(ctx)
        if not roles:
            return [], None
        is_auditor = "auditor" in roles
        has_pc = bool(roles & {"producer", "consumer"})

        params: dict[str, Any] = {
            "actor_id": ctx.actor_id,
            "tenant_id": ctx.tenant_id,
            "limit": _DEFAULT_PAGE_SIZE + 1,
            "is_auditor": is_auditor,
            "has_pc": has_pc,
        }

        # Visibility predicate uses Python-computed booleans instead of
        # EXISTS joins against actor_roles (the table is gone). Two
        # branches mirror the previous SQL:
        #   tenant-owned: any role holder can see it
        #   actor-owned: auditor sees all; owner sees their own if producer/consumer
        visibility_predicate = """(
            (w.owner_kind = 'tenant')
            OR
            (w.owner_kind = 'actor' AND (
                :is_auditor
                OR (w.owner_actor_id = :actor_id AND :has_pc)
            ))
        )"""

        where_clauses: list[str] = [
            "w.tenant_id = :tenant_id",
            "w.t_invalidated_at IS NULL",
            visibility_predicate,
        ]

        if not include_archived:
            where_clauses.append("w.archived_at IS NULL")

        if cursor_id is not None:
            where_clauses.append("w.workspace_id > :cursor_id")
            params["cursor_id"] = cursor_id

        where_sql = " AND ".join(where_clauses)

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    f"""
                    SELECT
                        w.workspace_id, w.tenant_id, w.name, w.description,
                        w.owner_kind, w.owner_actor_id,
                        w.archived_at, w.t_invalidated_at,
                        w.created_at, w.updated_at, w.created_by
                    FROM workspaces w
                    WHERE {where_sql}
                    ORDER BY w.workspace_id ASC
                    LIMIT :limit
                    """
                ),
                params,
            )
            rows = result.fetchall()

        has_next = len(rows) > _DEFAULT_PAGE_SIZE
        if has_next:
            rows = rows[:_DEFAULT_PAGE_SIZE]

        refs = [
            WorkspaceRef(
                workspace_id=row.workspace_id,
                tenant_id=row.tenant_id,
                name=row.name,
                description=row.description,
                owner_kind=row.owner_kind,
                owner_actor_id=row.owner_actor_id,
                archived_at=row.archived_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
                created_by=row.created_by,
                t_invalidated_at=row.t_invalidated_at,
            )
            for row in rows
        ]

        next_cursor: str | None = None
        if has_next and rows:
            next_cursor = _encode_id_cursor(rows[-1].workspace_id)

        _log.info(
            "workspace.list tenant=%s actor=%s count=%d has_next=%s",
            ctx.tenant_id,
            ctx.actor_id,
            len(refs),
            has_next,
        )

        return refs, next_cursor

    async def update_workspace(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
        name: str | None = None,
        description: str | None = None,
        archived_at: datetime | None = None,
    ) -> WorkspaceRef:
        """Update name, description, or archived_at for a workspace.

        Call get_workspace first — this is the visibility chokepoint and raises
        403/404 before any mutation runs.

        Authorization beyond get_workspace: the caller must be the owning actor
        OR an admin in the workspace's owning tenant. Any other caller that
        passed the read gate (e.g. a share holder from another tenant) gets 403
        here because write access requires ownership, not just read access.

        Only the fields explicitly supplied are written. None means "no change"
        for name and description; for archived_at None explicitly un-archives.
        Callers that want to un-archive must pass archived_at=None explicitly
        — the method signature does not distinguish "not supplied" from "None"
        at the call site, so callers that want to leave archived_at untouched
        must omit the argument (use the existing WorkspaceRef value themselves).
        """
        # Step 1 — visibility check (raises WorkspaceNotFound on 404/access denial).
        existing = await self.get_workspace(ctx, workspace_id)

        # Step 2 — write-auth gate: load roles and call the write helper.
        # _assert_can_archive_workspace is used (rather than _assert_can_update_workspace)
        # because update_workspace handles both metadata changes and archiving/unarchiving.
        # An already-archived workspace must still be un-archivable by the right role
        # holder, so the gate must be archive-state-independent.
        async with self._session_factory() as session, session.begin():
            effective_roles = _effective_roles(ctx)
        _assert_can_archive_workspace(effective_roles, ctx.actor_id, existing)

        now = self._clock.now()
        effective_name = name if name is not None else existing.name
        effective_description = description if description is not None else existing.description

        # Step 3 — UPDATE workspace row.
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE workspaces
                    SET name = :name,
                        description = :description,
                        archived_at = :archived_at,
                        updated_at = :now
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {
                    "name": effective_name,
                    "description": effective_description,
                    "archived_at": archived_at,
                    "now": now,
                    "workspace_id": workspace_id,
                },
            )

        # Step 4 — emit audit event.
        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_UPDATED,
            target_type="workspace",
            target_id=workspace_id,
            after={
                "workspace_id": str(workspace_id),
                "name": effective_name,
                "description": effective_description,
                "archived_at": archived_at.isoformat() if archived_at is not None else None,
            },
        )

        _log.info(
            "workspace.updated workspace_id=%s actor=%s tenant=%s",
            workspace_id,
            ctx.actor_id,
            ctx.tenant_id,
        )

        return WorkspaceRef(
            workspace_id=existing.workspace_id,
            tenant_id=existing.tenant_id,
            name=effective_name,
            description=effective_description,
            owner_kind=existing.owner_kind,
            owner_actor_id=existing.owner_actor_id,
            archived_at=archived_at,
            created_at=existing.created_at,
            updated_at=now,
            created_by=existing.created_by,
            t_invalidated_at=existing.t_invalidated_at,
        )

    async def delete_workspace(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
    ) -> None:
        """Soft-delete a workspace by setting t_invalidated_at.

        Authorization: owning actor or admin in the workspace's owning tenant.

        Idempotent: if t_invalidated_at is already set the call is a no-op —
        no second audit row is written and no error is raised. This lets callers
        retry without inspecting state first.

        Raises 404 if the workspace row does not exist at all (never created or
        physically deleted). An already-soft-deleted workspace is NOT a 404 —
        it is a no-op success.
        """
        now = self._clock.now()

        # Fetch the row directly (including already-soft-deleted rows) so we can
        # distinguish "doesn't exist" (→ WorkspaceNotFound) from "already deleted"
        # (→ no-op idempotency). The role-based visibility gate in get_workspace
        # filters soft-deleted rows, so we bypass it here with a direct query.
        async with self._session_factory() as session, session.begin():
            ws_result = await session.execute(
                text(
                    """
                    SELECT
                        workspace_id, tenant_id, owner_kind, owner_actor_id,
                        archived_at, t_invalidated_at
                    FROM workspaces
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {"workspace_id": workspace_id},
            )
            ws_row = ws_result.first()

            if ws_row is None:
                raise WorkspaceNotFound(f"Workspace {workspace_id} not found.")

            # Idempotency: already soft-deleted — no-op, no audit.
            if ws_row.t_invalidated_at is not None:
                _log.info(
                    "workspace.delete_noop workspace_id=%s actor=%s (already soft-deleted)",
                    workspace_id,
                    ctx.actor_id,
                )
                return

            # Write-auth gate: load roles and call the write helper.
            # Build a minimal WorkspaceRef so the helper can evaluate owner_kind
            # and owner_actor_id. archived_at is included because the helper
            # signature accepts the full ref shape.
            effective_roles = _effective_roles(ctx)
            _ws_ref = WorkspaceRef(
                workspace_id=ws_row.workspace_id,
                tenant_id=ws_row.tenant_id,
                name="",
                description=None,
                owner_kind=ws_row.owner_kind,
                owner_actor_id=ws_row.owner_actor_id,
                archived_at=ws_row.archived_at,
                created_at=now,
                updated_at=now,
                created_by=None,
                t_invalidated_at=ws_row.t_invalidated_at,
            )
            assert_can_soft_delete_workspace(ctx, _ws_ref, effective_roles)

            await session.execute(
                text(
                    """
                    UPDATE workspaces
                    SET t_invalidated_at = :now
                    WHERE workspace_id = :workspace_id
                      AND t_invalidated_at IS NULL
                    """
                ),
                {"now": now, "workspace_id": workspace_id},
            )

        # Emit audit event outside the transaction (consistent with create_workspace).
        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_DELETED,
            target_type="workspace",
            target_id=workspace_id,
            after={
                "workspace_id": str(workspace_id),
                "t_invalidated_at": now.isoformat(),
            },
        )

        _log.info(
            "workspace.deleted workspace_id=%s actor=%s tenant=%s",
            workspace_id,
            ctx.actor_id,
            ctx.tenant_id,
        )


__all__ = [
    "WorkspaceAuthError",
    "WorkspaceNotFound",
    "WorkspaceOperationDenied",
    "WorkspaceRef",
    "_CoreMethods",
    "_assert_can_archive_workspace",
    "_assert_can_delete_workspace",
    "_assert_can_update_workspace",
    "_assert_can_write_entries",
    "_can_perceive_workspace",
    "assert_can_create_entry",
    "assert_can_soft_delete_workspace",
    "assert_can_update_entry",
]
