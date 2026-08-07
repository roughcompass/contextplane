"""`ActivationService`: the ADR 040 Sec.5 / ADR 041 Sec.8 ten-predicate
atomic activation gate -- the whole point of the safety invariant this
authoring surface exists to enforce. No authored revision becomes `active`,
and no proposal version becomes `activated`, unless the current transaction
proves every one of the ten named predicates in `activation_predicates.py`
(split there purely for this repo's file-size ceiling; that module's own
docstring explains the shared "call a helper, then guard" shape every
predicate uses). There is no flag, no default, and no partial write that
gets there any other way.

**Predicate 10 (`operational_integrity`) is hard-wired to refuse in this
commit.** `PREDICATE_ORDER`'s tenth entry is evaluated, reported, and always
`satisfied=False` -- so `activate()` compiles, every other predicate is
fully real and independently testable, and `POST .../activate` is reachable
but cannot return success. A later commit replaces exactly that one
predicate's bracketed guard (never deletes or works around it) with a real
`registry.arc.service.integrity.RevisionIntegrityService.assess` call, at
the same moment the four other read-path callers that service names start
enforcing it too -- activation and safe serving arrive together, or not at
all. See `activation_predicates.check_operational_integrity`'s own
docstring.

**Why nine predicates are real here and not deferred to that same later
commit.** `RevisionIntegrityService.assess` (once wired) returns one bounded
valid/refused signal for the read path; this class's `predicates[]` is a
per-name breakdown a human approver or an operator reads to see *which*
gate is unmet. The two will always overlap in what they check without ever
being the same call -- a detailed eligibility report and a bounded serving
gate are different consumers with different disclosure rules, not one
collapsing into the other.

**Lock order (ADR 040): loads first, locks second, recomputes under lock.**
Evidence and its verifier are read read-only before any write lock is taken
(a plain, unlocked `SELECT` for the evidence row; `FOR SHARE` for the
verifier, matching `VerifierRegistry.get`'s own D4 reasoning -- a share lock
still serializes against a revocation's implicit `FOR UPDATE`, without
itself blocking a second concurrent reader). Write locks then follow in one
fixed class order -- artifact family, proposal version, revision family
(ascending `revision_id` within it, via `artifact_integrity._lock_family`),
qualification -- and every predicate that reads a locked row reads the value
the lock just re-fetched, never the earlier unlocked snapshot. Deadlock
retry is not success: a caller that retries after a serialization failure
re-enters this same method and re-proves every predicate from scratch, it
never resumes a partial evaluation.

**The old partial lifecycle method.** `ArtifactService.activate` (`artifact.
py`) is a distinct, pre-existing write path for content an operator
registers directly as an already-approved upstream projection --
`arc_artifacts.active_revision_id`, the column this predicate's
baseline-drift check compare-and-swaps, was added by the same migration
that introduced this authoring surface's proposal aggregate and has never
been written by that older path (see `ArcArtifact.active_revision_id`'s own
model comment: "nothing writes it before that predicate exists"). The two
paths write disjoint columns for disjoint content and neither can bypass
the other's checks; this module does not touch `artifact.py`. See this
task's own outcome notes for the evidence trail if that scope decision
needs revisiting.

**Every failed predicate except one is a no-write refusal.** A reducer
retired out from under a nonterminal candidate (`risk_reproducible`'s own
stale-trigger case) is the one exception ADR 041 names: `activate` may
atomically terminalize the proposal version as `stale` and audit that
transition. Every other failed predicate leaves the database exactly as it
found it -- no lifecycle change, no state transition, no success audit.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service import activation_predicates as predicates
from registry.arc.service import audit_outbox
from registry.arc.service.activation_predicates import PREDICATE_ORDER, PredicateResult, SourceStatusChecker
from registry.arc.service.approval_challenge import ReviewPackageDigests
from registry.arc.service.approval_challenge_verification import (
    SignatureVerifier,
    VerifierAttestationProvider,
    _ed25519_verify,
)
from registry.arc.service.artifact import ArtifactService
from registry.arc.service.artifact_integrity import _lock_family
from registry.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from registry.arc.service.queries import activation as queries
from registry.arc.service.queries import approval as approval_queries
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.queries import review_package as rp_queries
from registry.arc.service.review_package import (
    ReviewPackageIntegrityError,
    ReviewPackageService,
    ReviewPackageUnavailable,
)
from registry.arc.types import ArcRequestContext, AuthorityScope
from registry.audit import actions
from registry.exceptions import NotFoundError, RegistryError
from registry.types import Clock

#: The reason_code this task's one write-bearing failure records on the
#: proposal version's own terminal transition -- an internal audit detail,
#: not a wire `ReasonCode` from a closed vocabulary (see `proposal.py`'s own
#: docstring on why that field stays a plain string today).
_STALE_REASON_CODE = "risk_reducer_retired"


class ActivationError(RegistryError):
    """Base of every refusal this module raises."""


class ActivationPredicateFailed(ActivationError):
    """One or more of the ten predicates did not hold (`arc_activation_
    predicate_failed`, 409). Carries no evidence and names no predicate in
    its own message body beyond what the caller already has from a prior
    `GET .../activation-eligibility` call -- the bounded `predicates[]`
    breakdown is that route's job, not this exception's.
    """


class ActivationRequestMismatch(ActivationError):
    """`ActivateRequest.proposal_id`/`proposal_version` do not name the
    proposal version bound to `revision_id` (`arc_proposal_state_conflict`,
    409) -- a client request error, checked before any predicate runs.
    """


@dataclasses.dataclass(frozen=True)
class ActivationEligibility:
    """`eligible` is exactly `all(p.satisfied for p in predicates)`; kept as
    its own field rather than a property so the wire response and this
    dataclass can never independently drift on how "eligible" is derived.
    `predicates` always carries all ten, in §2.2 order -- see
    `activation_predicates.PREDICATE_ORDER`.
    """

    eligible: bool
    predicates: tuple[PredicateResult, ...]


@dataclasses.dataclass(frozen=True)
class RevisionActivation:
    """What a successful `activate()`/`revoke()` hands back -- the same
    shape `RevisionResponse` projects."""

    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    lifecycle_state: str
    operational_integrity_state: str
    activated_at: datetime.datetime | None
    revoked_at: datetime.datetime | None


@dataclasses.dataclass(frozen=True)
class _Evaluation:
    """The full internal result `_evaluate` produces -- `ActivationEligibility`
    plus the two facts only `activate()` itself needs: whether `risk_
    reproducible` failed specifically because its bound reducer was retired
    (the one failure mode with a write side effect), and the loaded family
    row `activate()`'s own write phase reuses rather than re-reading a
    second time under the same lock.
    """

    eligibility: ActivationEligibility
    stale_reducer_retired: bool
    family: proposal_queries.FamilyRow


def _scope(tenant_id: uuid.UUID | None) -> ArtifactScope:
    scope = AuthorityScope.GLOBAL if tenant_id is None else AuthorityScope.TENANT
    return ArtifactScope(scope=scope, tenant_id=tenant_id)


class ActivationService:
    """Owns the global lock order and all ten named predicates. See the
    module docstring for the safety invariant this class exists to prove on
    every call, with no exception.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
        review_package: ReviewPackageService,
        source_status: SourceStatusChecker,
        artifacts: ArtifactService,
        attestation_providers: dict[str, VerifierAttestationProvider] | None = None,
        signature_verifier: SignatureVerifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock
        self._review_package = review_package
        self._source_status = source_status
        # `revoke` delegates to the exact same lifecycle transition
        # `ArtifactService.revoke` already implements for the other write
        # path -- revocation carries none of activation's predicate
        # complexity, and a second, independent obligation-tombstoning
        # implementation here would be a second place for the two to drift.
        self._artifacts = artifacts
        self._attestation_providers = dict(attestation_providers or {})
        # Injectable, matching `RevisionIntegrityService`'s own identical
        # parameter: a unit test can supply a trivial fake instead of
        # exercising real Ed25519 verification for every planted case that
        # never needs to reach a signature check at all.
        self._signature_verifier = signature_verifier or _ed25519_verify

    # -- the read-only eligibility report --------------------------------------

    async def get_eligibility(self, ctx: ArcRequestContext, revision_id: uuid.UUID) -> ActivationEligibility:
        """`GET .../activation-eligibility`. Reports eligibility as if *this*
        authenticated caller were the one activating -- the actor-separation
        predicate needs a hypothetical activator identity to say anything
        about the third-distinct-identity rule, and the caller asking "am I
        eligible to activate this" is the only identity this read has any
        business assuming. Takes the same write locks `activate()` would
        (see the module docstring's lock-order section) and releases them
        on this read-only transaction's own commit, so a concurrent
        activation cannot interleave with this recomputation.
        """
        async with self._session_factory() as session, session.begin():
            version = await proposal_queries.load_version_by_revision_id(session, revision_id)
            if version is None:
                raise NotFoundError(f"revision {revision_id} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"revision {revision_id} references a vanished artifact family")
            self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
            evaluation = await self._evaluate(
                session,
                revision_id,
                activator_issuer=ctx.oidc_issuer,
                activator_subject=ctx.oidc_subject,
                qualification_id=None,
            )
        return evaluation.eligibility

    async def check_eligibility(self, session: AsyncSession, revision_id: uuid.UUID) -> ActivationEligibility:
        """The bare, no-authorization, no-hypothetical-activator core --
        matches `ReviewPackageService.assemble`'s own convention (see that
        class's docstring: authorization and identity are the caller's job,
        this takes the caller's own open transaction). With no activator
        known, `actor_separation`'s third-distinct-identity rule for a
        global-mandatory candidate cannot be confirmed and fails closed;
        every other predicate is unaffected.
        """
        evaluation = await self._evaluate(
            session, revision_id, activator_issuer=None, activator_subject=None, qualification_id=None
        )
        return evaluation.eligibility

    # -- activation -------------------------------------------------------------

    async def activate(
        self,
        ctx: ArcRequestContext,
        *,
        revision_id: uuid.UUID,
        proposal_id: uuid.UUID,
        proposal_version: int,
        qualification_id: uuid.UUID | None,
    ) -> RevisionActivation:
        """One transaction: loads, locks, recomputes every predicate, and --
        only if every one holds -- commits revision activation, the
        proposal's own `approved -> activated` transition, the family's
        active-revision compare-and-swap, and audit, together. Any
        exception raised anywhere below rolls back everything, including a
        supersession write already issued in this same call.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            identity = await proposal_queries.load_version_by_revision_id(session, revision_id)
            if identity is None:
                raise NotFoundError(f"revision {revision_id} not found")
            if identity.proposal_id != proposal_id or identity.proposal_version != proposal_version:
                msg = (
                    f"revision {revision_id} is bound to proposal version "
                    f"{identity.proposal_id}/{identity.proposal_version}, not {proposal_id}/{proposal_version}"
                )
                raise ActivationRequestMismatch(msg)
            family = await proposal_queries.load_family(session, identity.artifact_id)
            if family is None:
                raise RegistryError(f"revision {revision_id} references a vanished artifact family")
            self._authorization.assert_can_write_artifact(ctx, _scope(family.tenant_id))

            evaluation = await self._evaluate(
                session,
                revision_id,
                activator_issuer=ctx.oidc_issuer,
                activator_subject=ctx.oidc_subject,
                qualification_id=qualification_id,
            )

            if evaluation.stale_reducer_retired:
                # ADR 041's one write-bearing failure: the reducer this
                # candidate's risk classification was bound to has since
                # been retired. Recovery is not an in-place reinterpretation
                # under whatever reducer is current -- the version
                # terminalizes, and its author opens a fresh one.
                await proposal_queries.transition_version(
                    session,
                    proposal_id=proposal_id,
                    proposal_version=proposal_version,
                    from_states=("approved",),
                    to_state="stale",
                    reason_code=_STALE_REASON_CODE,
                    note=None,
                    actor_issuer=ctx.oidc_issuer,
                    actor_subject=ctx.oidc_subject,
                    now=now,
                )
                await audit_outbox.emit(
                    session,
                    tenant_id=ctx.tenant_id,
                    event_type=actions.ARC_PROPOSAL_STALE,
                    payload={
                        "proposal_id": str(proposal_id),
                        "proposal_version": proposal_version,
                        "reason_code": _STALE_REASON_CODE,
                    },
                )
                raise ActivationPredicateFailed(
                    f"proposal version {proposal_id}/{proposal_version} terminalized as stale: "
                    "its bound risk reducer has been retired"
                )

            if not evaluation.eligibility.eligible:
                # No write above this point in the transaction -- the
                # `session.begin()` block simply ends with nothing to
                # commit, which is what "commits no lifecycle change and no
                # success audit" means operationally: rollback and commit
                # are indistinguishable when there is nothing to undo.
                raise ActivationPredicateFailed(f"revision {revision_id} does not satisfy every activation predicate")

            return await self._commit_activation(session, revision_id=revision_id, family=family, now=now, ctx=ctx)

    async def _commit_activation(
        self,
        session: AsyncSession,
        *,
        revision_id: uuid.UUID,
        family: proposal_queries.FamilyRow,
        now: datetime.datetime,
        ctx: ArcRequestContext,
    ) -> RevisionActivation:
        """The write set a fully-satisfied evaluation unlocks. Every
        statement here runs under the write locks `_evaluate` already
        acquired in this same transaction -- no further locking is needed,
        and no predicate here is re-checked, because re-checking after
        already proving every predicate under the same lock would only be
        re-reading rows this transaction itself is holding exclusively.
        """
        current_active = family.active_revision_id
        if current_active is not None and current_active != revision_id:
            await queries.supersede_revision_row(
                session, revision_id=current_active, superseded_by_revision_id=revision_id, now=now
            )
        activated = await queries.activate_revision_row(session, revision_id=revision_id, now=now)
        if not activated:
            msg = f"revision {revision_id} was not in 'draft' at the moment of its own locked activation"
            raise RegistryError(msg)
        cas_won = await queries.cas_active_revision(
            session,
            artifact_id=family.artifact_id,
            expected_active_revision_id=current_active,
            new_active_revision_id=revision_id,
        )
        if not cas_won:
            msg = f"artifact {family.artifact_id}'s active-revision compare-and-swap lost under lock"
            raise RegistryError(msg)

        row = await queries.load_revision(session, revision_id)
        if row is None:  # pragma: no cover - just activated in this same transaction
            msg = f"revision {revision_id} vanished immediately after its own activation"
            raise RegistryError(msg)

        await audit_outbox.emit(
            session,
            tenant_id=ctx.tenant_id,
            event_type=actions.ARC_ARTIFACT_ACTIVATED,
            payload={
                "artifact_id": str(row.artifact_id),
                "revision_id": str(revision_id),
                "superseded_revision_id": str(current_active) if current_active is not None else None,
            },
        )
        await audit_outbox.emit(
            session,
            tenant_id=ctx.tenant_id,
            event_type=actions.ARC_PROPOSAL_ACTIVATED,
            payload={"revision_id": str(revision_id)},
        )

        # No new `arc_operational_events` row is appended here. Every event
        # type this deployment's schema accepts is fixed by a migration-
        # level CHECK constraint this task does not touch (see the module
        # docstring's own scope note); appending one that names "activation"
        # would need a new closed literal added to that constraint, which
        # this commit does not do. This leaves no gap today: predicate 10
        # above hard-refuses every call before this method is ever reached,
        # so no successful activation exists yet for an unrecorded
        # operational transition to be missing from. Recorded here rather
        # than left implicit, for whichever task first makes this method
        # reachable.
        return RevisionActivation(
            revision_id=row.revision_id,
            artifact_id=row.artifact_id,
            lifecycle_state=row.lifecycle_state,
            # Fixed at "pending" for the same reason `proposal.py`'s own
            # `_operational_integrity_state` helper is: no revision's
            # checkpoint has ever been assessed as durable by a caller that
            # actually reaches this far, because none has -- predicate 10
            # above never lets this method run.
            operational_integrity_state="pending",
            activated_at=row.activated_at,
            revoked_at=row.revoked_at,
        )

    # -- revocation ---------------------------------------------------------

    async def revoke(
        self, ctx: ArcRequestContext, revision_id: uuid.UUID, *, reason_code: str, note: str | None
    ) -> RevisionActivation:
        """`POST .../revoke`. Delegates the actual transition to
        `ArtifactService.revoke` -- see this class's own `__init__`
        docstring for why revocation is shared rather than reimplemented.
        """
        reason = reason_code if not note else f"{reason_code}: {note}"
        await self._artifacts.revoke(ctx, revision_id, reason=reason)
        async with self._session_factory() as session:
            row = await queries.load_revision(session, revision_id)
        if row is None:  # pragma: no cover - just revoked in the call above
            msg = f"revision {revision_id} vanished immediately after its own revocation"
            raise RegistryError(msg)
        return RevisionActivation(
            revision_id=row.revision_id,
            artifact_id=row.artifact_id,
            lifecycle_state=row.lifecycle_state,
            operational_integrity_state="pending",
            activated_at=row.activated_at,
            revoked_at=row.revoked_at,
        )

    # -- the shared evaluation core ---------------------------------------------

    async def _evaluate(
        self,
        session: AsyncSession,
        revision_id: uuid.UUID,
        *,
        activator_issuer: str | None,
        activator_subject: str | None,
        qualification_id: uuid.UUID | None,
    ) -> _Evaluation:
        now = self._clock.now()

        # -- loads, read-only, before any write lock -----------------------
        unlocked_identity = await proposal_queries.load_version_by_revision_id(session, revision_id)
        if unlocked_identity is None:
            raise NotFoundError(f"revision {revision_id} not found")
        proposal_id = unlocked_identity.proposal_id
        proposal_version = unlocked_identity.proposal_version

        live_evidence = await approval_queries.load_live_evidence_by_revision(session, revision_id)
        verifier = None
        if live_evidence is not None:
            verifier = await approval_queries.load_verifier_for_share(session, live_evidence.approval_verifier_id)

        # -- write locks, ascending class order: artifact, proposal
        #    version, revision family, qualification -----------------------
        family = await proposal_queries.load_family_for_update(session, unlocked_identity.artifact_id)
        if family is None:
            raise RegistryError(f"revision {revision_id} references a vanished artifact family")
        version = await queries.lock_proposal_version(session, proposal_id, proposal_version)
        if version is None:  # pragma: no cover - read a moment ago under the same session
            raise RegistryError(f"proposal version {proposal_id}/{proposal_version} vanished under lock")
        await _lock_family(session, revision_id)
        qualification = None
        if qualification_id is not None:
            qualification = await queries.lock_qualification(session, qualification_id)

        risk_row = await rp_queries.load_risk_classification(session, proposal_id, proposal_version)

        digests: ReviewPackageDigests | None = None
        if version.semantics is not None and version.revision_id is not None:
            try:
                digests = await self._review_package.assemble(
                    session, proposal_id=proposal_id, proposal_version=proposal_version
                )
            except (ReviewPackageUnavailable, ReviewPackageIntegrityError):
                digests = None

        latest = await proposal_queries.load_latest_version(session, proposal_id)
        risk_result, stale_reducer_retired = predicates.check_risk_reproducible(version, risk_row)

        results: list[PredicateResult] = [
            predicates.check_latest_version(version, latest),
            predicates.check_state_approved(version),
            predicates.check_digest_chain(
                artifact_id=unlocked_identity.artifact_id,
                revision_id=revision_id,
                digests=digests,
                live_evidence=live_evidence,
            ),
            predicates.check_baseline_current(version, family),
            await predicates.check_source_valid(self._source_status, version),
            risk_result,
            predicates.check_observation_qualified(
                risk_row=risk_row,
                qualification=qualification,
                qualification_id=qualification_id,
                revision_id=revision_id,
                now=now,
            ),
            predicates.check_projection_evidence_valid(
                artifact_id=unlocked_identity.artifact_id,
                revision_id=revision_id,
                digests=digests,
                live_evidence=live_evidence,
                verifier=verifier,
                now=now,
                attestation_providers=self._attestation_providers,
                signature_verifier=self._signature_verifier,
            ),
            await predicates.check_actor_separation(
                session,
                version=version,
                risk_row=risk_row,
                qualification=qualification,
                revision_id=revision_id,
                activator_issuer=activator_issuer,
                activator_subject=activator_subject,
            ),
            predicates.check_operational_integrity(),
        ]

        ordered = tuple(sorted(results, key=lambda p: PREDICATE_ORDER.index(p.name)))
        eligible = all(p.satisfied for p in ordered)
        return _Evaluation(
            eligibility=ActivationEligibility(eligible=eligible, predicates=ordered),
            stale_reducer_retired=stale_reducer_retired,
            family=family,
        )


__all__ = [
    "ActivationEligibility",
    "ActivationError",
    "ActivationPredicateFailed",
    "ActivationRequestMismatch",
    "ActivationService",
    "RevisionActivation",
]
