"""Reading receipts back, and explaining what a resolution did.

Separate from `ReceiptService` on purpose. That module writes inside a
caller's transaction and never opens a session; this one is a read surface
with its own sessions and its own authorization. Merging them would give one
class two lifecycles and make it easy to call a read helper from inside the
resolution transaction, where it would see a snapshot rather than current
state.

Two rules govern everything here.

**Not-found and not-yours look identical.** A receipt in another tenant
raises `NotFoundError`, not a permission error. Distinguishing them confirms
the receipt exists, and existence is itself something the caller is not
entitled to learn.

**An explanation is read from the record, never recomputed.** Re-running
selection to explain a past resolution could disagree with what actually
happened -- the artifacts may have changed, the engine may have changed --
and an explanation that contradicts its own receipt is worse than none.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.types import ArcRequestContext, DetailAudience
from registry.exceptions import NotFoundError


class ReceiptReader:
    """Authorized reads over receipts and their event chains."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, authorization: ArcAuthorizationService
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization

    async def get_receipt(self, ctx: ArcRequestContext, receipt_id: uuid.UUID) -> dict[str, object]:
        """One receipt, with its selected rows redacted by audience."""
        async with self._session_factory() as session:
            row = await self._load(session, ctx, receipt_id)
            selected = await self._selected(session, ctx, receipt_id)
            return {
                "receipt_id": str(row.receipt_id),
                "tenant_id": str(row.tenant_id),
                "actor_id": str(row.actor_id),
                "host_id": row.host_id,
                "session_id": row.session_id,
                "manifest_fingerprint": row.manifest_fingerprint,
                "attestation_id": row.attestation_id,
                "resolution_status": row.resolution_status,
                "selection_engine_version": row.selection_engine_version,
                "registry_build_revision": row.registry_build_revision,
                "canonical_profile_versions": row.canonical_profile_versions,
                "selection_config_digest": row.selection_config_digest,
                "evaluated_at": row.evaluated_at.isoformat(),
                "freshness_basis": row.freshness_basis,
                "blocked_reasons": list(row.blocked_reasons or []),
                "degraded_reasons": list(row.degraded_reasons or []),
                "mandatory_directive_count": row.mandatory_directive_count,
                "rendered_content_bytes": row.rendered_content_bytes,
                "budget_limit_bytes": row.budget_limit_bytes,
                "integrity_state": row.integrity_state,
                "selected": selected,
            }

    async def explain(self, ctx: ArcRequestContext, receipt_id: uuid.UUID) -> dict[str, object]:
        """What applied, what was withheld, and what stopped this resolution.

        Built from the receipt and its event chain alone. The event history
        is included because "what happened afterwards" -- a denied detail
        request, a later grant -- is part of explaining the outcome an
        operator is looking at.
        """
        async with self._session_factory() as session:
            row = await self._load(session, ctx, receipt_id)
            selected = await self._selected(session, ctx, receipt_id)
            events = (
                await session.execute(
                    text(
                        "SELECT sequence, event_type, event_source, event_payload, created_at "
                        "FROM arc_receipt_events WHERE receipt_id = :rid ORDER BY sequence"
                    ),
                    {"rid": receipt_id},
                )
            ).all()

            return {
                "receipt_id": str(row.receipt_id),
                "resolution_status": row.resolution_status,
                "evaluated_at": row.evaluated_at.isoformat(),
                # Both engine version and config digest, because "this
                # resolves differently now" is otherwise unanswerable: an
                # operator cannot tell a changed engine from changed content.
                "selection_engine_version": row.selection_engine_version,
                "selection_config_digest": row.selection_config_digest,
                "blocked_reasons": list(row.blocked_reasons or []),
                "degraded_reasons": list(row.degraded_reasons or []),
                "budget": {
                    "rendered_content_bytes": row.rendered_content_bytes,
                    "budget_limit_bytes": row.budget_limit_bytes,
                },
                "selected": selected,
                "events": [
                    {
                        "sequence": e.sequence,
                        "event_type": e.event_type,
                        "event_source": e.event_source,
                        "payload": e.event_payload,
                        "created_at": e.created_at.isoformat(),
                    }
                    for e in events
                ],
                "integrity_state": row.integrity_state,
            }

    async def _load(self, session: AsyncSession, ctx: ArcRequestContext, receipt_id: uuid.UUID) -> Row[Any]:
        row = (
            await session.execute(
                text(
                    "SELECT receipt_id, tenant_id, actor_id, host_id, session_id, manifest_fingerprint, "
                    "       attestation_id, resolution_status, selection_engine_version, "
                    "       registry_build_revision, canonical_profile_versions, selection_config_digest, "
                    "       evaluated_at, freshness_basis, blocked_reasons, degraded_reasons, "
                    "       mandatory_directive_count, rendered_content_bytes, budget_limit_bytes, "
                    "       integrity_state "
                    "FROM arc_receipts WHERE receipt_id = :rid"
                ),
                {"rid": receipt_id},
            )
        ).one_or_none()
        if row is None:
            msg = f"receipt {receipt_id} not found"
            raise NotFoundError(msg)

        # The authorization check runs after the row is loaded but its
        # failure is reported as not-found, so the two outcomes are
        # indistinguishable from outside.
        if not self._authorization.can_read_receipt(
            ctx, receipt_tenant_id=row.tenant_id, receipt_actor_id=row.actor_id
        ):
            msg = f"receipt {receipt_id} not found"
            raise NotFoundError(msg)
        return row

    async def _selected(
        self, session: AsyncSession, ctx: ArcRequestContext, receipt_id: uuid.UUID
    ) -> list[dict[str, object]]:
        """Selected rows, with source fields redacted per artifact audience.

        The receipt *stores* locators and digests regardless of who can read
        them -- it is the audit record. Redaction happens here, on the way
        out, so an auditor entitled to more sees more from the same row.
        """
        rows = (
            await session.execute(
                text(
                    "SELECT sd.artifact_id, sd.revision_id, sd.directive_id, sd.is_mandatory, "
                    "       sd.was_omitted, sd.omission_reason, sd.source_locator, "
                    "       sd.source_revision_locator, sd.content_digest, r.detail_audience "
                    "FROM arc_receipt_selected_directives sd "
                    "JOIN arc_revisions r ON r.revision_id = sd.revision_id "
                    "WHERE sd.receipt_id = :rid ORDER BY sd.directive_id"
                ),
                {"rid": receipt_id},
            )
        ).all()

        out: list[dict[str, object]] = []
        for r in rows:
            permitted = _audience_permits(ctx, DetailAudience(r.detail_audience))
            out.append(
                {
                    "artifact_id": str(r.artifact_id),
                    "revision_id": str(r.revision_id),
                    "directive_id": str(r.directive_id),
                    "is_mandatory": r.is_mandatory,
                    "was_omitted": r.was_omitted,
                    "omission_reason": r.omission_reason,
                    "source_locator": r.source_locator if permitted else None,
                    "source_revision_locator": r.source_revision_locator if permitted else None,
                    "content_digest": r.content_digest if permitted else None,
                    "audience_redacted": not permitted,
                }
            )
        return out


def _audience_permits(ctx: ArcRequestContext, audience: DetailAudience) -> bool:
    if audience is DetailAudience.ALL_MATCHED_ACTORS:
        return True
    if audience is DetailAudience.TENANT_ADMIN_AUDITOR:
        return "admin" in ctx.roles or "auditor" in ctx.roles
    if audience is DetailAudience.REGISTERED_GATEWAY_ONLY:
        return ctx.is_mcp_session
    return False


__all__ = ["ReceiptReader"]
