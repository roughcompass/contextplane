"""Moving a registered artifact revision through its lifecycle.

The only write path for the *transitions* a governed revision goes through
once it exists: activation, revocation, and (via the review-expiry worker's
constants below) expiry. Everything downstream assumes that a revision
reaching `active` has already cleared these checks -- selection trusts that
an active revision was approved, JIT trusts that its audience was set at
registration, and the receipt trusts that the content digest it recorded
identifies real upstream content.

**Registration is not authoring, and it is not a lifecycle transition
either.** Bringing a revision into existence -- recording an already-approved
upstream revision as a draft, then binding real approval evidence to it --
lives in `artifact_materialisation.py`, because that half of the work shares
a different precondition (the revision does not yet bind anyone) and a
different set of writes (inserts, not state changes) than the transitions
below. This file supplies `activate`, `revoke`, and `invalidate`; the class
that carries both halves as one instance is assembled here from the other
file's mixin, so callers still see one `ArtifactService` regardless of which
file a given method's body lives in. See `artifact_materialisation.py`'s
module docstring for the other half, and `artifact_integrity.py`'s for the
state vocabulary and row-loading primitives both halves share.

**A revision is registered draft and activated separately.** Registration
validates and records; activation is what makes a revision bind agents. They
are separate because activation has to serialize against the artifact family
— exactly one revision of an artifact may be active — and because an
operator registering content should not accidentally put it into force.
"""

from __future__ import annotations

import datetime
import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service import audit_outbox
from contextplane.arc.service.approval import assert_evidence_is_trusted
from contextplane.arc.service.artifact_integrity import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DRAFT,
    LIFECYCLE_EXPIRED,
    LIFECYCLE_REVOKED,
    LIFECYCLE_SUPERSEDED,
    ArtifactLifecycleError,
    EvidenceTypeNotWritableError,
    _assert_evidence_approves,
    _assert_transition,
    _load_artifact,
    _lock_family,
    applicability_digest,
    applicability_snapshot,
)
from contextplane.arc.service.artifact_materialisation import _MaterialisationMixin
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.types import ArcRequestContext
from contextplane.audit import actions
from contextplane.types import Clock

OBLIGATION_SATISFIED = "satisfied"
OBLIGATION_MISSING_REVOKED = "missing_revoked"
OBLIGATION_MISSING_INVALID = "missing_invalid"
OBLIGATION_MISSING_REVIEW_EXPIRED = "missing_review_expired"


class ArtifactService(_MaterialisationMixin):
    """The single write path for ARC governed content.

    Composed from `_MaterialisationMixin` (`artifact_materialisation.py`),
    which supplies `register_revision` and `attach_approval_evidence` -- the
    two operations that bring a revision into existence and ready it for
    activation. `activate`, `revoke`, and `invalidate` below move an
    existing, already-registered revision between lifecycle states. Callers
    hold one `ArtifactService` instance and see one API; which file a given
    method's body lives in is an internal cohesion boundary, not something
    a caller needs to know.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock

    # -- lifecycle ------------------------------------------------------------

    async def activate(
        self, ctx: ArcRequestContext, revision_id: uuid.UUID, *, supersedes: uuid.UUID | None = None
    ) -> None:
        """Put a revision into force, superseding its predecessor.

        Locks the whole artifact family `FOR UPDATE` before reading anything.
        Exactly one revision of an artifact may be active, and two concurrent
        activations that each checked "is anything active?" before either
        wrote would both see no. The database has a partial unique index as a
        backstop, but relying on it alone would turn a routine race into a
        failed request rather than a serialized one.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            revision = await _lock_family(session, revision_id)
            artifact = await _load_artifact(session, revision.artifact_id)
            self._authorization.assert_can_write_artifact(ctx, artifact)

            _assert_transition(revision.lifecycle_state, LIFECYCLE_ACTIVE)

            # Re-checked at activation, not trusted from registration: a
            # revision registered months ago may have passed its review date
            # while sitting in draft, and activating it would put stale
            # governance into force.
            if revision.review_expires_at <= now:
                msg = f"revision {revision_id} passed its review date on {revision.review_expires_at.isoformat()}"
                raise ArtifactLifecycleError(msg)
            # This is the only gate standing between a draft and activation,
            # and it has to be a real fact about the row rather than a
            # deployment setting -- a boolean here used to say "verification
            # is configured" and could be left true with nothing behind it,
            # which made every check below satisfied by exactly the
            # capability they exist to constrain: whoever can write the
            # evidence row can equally set `lifecycle_state = 'active'` and
            # skip them entirely. `attach_approval_evidence` now refuses
            # every `evidence_type` this deployment has no first-party writer
            # for, so this column can never legitimately hold a value through
            # any call this service exposes -- only a direct SQL write could
            # populate it, and that is an actor with database access this
            # module was never able to constrain anyway.
            if revision.approval_evidence_id is None:
                msg = f"revision {revision_id} has no approval evidence and cannot be activated"
                raise ArtifactLifecycleError(msg)
            # Re-checked here, not only where the link was made. `attach` was
            # the sole enforcer of "this evidence approves *this* revision",
            # and `register_revision` can set the column directly -- so a
            # revision could be registered citing an approval granted to
            # something else and then activated, borrowing it. Activation is
            # the step that puts a revision into force, so it is the one that
            # has to hold regardless of how the column was populated.
            await _assert_evidence_approves(session, revision.approval_evidence_id, revision_id)
            # Having evidence is not the same as having trusted evidence. The
            # revocation cascade withdraws what already stands on a revoked
            # verifier; refusing here is what stops the set being refilled a
            # moment later by a revision that attached the same evidence.
            await assert_evidence_is_trusted(session, revision.approval_evidence_id)

            current = await self._active_revision(session, revision.artifact_id)
            if current is not None and current != revision_id:
                if supersedes is not None and supersedes != current:
                    msg = f"revision {supersedes} is not the active revision of this artifact"
                    raise ArtifactLifecycleError(msg)
                await self._set_state(session, current, LIFECYCLE_SUPERSEDED, now=now, superseded_by=revision_id)

            await session.execute(
                text(
                    "UPDATE arc_revisions SET lifecycle_state = :state, activated_at = :now " "WHERE revision_id = :rid"
                ),
                {"rid": revision_id, "state": LIFECYCLE_ACTIVE, "now": now},
            )
            await self._refresh_obligations(session, revision_id, now=now)
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_ARTIFACT_ACTIVATED,
                payload={
                    "artifact_id": str(revision.artifact_id),
                    "revision_id": str(revision_id),
                    "superseded_revision_id": str(current) if current and current != revision_id else None,
                },
            )

    async def revoke(self, ctx: ArcRequestContext, revision_id: uuid.UUID, *, reason: str) -> None:
        """Withdraw a revision from force, permanently.

        Any mandatory obligation it satisfied becomes `missing_revoked`
        rather than disappearing. That is the whole point: a revoked
        obligation must keep blocking matching resolutions until an approved
        successor satisfies it, and an obligation that vanished with its
        revision would silently unblock everything it used to govern.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            revision = await _lock_family(session, revision_id)
            artifact = await _load_artifact(session, revision.artifact_id)
            self._authorization.assert_can_write_artifact(ctx, artifact)
            _assert_transition(revision.lifecycle_state, LIFECYCLE_REVOKED)

            await self._set_state(session, revision_id, LIFECYCLE_REVOKED, now=now)
            await self._tombstone_obligations(session, revision_id, OBLIGATION_MISSING_REVOKED, now=now)
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_ARTIFACT_REVOKED,
                payload={
                    "artifact_id": str(revision.artifact_id),
                    "revision_id": str(revision_id),
                    "reason": reason[:200],
                },
            )

    async def invalidate(self, ctx: ArcRequestContext, revision_id: uuid.UUID, *, reason: str) -> None:
        """Mark a revision's content no longer trustworthy.

        Distinct from revocation: revocation is a governance decision that
        this rule no longer applies, invalidation says the content itself is
        wrong or its upstream source is gone. Obligations tombstone as
        `missing_invalid` so an auditor can tell the two apart later.

        Operator-driven rather than automatic, because deciding that
        registered content is wrong is a judgement no worker should make.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            revision = await _lock_family(session, revision_id)
            artifact = await _load_artifact(session, revision.artifact_id)
            self._authorization.assert_can_write_artifact(ctx, artifact)
            _assert_transition(revision.lifecycle_state, LIFECYCLE_REVOKED)

            await self._set_state(session, revision_id, LIFECYCLE_REVOKED, now=now)
            await self._tombstone_obligations(session, revision_id, OBLIGATION_MISSING_INVALID, now=now)
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_ARTIFACT_INVALIDATED,
                payload={
                    "artifact_id": str(revision.artifact_id),
                    "revision_id": str(revision_id),
                    "reason": reason[:200],
                },
            )

    # -- row helpers ----------------------------------------------------------

    async def _active_revision(self, session: AsyncSession, artifact_id: uuid.UUID) -> uuid.UUID | None:
        return (
            await session.execute(
                text("SELECT revision_id FROM arc_revisions " "WHERE artifact_id = :aid AND lifecycle_state = :state"),
                {"aid": artifact_id, "state": LIFECYCLE_ACTIVE},
            )
        ).scalar_one_or_none()

    async def _set_state(
        self,
        session: AsyncSession,
        revision_id: uuid.UUID,
        state: str,
        *,
        now: datetime.datetime,
        superseded_by: uuid.UUID | None = None,
    ) -> None:
        await session.execute(
            text(
                "UPDATE arc_revisions SET lifecycle_state = :state, "
                "  superseded_by_revision_id = COALESCE(:superseded_by, superseded_by_revision_id), "
                "  revoked_at = CASE WHEN :state = 'revoked' THEN :now ELSE revoked_at END "
                "WHERE revision_id = :rid"
            ),
            {"rid": revision_id, "state": state, "now": now, "superseded_by": superseded_by},
        )

    # -- obligations ----------------------------------------------------------

    async def _refresh_obligations(
        self, session: AsyncSession, revision_id: uuid.UUID, *, now: datetime.datetime
    ) -> None:
        """Point each mandatory obligation at the newly active revision.

        An obligation is keyed by the *stable* directive id, so activating a
        successor satisfies the obligation its predecessor left behind rather
        than creating a second one. That is what lets a tombstoned obligation
        be cleared by approving a replacement.
        """
        rows = (
            await session.execute(
                text(
                    "SELECT d.directive_id, r.artifact_id, ar.rule_id, ar.effective_from, ar.effective_until, "
                    "       ar.scope, ar.target_tenant_id, ar.capability_ids, ar.domain_ids, ar.intent_kinds, "
                    "       ar.action_classes, ar.environments, ar.data_sensitivity_tiers "
                    "FROM arc_directives d "
                    "JOIN arc_revisions r ON r.revision_id = d.revision_id "
                    "JOIN arc_applicability_rules ar ON ar.revision_id = d.revision_id "
                    "WHERE d.revision_id = :rid AND ar.is_mandatory"
                ),
                {"rid": revision_id},
            )
        ).all()

        for row in rows:
            snapshot = applicability_snapshot(
                scope=row.scope,
                target_tenant_id=row.target_tenant_id,
                capability_ids=row.capability_ids,
                domain_ids=row.domain_ids,
                intent_kinds=row.intent_kinds,
                action_classes=row.action_classes,
                environments=row.environments,
                data_sensitivity_tiers=row.data_sensitivity_tiers,
            )
            canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            digest = applicability_digest(snapshot)

            existing = (
                await session.execute(
                    text(
                        "SELECT obligation_id FROM arc_mandatory_obligations "
                        "WHERE directive_id = :did AND applicability_digest = :digest"
                    ),
                    {"did": row.directive_id, "digest": digest},
                )
            ).scalar_one_or_none()

            if existing is None:
                await session.execute(
                    text(
                        "INSERT INTO arc_mandatory_obligations ("
                        "  artifact_id, directive_id, current_revision_id, applicability_snapshot,"
                        "  applicability_digest, obligation_state, effective_from, effective_until, updated_at"
                        ") VALUES (:aid, :did, :rid, CAST(:snapshot AS JSONB), :digest, :state,"
                        "          :efrom, :euntil, :now)"
                    ),
                    {
                        "aid": row.artifact_id,
                        "did": row.directive_id,
                        "rid": revision_id,
                        "snapshot": canonical,
                        "digest": digest,
                        "state": OBLIGATION_SATISFIED,
                        "efrom": row.effective_from,
                        "euntil": row.effective_until,
                        "now": now,
                    },
                )
            else:
                # The effective window is refreshed along with the revision.
                # The digest is computed over the applicability snapshot,
                # which carries the selectors but not the dates -- so a
                # successor that changes only its rule's window matches an
                # existing obligation and lands here. Leaving the dates
                # behind would strand the obligation on its predecessor's
                # window, and once that window passed, the row would stop
                # being read: a later revocation would tombstone a row
                # nothing loads, and the resolution it should have blocked
                # would come back ready.
                await session.execute(
                    text(
                        "UPDATE arc_mandatory_obligations SET current_revision_id = :rid, "
                        "  obligation_state = :state, effective_from = :efrom, "
                        "  effective_until = :euntil, updated_at = :now WHERE obligation_id = :oid"
                    ),
                    {
                        "oid": existing,
                        "rid": revision_id,
                        "state": OBLIGATION_SATISFIED,
                        "efrom": row.effective_from,
                        "euntil": row.effective_until,
                        "now": now,
                    },
                )

    async def _tombstone_obligations(
        self, session: AsyncSession, revision_id: uuid.UUID, state: str, *, now: datetime.datetime
    ) -> None:
        """Leave the obligation standing, with its revision cleared.

        `current_revision_id` goes NULL because there is no longer a revision
        satisfying it, and the schema's CHECK requires a satisfied obligation
        to name one. The applicability snapshot stays, so resolutions that
        would have matched still block.
        """
        await session.execute(
            text(
                "UPDATE arc_mandatory_obligations SET obligation_state = :state, "
                "  current_revision_id = NULL, updated_at = :now "
                "WHERE current_revision_id = :rid"
            ),
            {"rid": revision_id, "state": state, "now": now},
        )


__all__ = [
    "LIFECYCLE_ACTIVE",
    "LIFECYCLE_DRAFT",
    "LIFECYCLE_EXPIRED",
    "LIFECYCLE_REVOKED",
    "LIFECYCLE_SUPERSEDED",
    "OBLIGATION_MISSING_INVALID",
    "OBLIGATION_MISSING_REVIEW_EXPIRED",
    "OBLIGATION_MISSING_REVOKED",
    "OBLIGATION_SATISFIED",
    "ArtifactLifecycleError",
    "ArtifactService",
    "EvidenceTypeNotWritableError",
]
