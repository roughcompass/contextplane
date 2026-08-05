"""Right-to-be-forgotten: physically deleting one actor's workspace content.

purge_actor_personal_data is a hard DELETE, not the soft-invalidate every
other write path in this package uses. It is deliberately its own module
rather than a method tacked onto core.py or entries.py: it touches both
workspaces and workspace_entries, it is the one place in this package where
physical deletion is correct, and its own admin-role gate (rather than
get_workspace's perceivability chokepoint) is the right authorization check
for an operation that must reach data the requesting admin does not
otherwise have read access to.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text

from registry.audit import actions
from registry.service.workspace._shared import _WorkspaceState
from registry.types import TenantContext

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PurgeResult dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PurgeResult:
    """Counts returned by purge_actor_personal_data.

    Both counts are 0 on a repeated (idempotent) invocation once the
    actor's data has already been purged — there is nothing left to delete.
    """

    purged_entries: int
    purged_workspaces: int


# ---------------------------------------------------------------------------
# _PurgeMethods — combined into WorkspaceService in __init__.py
# ---------------------------------------------------------------------------


class _PurgeMethods(_WorkspaceState):
    """``WorkspaceService.purge_actor_personal_data`` — the whole of this concern."""

    async def purge_actor_personal_data(
        self,
        ctx: TenantContext,
        target_actor_id: uuid.UUID,
    ) -> PurgeResult:
        """Physically delete all workspace content authored by target_actor_id.

        This is a hard DELETE (not a soft-delete). The bi-temporal invalidation
        rule is suspended for this operation because it is an explicit, fully
        audit-logged GDPR Article 17 / CCPA right-to-delete action. Physical
        deletion is the only erasure primitive available today — content
        encryption, which would introduce DEKs to crypto-shred instead, does not
        exist yet.

        Authorization: caller must hold the admin role. Raises PermissionError
        otherwise. In production this check is defense-in-depth — the admin
        router endpoint that calls this already requires the admin role before
        the call ever reaches here — but the check stays here too because
        callers other than that one router endpoint (e.g. an erasure-registry
        fan-out) must not be able to reach this method without it.

        Two-step algorithm:

        Step 1 — Delete entries:
          Physical DELETE of workspace_entries WHERE created_by = target_actor_id
          in workspaces where t_invalidated_at IS NULL (active workspaces only).
          Tracks the row count.

        Step 2 — Actor-owned workspace cleanup:
          For each workspace owned by the target actor (owner_kind='actor',
          owner_actor_id=target_actor_id):
            - If residual entries authored by other actors remain after Step 1,
              cascade-delete them (counted as additional purged_entries).
            - DELETE the workspace row. Actor-owned workspaces are single-writer
              by design; they cannot be preserved past their owner. The owner_kind
              field is immutable after creation, so there is no path to convert
              them into tenant-owned artifacts.

        Entries authored by the target actor inside tenant-owned workspaces are
        deleted by Step 1; the tenant workspaces themselves are untouched.

        Audit event: action=rtbf.purged, with counts and requesting admin id.

        Returns PurgeResult{purged_entries, purged_workspaces}.
        Both counts are 0 on a repeated call (idempotent: nothing left to purge).
        """
        if "admin" not in ctx.roles:
            raise PermissionError(
                f"Actor {ctx.actor_id} does not hold the admin role and cannot invoke personal-data purge."
            )

        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            # ------------------------------------------------------------------
            # Step 1: physical DELETE of entries authored by the target actor
            # in active (not soft-deleted) workspaces.
            # ------------------------------------------------------------------
            entries_result = await session.execute(
                text(
                    """
                    DELETE FROM workspace_entries
                    WHERE created_by = :target_actor_id
                      AND workspace_id IN (
                          SELECT workspace_id FROM workspaces
                          WHERE t_invalidated_at IS NULL
                      )
                    """
                ),
                {"target_actor_id": target_actor_id},
            )
            purged_entries: int = entries_result.rowcount or 0  # type: ignore[attr-defined]

            # ------------------------------------------------------------------
            # Step 2: handle workspaces owned by the target actor.
            # ------------------------------------------------------------------
            owned_ws_result = await session.execute(
                text(
                    """
                    SELECT workspace_id
                    FROM workspaces
                    WHERE owner_kind = 'actor'
                      AND owner_actor_id = :target_actor_id
                      AND t_invalidated_at IS NULL
                    """
                ),
                {"target_actor_id": target_actor_id},
            )
            owned_ws_ids: list[uuid.UUID] = [row.workspace_id for row in owned_ws_result.fetchall()]

            purged_workspaces = 0
            for ws_id in owned_ws_ids:
                # Check whether any OTHER actor's entries remain in this workspace.
                other_entries_result = await session.execute(
                    text(
                        """
                        SELECT 1 FROM workspace_entries
                        WHERE workspace_id = :ws_id
                          AND created_by IS DISTINCT FROM :target_actor_id
                        LIMIT 1
                        """
                    ),
                    {"ws_id": ws_id, "target_actor_id": target_actor_id},
                )
                has_other_entries = other_entries_result.first() is not None

                if has_other_entries:
                    # Actor-owned workspaces are single-writer: only the owner
                    # can author entries. Stray entries authored by other actors
                    # are residual data that cannot recur, and the workspace
                    # owner is being erased. Cascade-delete the remaining
                    # entries (alongside the workspace) so the actor-owned
                    # container disappears entirely. Count those cascade
                    # deletions against purged_entries.
                    cascade_result = await session.execute(
                        text("DELETE FROM workspace_entries WHERE workspace_id = :ws_id"),
                        {"ws_id": ws_id},
                    )
                    purged_entries += cascade_result.rowcount or 0  # type: ignore[attr-defined]
                await session.execute(
                    text("DELETE FROM workspaces WHERE workspace_id = :ws_id"),
                    {"ws_id": ws_id},
                )
                purged_workspaces += 1

        # Audit outside the transaction so failure in audit does not roll back the
        # purge (the purge itself is the authoritative action; the audit record is
        # additional evidence). Use target_actor_id as the target_id so the audit
        # row is queryable by the erased actor's identifier.
        await self._audit_writer.emit(
            ctx,
            action=actions.RTBF_PURGE,
            target_type="actor",
            target_id=target_actor_id,
            after={
                "target_actor_id": str(target_actor_id),
                "purged_entries": purged_entries,
                "purged_workspaces": purged_workspaces,
                "requesting_admin": str(ctx.actor_id),
                "ts": now.isoformat(),
            },
        )

        _log.info(
            "rtbf.purged target=%s admin=%s entries=%d workspaces=%d",
            target_actor_id,
            ctx.actor_id,
            purged_entries,
            purged_workspaces,
        )

        return PurgeResult(
            purged_entries=purged_entries,
            purged_workspaces=purged_workspaces,
        )


__all__ = ["PurgeResult", "_PurgeMethods"]
