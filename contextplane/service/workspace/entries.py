"""Workspace entry CRUD, and the PII-scan dispatch every entry write funnels through.

_read_body(entry) is the normative accessor for entry body content. Every path
that reads an entry's body must call _read_body — never access entry.body_md
directly outside that helper. Content encryption is a retrofit layer that does
not exist yet; when it lands, only _read_body needs the conditional decrypt
branch instead of a codebase-wide sweep. search.py reuses this same accessor
rather than defining its own — cross-workspace search reads entry bodies too.

_scan_field is the three-outcome PII dispatch (block/warn/advisory) every entry
write (create_entry, update_entry) runs on body_md and, when supplied,
references_jsonb, before any row is written. A block resolves to
WorkspacePiiBlocked, carrying the field and matched categories as attributes
so the router can reconstruct the exact response shape API clients and the
MCP tool adapter already parse.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Protocol

from sqlalchemy import text

from contextplane.api.pii_guard import AdmissionRefused, PiiScanOutcome, admit_or_refuse
from contextplane.audit import actions
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.service.workspace._shared import _DEFAULT_PAGE_SIZE, VALID_ENTRY_KINDS, _effective_roles
from contextplane.service.workspace._shared import _decode_id_cursor as _decode_entry_cursor
from contextplane.service.workspace._shared import _encode_id_cursor as _encode_entry_cursor
from contextplane.service.workspace.core import (
    _assert_can_write_entries,
    _CoreMethods,
    assert_can_create_entry,
    assert_can_update_entry,
)
from contextplane.types import TenantContext

_log = logging.getLogger(__name__)


class _HasBodyMd(Protocol):
    """Structural protocol for any object with a body_md attribute.

    Accepted by _read_body so it can handle both ORM WorkspaceEntryRecord
    rows and plain SQLAlchemy Row[Any] results without a hard import dependency
    on the ORM class at runtime.
    """

    body_md: str


def _read_body(entry: _HasBodyMd) -> str:
    """Return the entry body as a string.

    This is the sole normative accessor for workspace entry body content.
    body_md is always NOT NULL plaintext today; this returns it directly.
    Every read of entry body content anywhere in this package must go through
    this helper — never access entry.body_md directly.

    Content encryption is a retrofit layer that does not exist yet. When it
    lands, only this function gains the conditional decrypt branch. Scattered
    direct reads of entry.body_md would each need to be found and updated at
    that point, creating the risk of a missed callsite. Centralising here
    eliminates that risk.
    """
    return entry.body_md


# ---------------------------------------------------------------------------
# WorkspaceEntryRef dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceEntryRef:
    """Immutable view of a workspace entry returned by all service methods.

    body_md is str (not Optional) today — every active entry carries a
    plaintext body. Content encryption is a retrofit layer that does not
    exist yet; when it lands, a migration will ALTER body_md to be nullable
    and this dataclass will update to Optional[str].

    No encryption_status field. That field is deferred until content
    encryption exists, to avoid a vestigial enum that always returns
    'plaintext' — clients would begin depending on it before it carries real
    meaning.

    warnings is populated on a PII 'warn' outcome. The PII scan is currently
    stubbed, so warnings will always be None in practice until full dispatch
    is wired. The field exists here so the return shape is stable and wiring
    full dispatch does not need a contract change.
    """

    entry_id: uuid.UUID
    workspace_id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    body_md: str
    references_jsonb: dict[str, Any] | None
    reference_ids: list[uuid.UUID]
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None
    t_invalidated_at: datetime | None
    warnings: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# PII-block domain error
# ---------------------------------------------------------------------------


class WorkspacePiiBlocked(ValidationError):
    """An entry write refused because the PII scanner's policy resolved to 'block'.

    Carries ``field`` and ``categories`` as attributes rather than only in the
    message, so the router can reconstruct the structured
    ``{"code": "pii_detected", "field": ..., "categories": [...]}`` response
    body that API clients and the MCP tool adapter already parse, instead of
    forcing a re-derivation from ``str(exc)``.
    """

    def __init__(self, field: str, categories: list[str]) -> None:
        cats = ", ".join(categories) if categories else "none"
        super().__init__(f"PII detected in field {field!r} (categories: {cats}); write rejected.")
        self.field = field
        self.categories = categories


# ---------------------------------------------------------------------------
# _EntryMethods — entry CRUD, combined into WorkspaceService in __init__.py
# ---------------------------------------------------------------------------


class _EntryMethods(_CoreMethods):
    """``WorkspaceService``'s entry-level CRUD: create/update/delete/list.

    Inherits ``_CoreMethods`` (not just ``_WorkspaceState``) because every
    entry write and read calls ``get_workspace`` — the perceivability
    chokepoint core.py owns — before touching a row. Declaring that
    dependency through inheritance, rather than assuming ``self`` carries a
    method this mixin never declares, is what lets this module type-check on
    its own.
    """

    async def _scan_field(
        self, ctx: TenantContext, text: str, field_type: str, *, subject: str = "workspace_entry"
    ) -> PiiScanOutcome:
        """Admit one entry field, and report the tenant-policy outcome.

        Delegates to the shared admission path — the same one artifacts,
        session events and claims run — so workspace entries are held to one
        floor instead of a workspace-local copy that could drift from it.
        Content carrying a prohibited class raises before this returns, on any
        deployment rather than only a configured one; the returned outcome
        still carries the tenant's own `warn` signal, which the callers below
        surface. Detection rows are written inside, exactly once per call.

        A refusal is re-raised as `WorkspacePiiBlocked` so the surfaces above
        keep mapping it to the response they already mapped a blocking policy
        to. Letting the admission exception escape would have turned a 4xx that
        callers handle into an unhandled 500.
        """
        try:
            return await admit_or_refuse(self._session_factory, ctx, text, field_type, subject=subject)
        except AdmissionRefused as refused:
            raise WorkspacePiiBlocked(field=field_type, categories=list(refused.decision.classes)) from refused

    async def create_entry(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
        kind: str,
        body_md: str,
        reference_ids: list[uuid.UUID],
        references_jsonb: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> WorkspaceEntryRef:
        """Create a new entry in a workspace.

        Step 0: Defense-in-depth regulated-tenant block. A regulated tenant normally
        cannot obtain a workspace (blocked at create_workspace), but this guard fires
        independently to close any gap introduced by test fixtures, migrations, or a
        future relaxation of the workspace-create path. Same error message as
        create_workspace.

        Step 1: get_workspace access check — raises 403/404 before any write.

        Step 2: Validate kind is in the closed vocabulary. The CHECK constraint on
        workspace_entries.kind is the DB backstop; the service validates first to give
        callers an actionable message.

        Step 3: Validate body_md is non-empty. An empty body is not a valid entry
        in any entry kind.

        Step 4: PII scan on body_md (field_type='workspace_entry.body').
        block → 422 with categories; warn → entry stored, warnings in response;
        advisory → stored silently with no client-visible signal.

        Step 5: PII scan on references_jsonb if provided (field_type=
        'workspace_entry.references'). Same three-outcome dispatch.

        Step 6: INSERT workspace_entries row.

        Step 7: Emit audit event. Return WorkspaceEntryRef with body via _read_body.
        """
        now = self._clock.now()
        entry_id = uuid.uuid4()

        async with self._session_factory() as session, session.begin():
            # Step 0 — regulated-tenant defense-in-depth block.
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

        # Step 1 — visibility check and write-auth gate.
        # get_workspace confirms perceivability; then load roles and enforce
        # the write gate before any mutation proceeds.
        ws = await self.get_workspace(ctx, workspace_id)
        async with self._session_factory() as session, session.begin():
            effective_roles = _effective_roles(ctx)
        assert_can_create_entry(ctx, ws, effective_roles)

        # Step 2 — validate kind.
        if kind not in VALID_ENTRY_KINDS:
            raise ValidationError(f"Invalid entry kind {kind!r}. Must be one of: {sorted(VALID_ENTRY_KINDS)}.")

        # Step 3 — validate body_md is non-empty.
        if not body_md:
            raise ValidationError("body_md must not be empty.")

        # Step 4 — PII scan on body_md. Three-outcome dispatch:
        #   block    → raise WorkspacePiiBlocked; do NOT insert row.
        #   warn     → proceed with INSERT; surface warning in returned ref.
        #   advisory → proceed silently; no client-visible signal.
        # _scan_field writes its own pii_detection_log rows regardless of
        # outcome, so there is nothing left for this method to log directly.
        warnings: list[dict[str, Any]] = []
        pii_body = await self._scan_field(ctx, body_md, "workspace_entry.body")
        if pii_body.action_taken == "block":
            raise WorkspacePiiBlocked(field="workspace_entry.body", categories=list(pii_body.categories))
        if pii_body.action_taken == "warn":
            warnings.append({"field": "body_md", "categories": list(pii_body.categories)})
        # advisory: proceed silently.

        # Step 5 — PII scan on references_jsonb if provided. Same three-outcome dispatch.
        if references_jsonb is not None:
            pii_refs = await self._scan_field(ctx, str(references_jsonb), "workspace_entry.references")
            if pii_refs.action_taken == "block":
                raise WorkspacePiiBlocked(field="workspace_entry.references", categories=list(pii_refs.categories))
            if pii_refs.action_taken == "warn":
                warnings.append({"field": "references_jsonb", "categories": list(pii_refs.categories)})
            # advisory: proceed silently.

        # Step 6 — INSERT workspace_entries row.
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO workspace_entries (
                        entry_id, workspace_id, tenant_id, kind, body_md,
                        references_jsonb, reference_ids,
                        expires_at, created_at, updated_at, created_by
                    ) VALUES (
                        :entry_id, :workspace_id, :tenant_id, :kind, :body_md,
                        :references_jsonb, :reference_ids,
                        :expires_at, :now, :now, :created_by
                    )
                    """
                ),
                {
                    "entry_id": entry_id,
                    "workspace_id": workspace_id,
                    "tenant_id": ctx.tenant_id,
                    "kind": kind,
                    "body_md": body_md,
                    # asyncpg's jsonb codec encodes a pre-serialized string (see
                    # SQLAlchemy's asyncpg dialect); a raw dict bound through
                    # text() has no column type to route it through that
                    # encoder, so it has to be serialized here instead.
                    "references_jsonb": (json.dumps(references_jsonb) if references_jsonb is not None else None),
                    "reference_ids": reference_ids,
                    "expires_at": expires_at,
                    "now": now,
                    "created_by": ctx.actor_id,
                },
            )

        # Step 7 — emit audit event.
        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_ENTRY_CREATED,
            target_type="workspace_entry",
            target_id=entry_id,
            after={
                "entry_id": str(entry_id),
                "workspace_id": str(workspace_id),
                "kind": kind,
                "tenant_id": str(ctx.tenant_id),
            },
        )

        _log.info(
            "workspace_entry.created entry_id=%s workspace_id=%s kind=%s actor=%s",
            entry_id,
            workspace_id,
            kind,
            ctx.actor_id,
        )

        # Build a synthetic record object so _read_body is the sole body accessor.
        _synthetic = SimpleNamespace(body_md=body_md)

        return WorkspaceEntryRef(
            entry_id=entry_id,
            workspace_id=workspace_id,
            tenant_id=ctx.tenant_id,
            kind=kind,
            body_md=_read_body(_synthetic),
            references_jsonb=references_jsonb,
            reference_ids=reference_ids,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
            created_by=ctx.actor_id,
            t_invalidated_at=None,
            warnings=warnings if warnings else None,
        )

    async def update_entry(
        self,
        ctx: TenantContext,
        entry_id: uuid.UUID,
        body_md: str | None = None,
        reference_ids: list[uuid.UUID] | None = None,
        references_jsonb: dict[str, Any] | None = None,
    ) -> WorkspaceEntryRef:
        """Update an existing workspace entry.

        Fetches the current entry row to resolve the owning workspace, then calls
        get_workspace to confirm the caller is authorised to write.

        Only fields that are not None are written; omitted fields retain their
        current values. PII scans run on body_md and references_jsonb when provided
        (block/warn/advisory three-outcome dispatch).

        Audit-logged on every successful update.
        """
        now = self._clock.now()

        # Fetch the entry row (including already-deleted rows so we can distinguish
        # "doesn't exist" from "already deleted").
        async with self._session_factory() as session, session.begin():
            entry_result = await session.execute(
                text(
                    """
                    SELECT
                        entry_id, workspace_id, tenant_id, kind, body_md,
                        references_jsonb, reference_ids,
                        expires_at, t_invalidated_at, created_at, updated_at, created_by
                    FROM workspace_entries
                    WHERE entry_id = :entry_id
                    """
                ),
                {"entry_id": entry_id},
            )
            entry_row = entry_result.first()

        if entry_row is None:
            raise NotFoundError(f"Workspace entry {entry_id} not found.")

        # Access check via the entry's workspace: perceivability first, then write gate.
        ws = await self.get_workspace(ctx, entry_row.workspace_id)
        async with self._session_factory() as session, session.begin():
            effective_roles = _effective_roles(ctx)
        assert_can_update_entry(ctx, ws, effective_roles)

        # PII scan on body_md (when provided). Three-outcome dispatch:
        #   block    → raise WorkspacePiiBlocked; do NOT update row.
        #   warn     → proceed with UPDATE; surface warning in returned ref.
        #   advisory → proceed silently; no client-visible signal.
        # _scan_field writes its own pii_detection_log rows regardless of
        # outcome, so there is nothing left for this method to log directly.
        update_warnings: list[dict[str, Any]] = []
        if body_md is not None:
            pii_body = await self._scan_field(ctx, body_md, "workspace_entry.body")
            if pii_body.action_taken == "block":
                raise WorkspacePiiBlocked(field="workspace_entry.body", categories=list(pii_body.categories))
            if pii_body.action_taken == "warn":
                update_warnings.append({"field": "body_md", "categories": list(pii_body.categories)})
            # advisory: proceed silently.

        # PII scan on references_jsonb (when provided). Same three-outcome dispatch.
        if references_jsonb is not None:
            pii_refs = await self._scan_field(ctx, str(references_jsonb), "workspace_entry.references")
            if pii_refs.action_taken == "block":
                raise WorkspacePiiBlocked(field="workspace_entry.references", categories=list(pii_refs.categories))
            if pii_refs.action_taken == "warn":
                update_warnings.append({"field": "references_jsonb", "categories": list(pii_refs.categories)})
            # advisory: proceed silently.

        # Resolve effective values — None means "leave unchanged".
        # Read existing body through _read_body so the future content-encryption
        # decryption path funnels through one helper instead of a codebase-wide audit.
        effective_body_md = body_md if body_md is not None else _read_body(entry_row)
        effective_reference_ids = reference_ids if reference_ids is not None else entry_row.reference_ids
        effective_references_jsonb = references_jsonb if references_jsonb is not None else entry_row.references_jsonb

        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE workspace_entries
                    SET body_md = :body_md,
                        reference_ids = :reference_ids,
                        references_jsonb = :references_jsonb,
                        updated_at = :now
                    WHERE entry_id = :entry_id
                    """
                ),
                {
                    "body_md": effective_body_md,
                    "reference_ids": effective_reference_ids,
                    # See create_entry's INSERT for why this needs pre-serializing.
                    "references_jsonb": (
                        json.dumps(effective_references_jsonb) if effective_references_jsonb is not None else None
                    ),
                    "now": now,
                    "entry_id": entry_id,
                },
            )

        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_ENTRY_UPDATED,
            target_type="workspace_entry",
            target_id=entry_id,
            after={
                "entry_id": str(entry_id),
                "workspace_id": str(entry_row.workspace_id),
            },
        )

        _log.info(
            "workspace_entry.updated entry_id=%s actor=%s",
            entry_id,
            ctx.actor_id,
        )

        # _read_body is the sole accessor; build a synthetic object so
        # the helper is exercised even when no ORM row is available.
        _synthetic = SimpleNamespace(body_md=effective_body_md)

        return WorkspaceEntryRef(
            entry_id=entry_row.entry_id,
            workspace_id=entry_row.workspace_id,
            tenant_id=entry_row.tenant_id,
            kind=entry_row.kind,
            body_md=_read_body(_synthetic),
            references_jsonb=effective_references_jsonb,
            reference_ids=effective_reference_ids,
            expires_at=entry_row.expires_at,
            created_at=entry_row.created_at,
            updated_at=now,
            created_by=entry_row.created_by,
            t_invalidated_at=entry_row.t_invalidated_at,
            warnings=update_warnings if update_warnings else None,
        )

    async def delete_entry(
        self,
        ctx: TenantContext,
        entry_id: uuid.UUID,
    ) -> None:
        """Soft-delete a workspace entry by setting t_invalidated_at.

        Idempotent: if the entry is already soft-deleted the call is a no-op —
        no second audit row is written and no error is raised. This lets callers
        retry without inspecting state first.

        Raises NotFoundError if the entry row does not exist at all.
        Access is confirmed via the entry's owning workspace (get_workspace).
        """
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            entry_result = await session.execute(
                text(
                    """
                    SELECT entry_id, workspace_id, t_invalidated_at
                    FROM workspace_entries
                    WHERE entry_id = :entry_id
                    """
                ),
                {"entry_id": entry_id},
            )
            entry_row = entry_result.first()

            if entry_row is None:
                raise NotFoundError(f"Workspace entry {entry_id} not found.")

            # Idempotency: already soft-deleted — no-op, no audit.
            if entry_row.t_invalidated_at is not None:
                _log.info(
                    "workspace_entry.delete_noop entry_id=%s actor=%s (already soft-deleted)",
                    entry_id,
                    ctx.actor_id,
                )
                return

        # Access check via the entry's workspace: perceivability first, then write gate.
        ws = await self.get_workspace(ctx, entry_row.workspace_id)
        async with self._session_factory() as session, session.begin():
            effective_roles = _effective_roles(ctx)
        _assert_can_write_entries(effective_roles, ctx.actor_id, ws)

        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE workspace_entries
                    SET t_invalidated_at = :now
                    WHERE entry_id = :entry_id
                      AND t_invalidated_at IS NULL
                    """
                ),
                {"now": now, "entry_id": entry_id},
            )

        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_ENTRY_DELETED,
            target_type="workspace_entry",
            target_id=entry_id,
            after={
                "entry_id": str(entry_id),
                "workspace_id": str(entry_row.workspace_id),
                "t_invalidated_at": now.isoformat(),
            },
        )

        _log.info(
            "workspace_entry.deleted entry_id=%s actor=%s",
            entry_id,
            ctx.actor_id,
        )

    async def list_entries(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
        kind: str | None = None,
        cursor: str | None = None,
    ) -> tuple[list[WorkspaceEntryRef], str | None]:
        """List active entries in a workspace.

        Access is gated by get_workspace — the caller must be the
        workspace owner (the calling actor for ``owner_kind='actor'``;
        any actor in the owning tenant for ``owner_kind='tenant'``).
        Entries past their expires_at are still returned; the expiry
        worker soft-deletes them in a background run. list_entries does
        not filter on expiry.

        Cursor is keyset on entry_id (ascending UUID natural order). Kind filter
        is applied server-side when provided.

        Returns a tuple of (entries, next_cursor).
        """
        # Access check — raises 403/404 if the caller cannot see this workspace.
        await self.get_workspace(ctx, workspace_id)

        cursor_id = _decode_entry_cursor(cursor)

        params: dict[str, Any] = {
            "workspace_id": workspace_id,
            "limit": _DEFAULT_PAGE_SIZE + 1,
        }

        where_clauses: list[str] = [
            "workspace_id = :workspace_id",
            "t_invalidated_at IS NULL",
        ]

        if kind is not None:
            where_clauses.append("kind = :kind")
            params["kind"] = kind

        if cursor_id is not None:
            where_clauses.append("entry_id > :cursor_id")
            params["cursor_id"] = cursor_id

        where_sql = " AND ".join(where_clauses)

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    f"""
                    SELECT
                        entry_id, workspace_id, tenant_id, kind, body_md,
                        references_jsonb, reference_ids,
                        expires_at, t_invalidated_at, created_at, updated_at, created_by
                    FROM workspace_entries
                    WHERE {where_sql}
                    ORDER BY entry_id ASC
                    LIMIT :limit
                    """
                ),
                params,
            )
            rows = result.fetchall()

        has_next = len(rows) > _DEFAULT_PAGE_SIZE
        if has_next:
            rows = rows[:_DEFAULT_PAGE_SIZE]

        # body_md for each row is accessed exclusively via _read_body — never directly.
        refs = [
            WorkspaceEntryRef(
                entry_id=row.entry_id,
                workspace_id=row.workspace_id,
                tenant_id=row.tenant_id,
                kind=row.kind,
                body_md=_read_body(row),
                references_jsonb=row.references_jsonb,
                reference_ids=list(row.reference_ids) if row.reference_ids else [],
                expires_at=row.expires_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
                created_by=row.created_by,
                t_invalidated_at=row.t_invalidated_at,
            )
            for row in rows
        ]

        next_cursor: str | None = None
        if has_next and rows:
            next_cursor = _encode_entry_cursor(rows[-1].entry_id)

        _log.info(
            "workspace_entry.list workspace_id=%s actor=%s kind=%s count=%d has_next=%s",
            workspace_id,
            ctx.actor_id,
            kind,
            len(refs),
            has_next,
        )

        return refs, next_cursor


__all__ = [
    "WorkspaceEntryRef",
    "WorkspacePiiBlocked",
    "_EntryMethods",
    "_HasBodyMd",
    "_read_body",
]
