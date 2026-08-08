"""`ApprovalChallengeService` -- the D2 first-party, two-call `artifact_
activation` approval writer.

The first call (`create_challenge`) recomputes the acyclic approval-target
digest `A` from the freshly-assembled review package, commits it (and every
other bound field) into a challenge row, and hands the exact canonical bytes
back for the named verifier to sign or attest over. The second call
(`complete`) verifies that proof against the challenge's own committed
bytes -- never against anything the caller supplies -- and, on the first
valid completion, atomically writes `arc_projection_approval_evidence` and
compare-and-swaps the bound proposal version from `submitted` to `approved`.
There is no third call and no standalone approve route: `submitted ->
approved` is a side effect of this transaction succeeding, never a separate
human click that could be reached without verified evidence behind it.

**Split for cohesion, not line count.** `approval_challenge_verification.py`
holds every pure function this module needs -- canonical-bytes construction,
proof verification, idempotency digests -- none of which touches a session.
This module holds the stateful orchestration: locking, the compare-and-swap,
and the one especially privileged write (see below). An earlier attempt at
this service combined both into one 828-line file; the split mirrors the
same seam `artifact_integrity.py` draws against `artifact.py`.

**No longer dormant.** This class still takes a *required* `review_package_
service` constructor argument -- no default, no `None`-guard refusal, so
constructing it without one remains a `TypeError` -- but `wiring/services.py`
now injects the real `ReviewPackageService`, `wiring/container.py` names both
on the typed `Services` container, and `api/routers/arc_authoring.py`
registers the Appendix A.1 challenge-creation/completion routes. The two
structural tests that used to assert *no* reference anywhere
(`tests/unit/test_arc_approval_challenge.py::
test_no_production_wiring_references_approval_challenge_service` and its
sibling) now assert the opposite -- that the reference exists in exactly the
expected files, that this file is still the sole entry in `scripts/
check_arc_approval_writers.py`'s allowlist, and that no standalone `/approve`
route exists.

**Completion recomputes before it trusts.** A verified signature only proves
the verifier signed the bytes committed at issuance; it says nothing about
whether the rows those bytes were computed from still agree with themselves
now. `complete` therefore recomputes `S`, `R`, and `A` fresh from the
authoritative rows -- the same call `create_challenge` already makes -- and
refuses (`ApprovalVerificationFailed`, without consuming an attempt or
terminalizing the challenge) if the fresh `A` disagrees with the one
committed at issuance. See `review_package.py`'s own module docstring for
which persisted digest columns this recomputation refuses to trust.

**This module is the single legitimate `artifact_activation` writer.**
`scripts/check_arc_approval_writers.py` names exactly this file in its
allowlist. The one INSERT into `arc_projection_approval_evidence` -- the row
every future activation predicate 8 revalidates -- is written directly here
rather than through the `queries` sibling; see that module's own docstring
for why this is the one deliberate exception to the "queries owns the SQL"
convention this package otherwise follows throughout.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import hashlib
import uuid
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service import audit_outbox
from contextplane.arc.service.approval_challenge_verification import (
    CHALLENGE_TTL,
    MAX_ATTEMPTS,
    SIGNING_DOMAIN_LABEL,
    ApprovalChallengeError,
    ApprovalVerificationFailed,
    DetachedSignatureProofInput,
    ProofInput,
    SignatureVerifier,
    VerifierAttestationProvider,
    VerifierMaterial,
    _ed25519_verify,
    build_canonical_evidence,
    idempotency_scope_digest,
    request_payload_digest,
    verify_proof,
)
from contextplane.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from contextplane.arc.service.proposal import ProposalStateConflict
from contextplane.arc.service.queries import approval as queries
from contextplane.arc.service.queries import proposal as proposal_queries
from contextplane.arc.types import ArcRequestContext, AuthorityScope
from contextplane.audit import actions
from contextplane.exceptions import NotFoundError, RegistryError
from contextplane.types import Clock

# D2's stated cardinality cap: at most ten live challenges per proposal
# version, and ten per actor, in addition to the authenticated write-rate
# limit every route already carries. A rejected over-limit request
# allocates no nonce.
MAX_LIVE_CHALLENGES_PER_VERSION = 10
MAX_LIVE_CHALLENGES_PER_ACTOR = 10


class ApprovalChallengeLimitReached(ApprovalChallengeError):
    """Over ten live challenges for this proposal version, or for this
    actor (`arc_approval_challenge_limit_reached`, 429)."""


class ApprovalChallengeExpired(ApprovalChallengeError):
    """`now >= expires_at` (`arc_approval_challenge_expired`, 409)."""


class ApprovalChallengeFailedTerminal(ApprovalChallengeError):
    """Three invalid signature attempts already terminalized this challenge
    (`arc_approval_challenge_failed`, 409)."""


class ApprovalChallengeSuperseded(ApprovalChallengeError):
    """A different challenge already won the `submitted -> approved`
    compare-and-swap for this proposal version. Carries no winner evidence
    (`arc_approval_challenge_superseded`, 409)."""


class ApprovalAlreadyCompleted(ApprovalChallengeError):
    """This challenge already completed under a different actor than the
    one asking now (`arc_approval_already_completed`, 409)."""


class ApprovalIdempotencyConflict(ApprovalChallengeError):
    """Same idempotency scope, changed request payload
    (`arc_idempotency_conflict`, 409)."""


@dataclasses.dataclass(frozen=True)
class ReviewPackageDigests:
    """`S` and `R` -- what `ApprovalChallengeService` needs from the review
    package to compute `A` itself. Deliberately not `A` as well: `A` needs
    only the target identity (`artifact_id`, `revision_id`), which this
    service already has from the proposal version's own bijection, so
    computing it here (via `build_canonical_evidence`) keeps `S, R -> A`
    from being computed twice by two different callers that could drift.
    """

    artifact_semantics_digest: str
    review_package_digest: str


class ReviewPackageService(Protocol):
    """The one collaborator this service requires. The concrete
    implementation in `review_package.py` reads field provenance, semantic
    tests, the sticky risk result, the expected-impact envelope, the
    baseline diff, and submission identity to compute `R`; this protocol
    only needs the two digests that computation produces, so this module
    never has to know how.
    """

    async def assemble(
        self, session: AsyncSession, *, proposal_id: uuid.UUID, proposal_version: int
    ) -> ReviewPackageDigests: ...


@dataclasses.dataclass(frozen=True)
class IssuedApprovalChallenge:
    """What `create_challenge` hands back -- the exact bytes to sign (or
    attest over), never the proposal's own review content."""

    approval_challenge_id: uuid.UUID
    canonical_evidence_bytes: bytes
    signing_domain: str
    approval_nonce: str
    expires_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class ProjectionApprovalEvidence:
    """What a won completion hands back. Never carries `proof_bytes` --
    Appendix B.1's own stated rule for this column, "never returned by any
    route" -- so there is no field here for a router to accidentally expose.
    """

    evidence_id: uuid.UUID
    approval_challenge_id: uuid.UUID
    proposal_id: uuid.UUID
    proposal_version: int
    revision_id: uuid.UUID
    approved_payload_digest: str
    approval_verifier_id: str
    approving_principal_issuer: str
    approving_principal_subject: str
    credential_fingerprint_at_approval: str
    verification_method: str
    verified_at: datetime.datetime
    revoked_at: datetime.datetime | None = None


def _scope(tenant_id: uuid.UUID | None) -> ArtifactScope:
    """Duplicated from `proposal.py`'s own private `_scope` rather than
    imported -- matching every service module in this package's own stated
    convention (`provenance.py`, `semantic_tests.py`, `submission.py` each
    build this same three-line mapping themselves)."""
    scope = AuthorityScope.GLOBAL if tenant_id is None else AuthorityScope.TENANT
    return ArtifactScope(scope=scope, tenant_id=tenant_id)


@dataclasses.dataclass(frozen=True)
class _CompletionOutcome:
    """What `_complete_locked` hands back to `complete` -- a result instead
    of a raised exception, precisely so `complete` can let the transaction
    commit cleanly before deciding whether to raise. Exactly one field is
    set; see `complete`'s own docstring for why this indirection exists.
    """

    evidence: ProjectionApprovalEvidence | None = None
    error: Exception | None = None


def _proof_bytes(proof: ProofInput) -> bytes:
    """The bytes `arc_projection_approval_evidence.proof_bytes` stores --
    never returned by any route (Appendix B.1). For a detached signature
    this is the exact signature bytes verified above; for a provider
    attestation it is the presented `assertion_base64` string's own UTF-8
    bytes, because the assertion's *decoded* form is provider-specific and
    opaque to this module -- storing the text a provider was actually
    handed is more useful for a later audit than a decode this module has
    no basis for interpreting.
    """
    if isinstance(proof, DetachedSignatureProofInput):
        return base64.b64decode(proof.signature_base64, validate=True)
    return proof.assertion_base64.encode("utf-8")


class ApprovalChallengeService:
    """Issues D2 approval challenges and completes them into projection
    evidence. See the module docstring for why this class is dormant on
    every deployment today.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
        review_package_service: ReviewPackageService,
        attestation_providers: dict[str, VerifierAttestationProvider] | None = None,
        signature_verifier: SignatureVerifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock
        self._review_package_service = review_package_service
        self._attestation_providers = dict(attestation_providers or {})
        self._signature_verifier = signature_verifier or _ed25519_verify

    # -- issuance -----------------------------------------------------------

    async def create_challenge(
        self,
        ctx: ArcRequestContext,
        proposal_id: uuid.UUID,
        proposal_version: int,
        *,
        approval_verifier_id: str,
        idempotency_key: str,
    ) -> IssuedApprovalChallenge:
        """Mint a five-minute, single-use approval challenge for one
        `submitted` proposal version and verifier -- or return the stored
        result of an exact retry, even past that challenge's own expiry.
        """
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                msg = f"proposal version {proposal_id}/{proposal_version} not found"
                raise NotFoundError(msg)
            self._authorization.assert_can_write_artifact(ctx, _scope(version.tenant_id))

            scope_digest = idempotency_scope_digest(
                issuer=ctx.oidc_issuer,
                subject=ctx.oidc_subject,
                proposal_id=proposal_id,
                proposal_version=proposal_version,
                idempotency_key=idempotency_key,
            )
            payload_digest = request_payload_digest(approval_verifier_id=approval_verifier_id)

            existing = await queries.find_challenge_by_scope_digest(session, scope_digest)
            if existing is not None:
                if existing.request_payload_digest != payload_digest:
                    msg = "idempotency key already identifies a different approval-challenge request"
                    raise ApprovalIdempotencyConflict(msg)
                return _issued(existing)

            if version.state != "submitted":
                msg = f"proposal version {proposal_id}/{proposal_version} is not submitted"
                raise ProposalStateConflict(msg)
            if version.revision_id is None:
                # Unreachable once `state == "submitted"` (materialisation
                # sets both together), kept so this fails loudly rather than
                # canonicalizing a `None` target identity.
                msg = f"proposal version {proposal_id}/{proposal_version} has no bound revision"
                raise RegistryError(msg)

            verifier = await queries.load_verifier_for_share(session, approval_verifier_id)
            if verifier is None:
                msg = f"no approval verifier {approval_verifier_id!r}"
                raise ApprovalVerificationFailed(msg)
            if "artifact_activation" not in verifier.allowed_evidence_types:
                msg = f"verifier {approval_verifier_id!r} is not permitted for artifact_activation evidence"
                raise ApprovalVerificationFailed(msg)
            if verifier.revoked_at is not None and now >= verifier.revoked_at:
                msg = f"verifier {approval_verifier_id!r} is revoked"
                raise ApprovalVerificationFailed(msg)

            live_for_version = await queries.count_live_challenges(
                session, proposal_id=proposal_id, proposal_version=proposal_version, now=now
            )
            if live_for_version >= MAX_LIVE_CHALLENGES_PER_VERSION:
                msg = (
                    f"proposal version {proposal_id}/{proposal_version} already has "
                    f"{live_for_version} live challenges"
                )
                raise ApprovalChallengeLimitReached(msg)
            live_for_actor = await queries.count_live_challenges_for_actor(
                session, requested_by_issuer=ctx.oidc_issuer, requested_by_subject=ctx.oidc_subject, now=now
            )
            if live_for_actor >= MAX_LIVE_CHALLENGES_PER_ACTOR:
                msg = f"actor {ctx.oidc_issuer}/{ctx.oidc_subject} already has {live_for_actor} live challenges"
                raise ApprovalChallengeLimitReached(msg)

            digests = await self._review_package_service.assemble(
                session, proposal_id=proposal_id, proposal_version=proposal_version
            )
            canonical_evidence_bytes = build_canonical_evidence(
                artifact_id=version.artifact_id,
                revision_id=version.revision_id,
                artifact_semantics_digest=digests.artifact_semantics_digest,
                review_package_digest=digests.review_package_digest,
            )
            approved_payload_digest = hashlib.sha256(canonical_evidence_bytes).hexdigest()

            approval_challenge_id = uuid.uuid4()
            nonce = uuid.uuid4().hex
            expires_at = now + CHALLENGE_TTL

            await queries.insert_challenge(
                session,
                approval_challenge_id=approval_challenge_id,
                proposal_id=proposal_id,
                proposal_version=proposal_version,
                artifact_id=version.artifact_id,
                revision_id=version.revision_id,
                approval_verifier_id=approval_verifier_id,
                nonce=nonce,
                canonical_evidence_bytes=canonical_evidence_bytes,
                signing_domain=SIGNING_DOMAIN_LABEL,
                approved_payload_digest=approved_payload_digest,
                idempotency_scope_digest=scope_digest,
                request_payload_digest=payload_digest,
                requested_by_issuer=ctx.oidc_issuer,
                requested_by_subject=ctx.oidc_subject,
                issued_at=now,
                expires_at=expires_at,
            )
            await self._emit(
                session,
                tenant_id=version.tenant_id,
                event_type=actions.ARC_APPROVAL_CHALLENGE_ISSUED,
                payload={
                    "approval_challenge_id": str(approval_challenge_id),
                    "proposal_id": str(proposal_id),
                    "proposal_version": proposal_version,
                    "approval_verifier_id": approval_verifier_id,
                    "requested_by_issuer": ctx.oidc_issuer,
                    "requested_by_subject": ctx.oidc_subject,
                },
            )

        return IssuedApprovalChallenge(
            approval_challenge_id=approval_challenge_id,
            canonical_evidence_bytes=canonical_evidence_bytes,
            signing_domain=SIGNING_DOMAIN_LABEL,
            approval_nonce=nonce,
            expires_at=expires_at,
        )

    # -- completion -----------------------------------------------------------

    async def complete(
        self, ctx: ArcRequestContext, approval_challenge_id: uuid.UUID, *, proof: ProofInput
    ) -> ProjectionApprovalEvidence:
        """Verify *proof* against the named challenge's committed bytes and,
        on the first valid completion, atomically write evidence and
        compare-and-swap the proposal version to `approved`.

        One transaction, the challenge locked `FOR UPDATE` for its whole
        duration: a second completion attempt for the same challenge blocks
        until this one commits or rolls back, then re-reads state and loses.

        Every refusal that itself writes something durable -- the attempt
        counter, an expiry or supersession transition -- has to commit that
        write even though the *call* ultimately fails. Raising directly
        inside `session.begin()`'s own context manager would roll the whole
        transaction back, silently discarding exactly the write the refusal
        depends on (an earlier version of this method did precisely that,
        and the attempt ceiling never advanced past zero). `_complete_locked`
        below returns its result instead of raising, so this method can let
        the transaction commit cleanly first and only then raise.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            outcome = await self._complete_locked(session, ctx, approval_challenge_id, proof=proof, now=now)
        if outcome.error is not None:
            raise outcome.error
        if outcome.evidence is None:
            # Unreachable: `_complete_locked` always sets exactly one of the
            # two fields. Kept fail-closed rather than returning `None`.
            msg = f"approval challenge {approval_challenge_id} completed with neither evidence nor an error"
            raise RegistryError(msg)
        return outcome.evidence

    async def _complete_locked(
        self,
        session: AsyncSession,
        ctx: ArcRequestContext,
        approval_challenge_id: uuid.UUID,
        *,
        proof: ProofInput,
        now: datetime.datetime,
    ) -> _CompletionOutcome:
        challenge = await queries.lock_challenge(session, approval_challenge_id)
        if challenge is None:
            return _CompletionOutcome(error=NotFoundError(f"no approval challenge {approval_challenge_id}"))

        actor = (ctx.oidc_issuer, ctx.oidc_subject)
        requester = (challenge.requested_by_issuer, challenge.requested_by_subject)

        if challenge.state == "completed":
            if actor != requester:
                # Never disclose a winner's evidence to a different actor
                # than the one who created the challenge.
                msg = f"approval challenge {approval_challenge_id} was already completed"
                return _CompletionOutcome(error=ApprovalAlreadyCompleted(msg))
            evidence = await self._load_evidence(session, approval_challenge_id)
            if evidence is None:
                msg = f"challenge {approval_challenge_id} is marked completed but has no evidence row"
                return _CompletionOutcome(error=RegistryError(msg))
            return _CompletionOutcome(evidence=evidence)
        if challenge.state == "failed":
            msg = f"approval challenge {approval_challenge_id} failed after {MAX_ATTEMPTS} invalid attempts"
            return _CompletionOutcome(error=ApprovalChallengeFailedTerminal(msg))
        if challenge.state == "superseded":
            msg = f"approval challenge {approval_challenge_id} was superseded by another challenge's completion"
            return _CompletionOutcome(error=ApprovalChallengeSuperseded(msg))
        if challenge.state == "expired" or now >= challenge.expires_at:
            if challenge.state == "issued":
                await queries.mark_terminal(session, approval_challenge_id, state="expired", now=now)
            msg = f"approval challenge {approval_challenge_id} expired at {challenge.expires_at.isoformat()}"
            return _CompletionOutcome(error=ApprovalChallengeExpired(msg))

        if actor != requester:
            msg = "the completing actor must be the same authenticated actor that created this challenge"
            return _CompletionOutcome(error=ApprovalVerificationFailed(msg))

        verifier_row = await queries.load_verifier_for_share(session, challenge.approval_verifier_id)
        if verifier_row is None:
            msg = f"verifier {challenge.approval_verifier_id!r} vanished after the challenge naming it was issued"
            return _CompletionOutcome(error=ApprovalVerificationFailed(msg))
        verifier = VerifierMaterial(
            approval_verifier_id=verifier_row.approval_verifier_id,
            allowed_evidence_types=frozenset(verifier_row.allowed_evidence_types),
            valid_from=verifier_row.valid_from,
            valid_to=verifier_row.valid_to,
            revoked_at=verifier_row.revoked_at,
            principal_binding_kind=verifier_row.principal_binding_kind,
            principal_issuer=verifier_row.principal_issuer,
            principal_subject=verifier_row.principal_subject,
            provider_id=verifier_row.provider_id,
            algorithm=verifier_row.algorithm,
            public_key=verifier_row.public_key,
            credential_fingerprint=verifier_row.credential_fingerprint,
        )

        try:
            verified = verify_proof(
                verifier=verifier,
                proof=proof,
                canonical_evidence_bytes=challenge.canonical_evidence_bytes,
                as_of=now,
                attestation_providers=self._attestation_providers,
                signature_verifier=self._signature_verifier,
            )
        except ApprovalVerificationFailed as exc:
            new_count = challenge.attempt_count + 1
            await queries.record_failed_attempt(session, approval_challenge_id, new_attempt_count=new_count, now=now)
            if new_count >= MAX_ATTEMPTS:
                await self._emit(
                    session,
                    tenant_id=None,
                    event_type=actions.ARC_APPROVAL_CHALLENGE_FAILED,
                    payload={
                        "approval_challenge_id": str(approval_challenge_id),
                        "proposal_id": str(challenge.proposal_id),
                        "proposal_version": challenge.proposal_version,
                        "attempt_count": new_count,
                    },
                )
                msg = f"approval challenge {approval_challenge_id} failed after {new_count} invalid attempts"
                return _CompletionOutcome(error=ApprovalChallengeFailedTerminal(msg))
            return _CompletionOutcome(error=exc)

        # Recompute S, R, A fresh from the authoritative rows behind this
        # challenge and compare against what was actually committed (and
        # signed) at issuance -- never trust `challenge.approved_payload_
        # digest` as truth just because the signature over `challenge.
        # canonical_evidence_bytes` verified. That signature only proves the
        # verifier signed *those bytes*; it says nothing about whether the
        # rows those bytes were computed from still agree with themselves
        # now. A disagreement here means something feeding S or R (the
        # candidate semantics, a field-provenance row, the sticky risk
        # result, the frozen envelope) changed after issuance -- corruption
        # or tampering, since every one of those rows is supposed to be
        # frozen by the time a proposal reaches `submitted`. Neither the
        # attempt counter nor the challenge's state changes on this path:
        # this is not evidence of a forged signature, so it must not consume
        # an attempt or terminalize a challenge a legitimate retry could
        # still resolve once the underlying drift is fixed.
        digests = await self._review_package_service.assemble(
            session, proposal_id=challenge.proposal_id, proposal_version=challenge.proposal_version
        )
        recomputed_evidence_bytes = build_canonical_evidence(
            artifact_id=challenge.artifact_id,
            revision_id=challenge.revision_id,
            artifact_semantics_digest=digests.artifact_semantics_digest,
            review_package_digest=digests.review_package_digest,
        )
        recomputed_payload_digest = hashlib.sha256(recomputed_evidence_bytes).hexdigest()
        if recomputed_payload_digest != challenge.approved_payload_digest:
            msg = (
                f"approval challenge {approval_challenge_id} recomputed a review-package digest that "
                "disagrees with the one committed at issuance -- an authoritative row this challenge's "
                "digest chain depends on has drifted since it was issued"
            )
            return _CompletionOutcome(error=ApprovalVerificationFailed(msg))

        approved = await queries.cas_submitted_to_approved(
            session, proposal_id=challenge.proposal_id, proposal_version=challenge.proposal_version
        )
        if approved is None:
            await queries.mark_terminal(session, approval_challenge_id, state="superseded", now=now)
            msg = f"approval challenge {approval_challenge_id} was superseded by another challenge's completion"
            return _CompletionOutcome(error=ApprovalChallengeSuperseded(msg))

        evidence_id = uuid.uuid4()
        if isinstance(proof, DetachedSignatureProofInput):
            verification_method = "detached_signature"
            signature_algorithm: str | None = proof.signature_algorithm
        else:
            verification_method = "verifier_attestation"
            signature_algorithm = None
        try:
            proof_bytes = _proof_bytes(proof)
        except (binascii.Error, ValueError) as exc:
            # Unreachable for a detached signature (already decoded once
            # inside `verify_proof`); kept fail-closed rather than storing
            # an un-decodable value if that ever changes. Nothing has been
            # written yet in this branch, so there is no poisoned-
            # transaction concern -- the caller's rollback is harmless.
            msg = "proof bytes could not be decoded for storage"
            raise ApprovalVerificationFailed(msg) from exc

        try:
            await session.execute(
                text(
                    "INSERT INTO arc_projection_approval_evidence ("
                    "  evidence_id, approval_challenge_id, proposal_id, proposal_version, revision_id,"
                    "  approved_payload_digest, approval_verifier_id, approving_principal_issuer,"
                    "  approving_principal_subject, credential_fingerprint_at_approval, verification_method,"
                    "  signature_algorithm, proof_bytes, signing_domain, verified_at"
                    ") VALUES ("
                    "  :evidence_id, :approval_challenge_id, :proposal_id, :proposal_version, :revision_id,"
                    "  :approved_payload_digest, :approval_verifier_id, :approving_principal_issuer,"
                    "  :approving_principal_subject, :credential_fingerprint_at_approval, :verification_method,"
                    "  :signature_algorithm, :proof_bytes, :signing_domain, :verified_at"
                    ")"
                ),
                {
                    "evidence_id": evidence_id,
                    "approval_challenge_id": approval_challenge_id,
                    "proposal_id": challenge.proposal_id,
                    "proposal_version": challenge.proposal_version,
                    "revision_id": approved.revision_id,
                    "approved_payload_digest": challenge.approved_payload_digest,
                    "approval_verifier_id": challenge.approval_verifier_id,
                    "approving_principal_issuer": verified.approving_principal_issuer,
                    "approving_principal_subject": verified.approving_principal_subject,
                    "credential_fingerprint_at_approval": verified.credential_fingerprint,
                    "verification_method": verification_method,
                    "signature_algorithm": signature_algorithm,
                    "proof_bytes": proof_bytes,
                    "signing_domain": challenge.signing_domain,
                    "verified_at": now,
                },
            )
        except IntegrityError:
            # Unreachable in practice: `cas_submitted_to_approved`'s row
            # lock on the proposal version already serializes every
            # concurrent completion for this version, so only one
            # transaction ever reaches this statement. No further write is
            # attempted here on purpose -- a failed INSERT already poisons
            # the rest of this Postgres transaction, so a same-transaction
            # `mark_terminal` would itself fail; the caller's rollback
            # leaves the challenge `issued`, which a fresh completion
            # attempt can still resolve correctly.
            msg = f"approval challenge {approval_challenge_id} lost a race writing projection evidence"
            return _CompletionOutcome(error=ApprovalChallengeSuperseded(msg))

        await queries.mark_completed(session, approval_challenge_id)
        await self._emit(
            session,
            tenant_id=None,
            event_type=actions.ARC_PROJECTION_APPROVAL_EVIDENCE_RECORDED,
            payload={
                "evidence_id": str(evidence_id),
                "approval_challenge_id": str(approval_challenge_id),
                "proposal_id": str(challenge.proposal_id),
                "proposal_version": challenge.proposal_version,
                "revision_id": str(approved.revision_id),
                "approval_verifier_id": challenge.approval_verifier_id,
                "approving_principal_issuer": verified.approving_principal_issuer,
                "approving_principal_subject": verified.approving_principal_subject,
            },
        )
        return _CompletionOutcome(
            evidence=ProjectionApprovalEvidence(
                evidence_id=evidence_id,
                approval_challenge_id=approval_challenge_id,
                proposal_id=challenge.proposal_id,
                proposal_version=challenge.proposal_version,
                revision_id=approved.revision_id,
                approved_payload_digest=challenge.approved_payload_digest,
                approval_verifier_id=challenge.approval_verifier_id,
                approving_principal_issuer=verified.approving_principal_issuer,
                approving_principal_subject=verified.approving_principal_subject,
                credential_fingerprint_at_approval=verified.credential_fingerprint,
                verification_method=verification_method,
                verified_at=now,
            )
        )

    # -- reads ----------------------------------------------------------------

    async def get_evidence(self, proposal_id: uuid.UUID, proposal_version: int) -> ProjectionApprovalEvidence | None:
        """The one live (`revoked_at IS NULL`) evidence row for this
        proposal version, if any -- a plain, unlocked read."""
        async with self._session_factory() as session:
            return await self._load_evidence_by_version(
                session, proposal_id=proposal_id, proposal_version=proposal_version
            )

    async def _load_evidence(
        self, session: AsyncSession, approval_challenge_id: uuid.UUID
    ) -> ProjectionApprovalEvidence | None:
        row = (
            await session.execute(
                text(
                    "SELECT evidence_id, approval_challenge_id, proposal_id, proposal_version, revision_id,"
                    "       approved_payload_digest, approval_verifier_id, approving_principal_issuer,"
                    "       approving_principal_subject, credential_fingerprint_at_approval, verification_method,"
                    "       verified_at, revoked_at "
                    "FROM arc_projection_approval_evidence WHERE approval_challenge_id = :cid"
                ),
                {"cid": approval_challenge_id},
            )
        ).one_or_none()
        return None if row is None else _evidence_result(row)

    async def _load_evidence_by_version(
        self, session: AsyncSession, *, proposal_id: uuid.UUID, proposal_version: int
    ) -> ProjectionApprovalEvidence | None:
        row = (
            await session.execute(
                text(
                    "SELECT evidence_id, approval_challenge_id, proposal_id, proposal_version, revision_id,"
                    "       approved_payload_digest, approval_verifier_id, approving_principal_issuer,"
                    "       approving_principal_subject, credential_fingerprint_at_approval, verification_method,"
                    "       verified_at, revoked_at "
                    "FROM arc_projection_approval_evidence "
                    "WHERE proposal_id = :pid AND proposal_version = :pv AND revoked_at IS NULL"
                ),
                {"pid": proposal_id, "pv": proposal_version},
            )
        ).one_or_none()
        return None if row is None else _evidence_result(row)

    async def _emit(
        self, session: AsyncSession, *, tenant_id: uuid.UUID | None, event_type: str, payload: dict[str, object]
    ) -> None:
        """Tenant-scoped when the proposal names a tenant, global otherwise
        -- matching every other ARC audit emit site's own branch on scope.
        """
        if tenant_id is None:
            await audit_outbox.emit_global(session, event_type=event_type, payload=payload)
        else:
            await audit_outbox.emit(session, tenant_id=tenant_id, event_type=event_type, payload=payload)


def _issued(existing: queries.ChallengeRow) -> IssuedApprovalChallenge:
    return IssuedApprovalChallenge(
        approval_challenge_id=existing.approval_challenge_id,
        canonical_evidence_bytes=existing.canonical_evidence_bytes,
        signing_domain=existing.signing_domain,
        approval_nonce=existing.nonce,
        expires_at=existing.expires_at,
    )


def _evidence_result(row: object) -> ProjectionApprovalEvidence:
    return ProjectionApprovalEvidence(
        evidence_id=row.evidence_id,  # type: ignore[attr-defined]
        approval_challenge_id=row.approval_challenge_id,  # type: ignore[attr-defined]
        proposal_id=row.proposal_id,  # type: ignore[attr-defined]
        proposal_version=row.proposal_version,  # type: ignore[attr-defined]
        revision_id=row.revision_id,  # type: ignore[attr-defined]
        approved_payload_digest=row.approved_payload_digest,  # type: ignore[attr-defined]
        approval_verifier_id=row.approval_verifier_id,  # type: ignore[attr-defined]
        approving_principal_issuer=row.approving_principal_issuer,  # type: ignore[attr-defined]
        approving_principal_subject=row.approving_principal_subject,  # type: ignore[attr-defined]
        credential_fingerprint_at_approval=row.credential_fingerprint_at_approval,  # type: ignore[attr-defined]
        verification_method=row.verification_method,  # type: ignore[attr-defined]
        verified_at=row.verified_at,  # type: ignore[attr-defined]
        revoked_at=row.revoked_at,  # type: ignore[attr-defined]
    )


__all__ = [
    "MAX_LIVE_CHALLENGES_PER_ACTOR",
    "MAX_LIVE_CHALLENGES_PER_VERSION",
    "ApprovalAlreadyCompleted",
    "ApprovalChallengeExpired",
    "ApprovalChallengeFailedTerminal",
    "ApprovalChallengeLimitReached",
    "ApprovalChallengeService",
    "ApprovalChallengeSuperseded",
    "ApprovalIdempotencyConflict",
    "IssuedApprovalChallenge",
    "ProjectionApprovalEvidence",
    "ReviewPackageDigests",
    "ReviewPackageService",
]
