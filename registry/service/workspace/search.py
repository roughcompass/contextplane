"""Cross-workspace full-text search over entries.

search_workspaces is the one read path in this package that does not call
get_workspace: get_workspace answers "can this actor see this one workspace,"
while search answers "which entries, across every workspace this actor can
see, match this query" — a different scope that would cost one get_workspace
round-trip per candidate workspace if it delegated. Instead the visibility
predicate that backs get_workspace's role-based rules is re-expressed once as
a SQL CTE (``visible_workspaces``) and applied to the whole entries table in
one query. Any change to who can perceive which workspace has to be made in
both places; core.py's docstring on ``_can_perceive_workspace`` is the source
of truth for what the predicate here must match.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from registry.service.workspace._shared import _DEFAULT_PAGE_SIZE, _effective_roles, _WorkspaceState
from registry.service.workspace._shared import _decode_id_cursor as _decode_entry_cursor
from registry.service.workspace._shared import _encode_id_cursor as _encode_entry_cursor
from registry.service.workspace.entries import WorkspaceEntryRef, _read_body
from registry.types import TenantContext

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SearchResult dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchResult:
    """Result set returned by search_workspaces.

    items contains the matching WorkspaceEntryRef objects for the current page.
    next_cursor is non-None when a subsequent page exists; pass it back as
    cursor to retrieve the next page.
    total_count is populated when the DB can supply it cheaply (e.g. a COUNT
    included in the same query); None otherwise. Callers must not assume it is
    always present.
    """

    items: list[WorkspaceEntryRef]
    next_cursor: str | None
    total_count: int | None


# ---------------------------------------------------------------------------
# _SearchMethods — combined into WorkspaceService in __init__.py
# ---------------------------------------------------------------------------


class _SearchMethods(_WorkspaceState):
    """``WorkspaceService.search_workspaces`` — the whole of this concern.

    Unlike entries.py, this mixin never calls ``get_workspace`` (see the
    module docstring for why the visibility predicate is re-expressed here
    instead of delegating to it), so it only needs the plain session/audit/
    clock attributes every concern declares through ``_WorkspaceState``.
    """

    async def search_workspaces(
        self,
        ctx: TenantContext,
        q: str | None = None,
        kind: str | None = None,
        owner_actor_id: uuid.UUID | None = None,
        reference_ids: list[uuid.UUID] | None = None,
        cursor: str | None = None,
    ) -> SearchResult:
        """Search workspace entries visible to the calling actor.

        Visibility scope (content-leak boundary — enforced unconditionally):
          A row is included when the entry's workspace satisfies at least one of:
            - owner_kind='actor' AND workspace.owner_actor_id = ctx.actor_id, OR
            - owner_kind='tenant' AND workspace.tenant_id = ctx.tenant_id.
          Entries from workspaces the actor cannot access are excluded.
          This scope is NOT equivalent to get_workspace (which operates on a single
          workspace ID) but enforces the same two-path visibility rule across all
          workspaces. Workspaces never cross tenant boundaries.

        FTS (when q is provided): to_tsvector('english', body_md) @@ to_tsquery('english', q)
        against the idx_we_body_fts GIN index. No ILIKE fallback.

        reference_ids filter (when provided): entry must contain ALL listed UUIDs
        in its reference_ids array (GIN containment @>).

        kind filter (when provided): WHERE kind = :kind.

        owner_actor_id filter (when provided): restricts to workspaces owned by the
        specified actor. Valid only when ctx.actor_id == owner_actor_id or ctx carries
        an admin role. Raises PermissionError otherwise — callers cannot enumerate
        another actor's workspace entries without admin privilege.

        q=None and reference_ids=None: returns all visible entries paginated (not an error).

        Cursor is keyset on entry_id (ascending UUID order). Decodes on input;
        next_cursor is encoded on output.

        total_count is not populated (None) — the cross-workspace join makes a cheap
        COUNT expensive; callers must use next_cursor for pagination control.
        """
        # owner_actor_id filter: only allowed when the caller IS that actor or is admin.
        if owner_actor_id is not None:
            is_self = ctx.actor_id == owner_actor_id
            is_admin = "admin" in ctx.roles
            if not (is_self or is_admin):
                raise PermissionError(
                    f"Actor {ctx.actor_id} may not filter by owner_actor_id={owner_actor_id}. "
                    "Only the owning actor or an admin may use this filter."
                )

        cursor_id = _decode_entry_cursor(cursor)

        # Caller's role set is fixed for this request — read from the
        # entitlement-resolved TenantContext, not via DB join. Empty
        # role set → no workspaces are visible → return empty result.
        roles = _effective_roles(ctx)
        if not roles:
            return SearchResult(items=[], next_cursor=None, total_count=None)
        is_auditor = "auditor" in roles
        has_pc = bool(roles & {"producer", "consumer"})

        params: dict[str, Any] = {
            "actor_id": ctx.actor_id,
            "tenant_id": ctx.tenant_id,
            "limit": _DEFAULT_PAGE_SIZE + 1,
            "is_auditor": is_auditor,
            "has_pc": has_pc,
        }

        # Visibility CTE — predicate uses Python-computed booleans
        # instead of EXISTS-joins against actor_roles (the table is gone).
        visibility_cte = """
            visible_workspaces AS (
                SELECT w.workspace_id
                FROM workspaces w
                WHERE w.tenant_id = :tenant_id
                  AND w.t_invalidated_at IS NULL
                  AND (
                      (w.owner_kind = 'tenant')
                      OR
                      (w.owner_kind = 'actor' AND (
                          :is_auditor
                          OR (w.owner_actor_id = :actor_id AND :has_pc)
                      ))
                  )
            )
        """

        where_clauses: list[str] = [
            "e.t_invalidated_at IS NULL",
            "e.workspace_id IN (SELECT workspace_id FROM visible_workspaces)",
        ]

        if q is not None:
            where_clauses.append("to_tsvector('english', e.body_md) @@ to_tsquery('english', :q)")
            params["q"] = q

        if reference_ids is not None:
            where_clauses.append("e.reference_ids @> :reference_ids")
            params["reference_ids"] = reference_ids

        if kind is not None:
            where_clauses.append("e.kind = :kind")
            params["kind"] = kind

        if owner_actor_id is not None:
            where_clauses.append(
                "e.workspace_id IN ("
                "  SELECT workspace_id FROM workspaces"
                "  WHERE owner_kind = 'actor' AND owner_actor_id = :owner_actor_id"
                ")"
            )
            params["owner_actor_id"] = owner_actor_id

        if cursor_id is not None:
            where_clauses.append("e.entry_id > :cursor_id")
            params["cursor_id"] = cursor_id

        where_sql = " AND ".join(where_clauses)

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    f"""
                    WITH {visibility_cte}
                    SELECT
                        e.entry_id, e.workspace_id, e.tenant_id, e.kind, e.body_md,
                        e.references_jsonb, e.reference_ids,
                        e.expires_at, e.t_invalidated_at, e.created_at, e.updated_at, e.created_by
                    FROM workspace_entries e
                    WHERE {where_sql}
                    ORDER BY e.entry_id ASC
                    LIMIT :limit
                    """
                ),
                params,
            )
            rows = result.fetchall()

        has_next = len(rows) > _DEFAULT_PAGE_SIZE
        if has_next:
            rows = rows[:_DEFAULT_PAGE_SIZE]

        # body_md is always accessed via _read_body — never directly.
        items = [
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
            "workspace_entry.search actor=%s tenant=%s q=%r kind=%s ref_ids=%s count=%d has_next=%s",
            ctx.actor_id,
            ctx.tenant_id,
            q,
            kind,
            bool(reference_ids),
            len(items),
            has_next,
        )

        return SearchResult(items=items, next_cursor=next_cursor, total_count=None)


__all__ = ["SearchResult", "_SearchMethods"]
