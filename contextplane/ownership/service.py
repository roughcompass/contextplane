"""Assign, validate, supersede and revoke ownership — and never touch authorization.

Ownership answers "who is accountable for this". Authorization answers "what may
this caller do". Wiring one to the other would make assigning an owner a
privilege-escalation primitive: anybody who could name themselves owner would
thereby gain whatever owners are permitted to do. So nothing in this module reads
or writes an entitlement, a role the auth layer understands, or a grant — and
`tests/conformance/test_ownership_non_authorization.py` inspects the auth code to
prove the reverse direction too.

**A transition is a recorded move, not a column update.** Each one appends a row
with its own sequence, actor, time and reason, and the reason is required. An
ownership change with no reason is unreviewable: months later the row says who
holds it and nothing says why it moved, which is exactly what an accountability
record exists to answer.

**Legal moves are the ones the schema declares.** `draft → proposed → validated`,
with `revoked` reachable from any live state and `superseded` only from
`validated`. The check constraint enforces it; this module refuses first so the
caller gets the move named rather than a constraint violation.

**Replacement is explicit and one-way.** Superseding points the old assignment at
the new one, so the history is walkable, and the replacement link may only be set
on a superseded row — the schema says so too.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.entities import assertions
from contextplane.entities.provenance import AssertionProvenance
from contextplane.ownership import queries
from contextplane.ownership.queries import (
    DRAFT,
    PROPOSED,
    REVOKED,
    SUPERSEDED,
    VALIDATED,
    OwnershipAssignment,
)
from contextplane.types import Clock

#: Every legal move, mirroring the schema's own constraint. Absence is a refusal
#: rather than an unknown: a move nobody declared is one nobody reasoned about.
LEGAL_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    DRAFT: frozenset({PROPOSED, REVOKED}),
    PROPOSED: frozenset({VALIDATED, REVOKED}),
    VALIDATED: frozenset({SUPERSEDED, REVOKED}),
    SUPERSEDED: frozenset(),
    REVOKED: frozenset(),
}


class OwnershipError(RuntimeError):
    """A refusal from the ownership service, named so a transport can map it."""


class AssignmentNotFound(OwnershipError):
    """No such assignment for this tenant."""


class IllegalTransition(OwnershipError):
    """The requested move is not one the lifecycle declares."""


class SubjectMismatch(OwnershipError):
    """The owned target does not exist, or belongs to another tenant."""


@dataclasses.dataclass(frozen=True)
class OwnershipService:
    """Writes ownership assignments and their transition history.

    A frozen dataclass rather than a class with a constructor body: it holds two
    collaborators and no mutable state, and an ownership service that could be
    reconfigured after construction is one whose clock could move between a
    transition and the row it stamps.
    """

    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock

    async def assign(
        self,
        *,
        tenant_id: uuid.UUID,
        owner_principal: str,
        owned_target_kind: str,
        owned_target_id: uuid.UUID,
        role: str,
        scope: str,
        source: str,
        recorded_by: str,
        profile_revision_id: uuid.UUID,
        derivation_method: str | None = None,
        confidence: float | None = None,
        effective_from: datetime.datetime | None = None,
    ) -> OwnershipAssignment:
        """Record a new assignment in `draft`, with its own provenance.

        Starts in `draft` rather than `validated` because an assignment nobody has
        confirmed is a proposal. Creating one already in force would let any caller
        establish accountability for anything by asserting it.

        The subject is checked before anything is written: an assignment naming a
        target that does not exist is unresolvable forever, and one naming another
        tenant's target is a claim this tenant cannot make.
        """
        now = self.clock.now()
        started = effective_from if effective_from is not None else now

        async with self.session_factory() as session, session.begin():
            await self._assert_subject(session, tenant_id, owned_target_kind, owned_target_id)

            provenance_id = await assertions.record(
                session,
                AssertionProvenance(
                    tenant_id=tenant_id,
                    source_system="contextplane",
                    source_namespace="ownership",
                    ingested_at=now,
                    authority="derived" if derivation_method else "canonical_owner",
                    freshness_state="fresh",
                    produced_by=recorded_by,
                    validating_profile_revision_id=profile_revision_id,
                    derivation_method=derivation_method,
                    confidence=confidence,
                ),
            )
            assignment_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO ownership_assignments ("
                    "  ownership_assignment_id, tenant_id, owner_principal, owned_target_kind,"
                    "  owned_target_id, role, scope, source, derivation_method, confidence,"
                    "  validation_state, effective_from, provenance_id, recorded_by, recorded_at"
                    ") VALUES (:aid, :tid, :owner, :kind, :target, :role, :scope, :source,"
                    "          :method, :confidence, :state, :from, :pid, :by, :now)"
                ),
                {
                    "aid": assignment_id,
                    "tid": tenant_id,
                    "owner": owner_principal,
                    "kind": owned_target_kind,
                    "target": owned_target_id,
                    "role": role,
                    "scope": scope,
                    "source": source,
                    "method": derivation_method,
                    "confidence": confidence,
                    "state": DRAFT,
                    "from": started,
                    "pid": provenance_id,
                    "by": recorded_by,
                    "now": now,
                },
            )
            assignment = await queries.get(session, tenant_id=tenant_id, assignment_id=assignment_id)

        assert assignment is not None  # noqa: S101 - just written in this transaction
        return assignment

    async def transition(
        self,
        *,
        tenant_id: uuid.UUID,
        assignment_id: uuid.UUID,
        to_state: str,
        reason: str,
        recorded_by: str,
        replaced_by_assignment_id: uuid.UUID | None = None,
    ) -> OwnershipAssignment:
        """Move an assignment, appending the move to its history.

        `reason` is required by the signature, not merely by the column. An
        ownership change with no reason is unreviewable: the row says who holds it
        and nothing says why it moved.
        """
        if not reason.strip():
            msg = "an ownership transition states its reason; without one the change cannot be reviewed later"
            raise OwnershipError(msg)

        now = self.clock.now()
        async with self.session_factory() as session, session.begin():
            current = await queries.get(session, tenant_id=tenant_id, assignment_id=assignment_id)
            if current is None:
                raise AssignmentNotFound(f"no ownership assignment {assignment_id} for this tenant")

            allowed = LEGAL_TRANSITIONS.get(current.validation_state, frozenset())
            if to_state not in allowed:
                msg = (
                    f"{current.validation_state!r} does not move to {to_state!r}; legal moves from here are "
                    f"{sorted(allowed) or 'none — this assignment is terminal'}"
                )
                raise IllegalTransition(msg)
            if to_state == SUPERSEDED and replaced_by_assignment_id is None:
                msg = "superseding names the assignment that replaces it, or the history cannot be walked forward"
                raise OwnershipError(msg)
            if to_state != SUPERSEDED and replaced_by_assignment_id is not None:
                msg = "only a superseded assignment carries a replacement link"
                raise OwnershipError(msg)

            sequence = (
                await session.execute(
                    text(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM ownership_assignment_transitions"
                        " WHERE ownership_assignment_id = :a"
                    ),
                    {"a": assignment_id},
                )
            ).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO ownership_assignment_transitions ("
                    "  transition_id, ownership_assignment_id, sequence, from_state, to_state,"
                    "  reason, recorded_by, recorded_at"
                    ") VALUES (:tid, :aid, :seq, :frm, :to, :reason, :by, :now)"
                ),
                {
                    "tid": uuid.uuid4(),
                    "aid": assignment_id,
                    "seq": sequence,
                    "frm": current.validation_state,
                    "to": to_state,
                    "reason": reason,
                    "by": recorded_by,
                    "now": now,
                },
            )
            await session.execute(
                text(
                    "UPDATE ownership_assignments"
                    "   SET validation_state = :state,"
                    "       revocation_reason = CASE WHEN :state = 'revoked' THEN :reason ELSE revocation_reason END,"
                    "       replaced_by_assignment_id = COALESCE(:replacement, replaced_by_assignment_id),"
                    # An end is recorded only when there was time to end. An
                    # assignment revoked at the instant it began was never in
                    # force, so there is no moment at which it stopped being --
                    # and the schema refuses an empty interval outright. Its
                    # state is what excludes it from the views either way.
                    "       effective_to = CASE"
                    "         WHEN :state IN ('revoked', 'superseded') AND :now > effective_from THEN :now"
                    "         ELSE effective_to END"
                    " WHERE tenant_id = :tid AND ownership_assignment_id = :aid"
                ),
                {
                    "state": to_state,
                    "reason": reason,
                    "replacement": replaced_by_assignment_id,
                    "now": now,
                    "tid": tenant_id,
                    "aid": assignment_id,
                },
            )
            moved = await queries.get(session, tenant_id=tenant_id, assignment_id=assignment_id)

        assert moved is not None  # noqa: S101 - read back inside the same transaction
        return moved

    @staticmethod
    async def _assert_subject(
        session: AsyncSession, tenant_id: uuid.UUID, owned_target_kind: str, owned_target_id: uuid.UUID
    ) -> None:
        """The owned target exists and belongs to this tenant.

        Only entity targets are resolvable here today; a kind this cannot check is
        accepted rather than refused, because refusing an unknown kind would make
        this module the gatekeeper of what may be owned, which is the profile's
        job and not this one's.
        """
        if owned_target_kind != "entity":
            return
        owner_tenant = (
            await session.execute(
                text("SELECT tenant_id FROM entities WHERE entity_id = :eid"), {"eid": owned_target_id}
            )
        ).scalar_one_or_none()
        if owner_tenant is None:
            raise SubjectMismatch("the owned target does not exist")
        if owner_tenant != tenant_id:
            raise SubjectMismatch("the owned target belongs to another tenant")


__all__ = [
    "LEGAL_TRANSITIONS",
    "AssignmentNotFound",
    "IllegalTransition",
    "OwnershipError",
    "OwnershipService",
    "SubjectMismatch",
]
