"""Approval-trust revocation: withdrawing what a verifier or one piece of
approval evidence vouched for.

Every activated revision and every approved exception carries a claim that
someone verified it -- a registered verifier attesting to the approval, or an
operator signing one directly. `ArtifactService.activate` and
`ExceptionService.approve_exception` check that claim exactly once, at
approval time, and never revisit it: neither has any reason to ask, days or
months later, whether the verifier behind a revision's evidence is still
trusted. Something has to be the place that asks, on the day the answer
becomes no. This module is that place -- the two admin routes that call it
refuse outright rather than answer "yes" by omission when it is unwired; see
the 501 in `arc_admin.py` for what withdrawing a verifier without doing this
would leave standing.

**Why this does not belong on `ArtifactService` or `ExceptionService`.** Both
funnel every write through `assert_can_write_artifact` or
`assert_request_tenant`, which resolve one tenant (or one global scope) per
call and refuse -- or, worse, silently skip -- anything outside it. A
verifier is not scoped to the artifacts that happen to cite it: a single
`trusted_attestation_provider` verifier can be the one every tenant's host
uses, and the evidence rows it produced can each carry a different
`scope_tenant_id`. Withdrawing trust in it has to reach every tenant's
revisions and exceptions in the same breath a tenant-scoped write cannot --
reusing either service would mean the cascade quietly stops at the first
tenant boundary it hits.

**Why authorization is not re-checked here.** `_require_global_operator` in
the admin router already gates both routes on the exact `(issuer, subject)`
deployment-operator pair before either reaches this service. No role check
can substitute for it -- every role including `admin` is tenant-scoped, so no
tenant's admin is the deployment trust root. `ArcAuthorizationService`'s own
`assert_can_write_artifact` encodes that same allowlist, but through an
`ArtifactScope` naming one tenant or one global artifact; a verifier's blast
radius is neither, so there is no scope here to pass it, and calling it
anyway would just re-ask, in a shape that does not fit, a question the
router already answered. What this module does still use from
`ArcAuthorizationService` is `assert_request_tenant` -- a structural check,
not a permission decision -- which refuses the reserved deployment tenant as
a *requesting* identity regardless of what is being requested.

**Why the whole cascade is one transaction.** `ReviewExpiryWorker` tombstones
in capped passes because it is a background job that can always run again
next minute for whatever a pass left behind. Revoking a verifier is not a
background job: an operator who has just learned a verifier is compromised
is saying every revision and exception it vouched for must stop being
trusted *now*. A cascade that applied to half its targets and deferred the
rest would leave the other half looking exactly as trustworthy as before for
however long it took to finish -- the same failure the 501 exists to
prevent, just moved one layer down.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service import audit_outbox
from contextplane.arc.service.artifact import LIFECYCLE_ACTIVE, LIFECYCLE_EXPIRED, OBLIGATION_MISSING_INVALID
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.types import ArcRequestContext
from contextplane.audit import actions
from contextplane.exceptions import NotFoundError
from contextplane.types import Clock

# Revisions eligible for the cascade. A draft revision never bound anyone and
# a superseded one already handed that off to its successor, so cascading
# either would tombstone an obligation something else already satisfies, or
# one that was never live. This is the same set `CorpusReader` treats as
# still governing.
_CASCADABLE_LIFECYCLE_STATES = (LIFECYCLE_ACTIVE, LIFECYCLE_EXPIRED)

# `arc_approval_evidence_revocations.reason_code` is a bounded code, not the
# operator's free text -- the CHECK constraint holds it to 1..64 characters
# because the column exists for an auditor to filter and group on, not to
# read a sentence. The two values below are the two ways evidence ends up
# revoked through this service, and keeping them distinct lets that auditor
# tell "this evidence was pulled because its verifier was revoked" apart from
# "this one approval, specifically, was pulled while its verifier stayed
# trusted" without cross-referencing the verifier-revocation audit trail.
_REASON_CODE_VERIFIER_REVOKED = "verifier_revoked"
_REASON_CODE_EVIDENCE_REVOKED = "evidence_revoked"


def _reason_digest(reason: str) -> str:
    """The operator's free-text reason, at rest only as a digest.

    `reason_code` carries the bounded, queryable cause; the sentence an
    operator typed is never written to a column the schema CHECKs to 64
    characters -- that would either truncate it silently or reject an
    otherwise-valid revocation for a reason unrelated to the revocation
    itself.
    """
    return hashlib.sha256(reason.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class CascadeResult:
    """What one call actually changed -- the audit row's payload, not a log.

    Counts only, deliberately. A verifier cited by every tenant's revisions
    must not turn one revocation into a payload that lists them all; see
    `audit_outbox.MAX_PAYLOAD_BYTES`. Each count reflects what *this* call
    changed, not the total standing state -- a repeat call reports zeros
    once there is nothing left for it to do, which is what makes the audit
    trail for a redundant revoke distinguishable from the one that mattered.
    """

    evidence_revoked: int
    revisions_revoked: int
    exceptions_revoked: int


class ApprovalTrustService:
    """Withdraws trust in an approval verifier, or in one piece of evidence.

    Deployment-wide and tenant-agnostic by construction: every query here
    matches on the verifier id or evidence id alone, never on a tenant,
    because the whole point of this service is to reach rows regardless of
    which tenant they belong to.
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

    async def revoke_verifier(self, ctx: ArcRequestContext, approval_verifier_id: str, *, reason: str) -> None:
        """Withdraw trust in a verifier and cascade to everything it vouched for.

        Matches evidence through *both* `approval_verifier_id` and
        `signer_key_id` -- the schema's `ck_arc_evidence_representation`
        CHECK guarantees exactly one is set per evidence row depending on
        `verification_method`, so a query that matched only one column would
        leave every `operator_signed` (or every `verifier_attested`)
        approval this verifier made standing. That is not a smaller version
        of this cascade; it is half a cascade that looks complete.

        Idempotent the way `HostSignerKeyRegistry.revoke` is: a second call
        preserves the first `revoked_at` rather than moving it, and every
        step downstream (the evidence-revocation insert, the revision and
        exception cascades) is guarded by a predicate only an unrevoked row
        satisfies -- so a repeat call finds nothing left to do and still
        succeeds rather than erroring.
        """
        self._authorization.assert_request_tenant(ctx)
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            exists = (
                await session.execute(
                    text("SELECT 1 FROM arc_approval_verifiers WHERE approval_verifier_id = :vid"),
                    {"vid": approval_verifier_id},
                )
            ).one_or_none()
            if exists is None:
                msg = f"approval verifier {approval_verifier_id!r} not found"
                raise NotFoundError(msg)

            # The `revoked_at IS NULL` guard is what makes this idempotent: a
            # re-revoke matches zero rows instead of overwriting the first
            # timestamp. Moving it later could retroactively legitimize
            # whatever relied on this verifier in the interval between the
            # two calls.
            await session.execute(
                text(
                    "UPDATE arc_approval_verifiers SET revoked_at = :now "
                    "WHERE approval_verifier_id = :vid AND revoked_at IS NULL"
                ),
                {"vid": approval_verifier_id, "now": now},
            )

            evidence_ids = [
                row.evidence_id
                for row in (
                    await session.execute(
                        text(
                            "SELECT evidence_id FROM arc_approval_evidence "
                            "WHERE approval_verifier_id = :vid OR signer_key_id = :vid"
                        ),
                        {"vid": approval_verifier_id},
                    )
                ).all()
            ]
            result = await self._cascade(
                session,
                evidence_ids,
                reason_code=_REASON_CODE_VERIFIER_REVOKED,
                reason=reason,
                actor_id=ctx.actor_id,
                now=now,
            )

            await audit_outbox.emit_global(
                session,
                event_type=actions.ARC_APPROVAL_VERIFIER_REVOKED,
                payload={
                    "approval_verifier_id": approval_verifier_id,
                    "reason": reason[:200],
                    "evidence_revoked_count": result.evidence_revoked,
                    "revisions_revoked_count": result.revisions_revoked,
                    "exceptions_revoked_count": result.exceptions_revoked,
                },
            )

    async def revoke_evidence(self, ctx: ArcRequestContext, evidence_id: uuid.UUID, *, reason: str) -> None:
        """Withdraw one piece of evidence; the verifier behind it stays trusted.

        Narrower than `revoke_verifier`: exactly one evidence row is matched,
        so at most one revision family and one exception are affected.
        Otherwise this runs the identical cascade -- an approval granted in
        error is no less able to leave a revision standing on withdrawn
        trust than a whole compromised verifier is; it is just smaller in
        scope.
        """
        self._authorization.assert_request_tenant(ctx)
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            exists = (
                await session.execute(
                    text("SELECT 1 FROM arc_approval_evidence WHERE evidence_id = :eid"),
                    {"eid": evidence_id},
                )
            ).one_or_none()
            if exists is None:
                msg = f"approval evidence {evidence_id} not found"
                raise NotFoundError(msg)

            result = await self._cascade(
                session,
                [evidence_id],
                reason_code=_REASON_CODE_EVIDENCE_REVOKED,
                reason=reason,
                actor_id=ctx.actor_id,
                now=now,
            )

            await audit_outbox.emit_global(
                session,
                event_type=actions.ARC_APPROVAL_EVIDENCE_REVOKED,
                payload={
                    "evidence_id": str(evidence_id),
                    "reason": reason[:200],
                    "revisions_revoked_count": result.revisions_revoked,
                    "exceptions_revoked_count": result.exceptions_revoked,
                },
            )

    # -- the shared cascade -----------------------------------------------------

    async def _cascade(
        self,
        session: AsyncSession,
        evidence_ids: list[uuid.UUID],
        *,
        reason_code: str,
        reason: str,
        actor_id: uuid.UUID,
        now: datetime.datetime,
    ) -> CascadeResult:
        """Revoke every evidence row in `evidence_ids`, then withdraw what each
        one vouched for.

        One implementation serves both callers: `revoke_verifier` passes
        every evidence id its verifier ever produced, `revoke_evidence`
        passes exactly one. From here down, neither the revocation-row
        insert nor the revision and exception cascades know or need to know
        which caller built the list.
        """
        if not evidence_ids:
            return CascadeResult(evidence_revoked=0, revisions_revoked=0, exceptions_revoked=0)

        # Append-only and idempotent: a row already present for an evidence
        # id -- this is a re-run, or `revoke_evidence` on something a
        # verifier revocation already reached -- is left exactly as it was
        # rather than rewritten. There is no un-revoke, so there is nothing a
        # second write should legitimately change. `RETURNING` reports only
        # the rows this call actually inserted, which is what lets the audit
        # payload describe this call rather than the cumulative total.
        newly_revoked = (
            await session.execute(
                text(
                    "INSERT INTO arc_approval_evidence_revocations "
                    "  (evidence_id, revoked_at, reason_code, reason_digest, revoked_by_actor_id) "
                    "SELECT unnest(CAST(:eids AS uuid[])), :now, :reason_code, :reason_digest, :actor_id "
                    "ON CONFLICT (evidence_id) DO NOTHING "
                    "RETURNING evidence_id"
                ),
                {
                    "eids": [str(eid) for eid in evidence_ids],
                    "now": now,
                    "reason_code": reason_code,
                    "reason_digest": _reason_digest(reason),
                    "actor_id": actor_id,
                },
            )
        ).all()

        # Only a revision still `active` or `expired` is currently binding
        # anyone; a `draft` never was and a `superseded` one already handed
        # that off to its successor, so touching either would tombstone an
        # obligation something else already satisfies, or one that was never
        # live.
        revoked_revisions = (
            await session.execute(
                text(
                    "UPDATE arc_revisions SET lifecycle_state = 'revoked', revoked_at = :now "
                    "WHERE approval_evidence_id = ANY(:eids) AND lifecycle_state = ANY(:states) "
                    "RETURNING revision_id"
                ),
                {"eids": evidence_ids, "now": now, "states": list(_CASCADABLE_LIFECYCLE_STATES)},
            )
        ).all()
        revision_ids = [row.revision_id for row in revoked_revisions]

        if revision_ids:
            # `OBLIGATION_MISSING_INVALID`, not `OBLIGATION_MISSING_REVOKED`:
            # `ArtifactService.revoke` tombstones as `missing_revoked` when
            # governance decides a rule no longer applies -- a decision about
            # the rule itself. Here the rule was never the problem; the
            # trust behind its approval was. That is the same distinction
            # `ArtifactService.invalidate` draws for content that turns out
            # to be wrong rather than deliberately withdrawn, and it matters
            # here for the same reason: an auditor reading the tombstone
            # later needs to be able to tell "this was withdrawn by choice"
            # from "this was never actually approved the way it claimed to
            # be" without cross-referencing the verifier-revocation trail to
            # find out which.
            await session.execute(
                text(
                    "UPDATE arc_mandatory_obligations SET obligation_state = :state, "
                    "  current_revision_id = NULL, updated_at = :now "
                    "WHERE current_revision_id = ANY(:rids)"
                ),
                {"rids": revision_ids, "state": OBLIGATION_MISSING_INVALID, "now": now},
            )

        revoked_exceptions = (
            await session.execute(
                text(
                    "UPDATE arc_approved_exceptions SET revoked_at = :now "
                    "WHERE approval_evidence_id = ANY(:eids) AND revoked_at IS NULL "
                    "RETURNING exception_id"
                ),
                {"eids": evidence_ids, "now": now},
            )
        ).all()

        return CascadeResult(
            evidence_revoked=len(newly_revoked),
            revisions_revoked=len(revision_ids),
            exceptions_revoked=len(revoked_exceptions),
        )


__all__ = ["ApprovalTrustService", "CascadeResult"]
