"""`RevisionIntegrityService` -- the one chokepoint every read-path caller
that must trust a revision's approval state calls before doing anything on
the strength of it.

TDD names four eventual callers: `activation.py` (before activation),
`corpus.py` (mandatory corpus assembly), `selection.py` (new context
selection), `authorization.py` (protected-action authorization). None of
them calls this module yet. That is proven, not merely stated: `registry/
tests/unit/test_arc_integrity.py::
test_no_production_caller_references_revision_integrity_service_yet` scans
those four files and fails if any one of them so much as imports this
module. Wiring all four, together with enabling activation for real, is one
later, atomic task -- see that test's own docstring for why it must be
*replaced*, not deleted, the day that happens.

**Five independent verification axes, each removable and each proven
load-bearing.** `assess` checks, in order:

1. *Source status* -- the admitted source behind this revision's candidate
   is still `current`, unexpired, unrevoked, and fresh within the five-
   minute cap (`SourceStatusService.check_status`, reused, not reimplemented).
2. *Cached derived state* -- the sticky risk classification and the frozen
   expected-impact envelope's own digest still agree with what recomputing
   them from the authoritative rows behind them produces
   (`ReviewPackageService.assemble`, reused -- see that class's own module
   docstring for why persisted digest columns are caches, not truth). This
   axis also produces `S` and `R`, which the next axis needs.
3. *Projection evidence* -- the D2 `arc_projection_approval_evidence` row is
   live, its recomputed approval-target digest (`A`, built from the `S`/`R`
   axis 2 just produced) still matches what was signed, its verifier is not
   revoked, its snapshotted credential fingerprint still matches the
   verifier's current one, and (for a detached-signature completion, the
   one shape this deployment's evidence schema keeps enough material to
   re-derive) the signature itself still verifies against the verifier's
   *current* public key -- catching a tampered key that a stale fingerprint
   column would not.
4. *Operational chain* -- the revision's signed, hash-chained operational
   event history still verifies end to end (`OperationalChainService.
   verify_chain`, reused).
5. *Durable checkpoint* -- the chain's latest checkpoint has actually been
   exported to a sink and carries a receipt, not merely appended and still
   pending.

**Each axis is a call-then-guard pair, deliberately uniform.** Every axis
below is exactly: call a private `_check_*` helper that does that axis's
real work and returns `None` (satisfied) or a bounded `IntegrityAssessment`
(refused); then, inside a `# mutation-axis: <name>` / `# end-mutation-axis:
<name>` comment pair, `if <result> is not None: return <result>`. `registry/
tests/conformance/test_arc_integrity_mutation.py` mechanically replaces each
bracketed two-line guard with a single `pass` in a scratch copy of this
file's *actual bytes* and re-runs this module's own unit tests against it,
proving each axis's guard is what makes its own test fail, not decoration
around it -- see that file's own module docstring for the full mechanism
and why the guard (not the helper's body) is the mutated surface: the
helper still runs and still produces whatever data a later axis needs (`S`/
`R` from axis 2, for instance), so neutralizing one axis's guard cannot
starve a different axis of data it did not itself remove.

**Bounded result, deliberately.** `assess` returns `IntegrityAssessment
(valid, reason_code)` -- never evidence bytes, a verifier identity, or a
digest, on the success path or on any refusal path. The temptation to make
a refusal "more helpful" by naming which axis failed, which verifier, or
which digest disagreed is exactly the leak this class exists to not have:
a caller (or an attacker probing through one) learns only "trust this
revision right now, or don't." `_refused` below is the one place a
refusal is constructed, so an operator-facing detail (axis name, revision
id, purpose -- never a digest or an identity) lands in a structured log
line instead of in the returned object.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import logging
import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from registry.arc.service.approval_challenge import ReviewPackageDigests
from registry.arc.service.approval_challenge import ReviewPackageService as ReviewPackageAssembler
from registry.arc.service.approval_challenge_verification import (
    ApprovalVerificationFailed,
    DetachedSignatureProofInput,
    SignatureVerifier,
    VerifierAttestationProvider,
    VerifierMaterial,
    _ed25519_verify,
    build_canonical_evidence,
    verify_proof,
)
from registry.arc.service.operational_chain import OperationalChainIntegrityError
from registry.arc.service.queries import approval as approval_queries
from registry.arc.service.queries import operational_chain as operational_chain_queries
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.review_package import ReviewPackageIntegrityError, ReviewPackageUnavailable
from registry.arc.service.source_status import SourceStatusUnavailable
from registry.exceptions import NotFoundError, ValidationError
from registry.types import Clock

_log = logging.getLogger(__name__)

# The closed `purpose` vocabulary -- an audit/metrics tag only (it never
# branches the assessment logic below), but still a boundary input this
# method validates rather than logging or tagging a metric with an
# unbounded caller-supplied string.
PURPOSE_ACTIVATION = "activation"
PURPOSE_CORPUS_ASSEMBLY = "corpus_assembly"
PURPOSE_SELECTION = "selection"
PURPOSE_AUTHORIZATION = "authorization"
_PURPOSES = frozenset({PURPOSE_ACTIVATION, PURPOSE_CORPUS_ASSEMBLY, PURPOSE_SELECTION, PURPOSE_AUTHORIZATION})

# The closed set of values `IntegrityAssessment.reason_code` may ever carry.
# Reused from the wire contract's own closed refusal-code vocabulary
# (`registry/api/schemas/arc_authoring_enums.py`'s `RefusalCode`) rather
# than invented here -- every one of these already has a stated meaning and
# HTTP status a caller one layer up can map to. Transcribed as plain string
# literals rather than imported: `api/schemas/` depends on `arc/service/`,
# never the other way around, in this codebase (see that module's own
# `RevisionLifecycleState` docstring for the identical reasoning against
# the reverse import).
REASON_SOURCE_STATUS_UNAVAILABLE = "arc_source_status_unavailable"
REASON_PROJECTION_EVIDENCE_INVALID = "arc_approval_verification_failed"
REASON_OPERATIONAL_INTEGRITY_FAILED = "arc_operational_integrity_failed"
REASON_OPERATIONAL_INTEGRITY_PENDING = "arc_operational_integrity_pending"
REASON_PROPOSAL_STATE_CONFLICT = "arc_proposal_state_conflict"

_REASON_CODES = frozenset(
    {
        REASON_SOURCE_STATUS_UNAVAILABLE,
        REASON_PROJECTION_EVIDENCE_INVALID,
        REASON_OPERATIONAL_INTEGRITY_FAILED,
        REASON_OPERATIONAL_INTEGRITY_PENDING,
        REASON_PROPOSAL_STATE_CONFLICT,
    }
)


class SourceStatusChecker(Protocol):
    """The one capability this module needs from source-status storage --
    narrowed to `SourceStatusService.check_status` so a unit test can inject
    a trivial fake instead of the real, session-opening service.
    """

    async def check_status(self, source_evidence_id: uuid.UUID) -> object: ...


class ChainVerifier(Protocol):
    """The one capability this module needs from the operational chain --
    narrowed to `OperationalChainService.verify_chain` for the same reason
    as `SourceStatusChecker` above.
    """

    async def verify_chain(self, session: AsyncSession, revision_id: uuid.UUID) -> None: ...


@dataclasses.dataclass(frozen=True)
class IntegrityAssessment:
    """The only thing `assess` ever returns. Exactly two fields, both
    already present at the type level -- there is no third field this
    class could grow a caller into reading evidence bytes, a verifier
    identity, or a digest off of. `__post_init__` enforces the pairing
    (`valid=True` implies no code; `valid=False` implies a code from the
    closed set above) so a construction that would leak more than the
    contract allows fails immediately, at every call site, not only at the
    ones this module's own tests happen to cover.
    """

    valid: bool
    reason_code: str | None

    def __post_init__(self) -> None:
        if self.valid and self.reason_code is not None:
            msg = "a valid assessment carries no reason_code"
            raise ValueError(msg)
        if not self.valid and self.reason_code not in _REASON_CODES:
            msg = f"reason_code must be one of {sorted(_REASON_CODES)!r}, not {self.reason_code!r}"
            raise ValueError(msg)


def _refused(reason_code: str, *, revision_id: uuid.UUID, purpose: str, axis: str) -> IntegrityAssessment:
    """Every refusal path funnels through here so the operator-facing log
    line (axis name, revision id, purpose -- never a digest, a verifier id,
    or evidence bytes) cannot be forgotten on one path and present on
    another.  `axis` is for this log line only; it never reaches the
    returned object.
    """
    _log.info(
        "arc.integrity.refused: axis=%s revision_id=%s purpose=%s reason_code=%s",
        axis,
        revision_id,
        purpose,
        reason_code,
    )
    return IntegrityAssessment(valid=False, reason_code=reason_code)


class RevisionIntegrityService:
    """See the module docstring for the five axes `assess` checks and why
    each is independently provable rather than merely present.
    """

    def __init__(
        self,
        *,
        review_package_service: ReviewPackageAssembler,
        source_status_service: SourceStatusChecker,
        operational_chain_service: ChainVerifier,
        clock: Clock,
        attestation_providers: dict[str, VerifierAttestationProvider] | None = None,
        signature_verifier: SignatureVerifier | None = None,
    ) -> None:
        self._review_package_service = review_package_service
        self._source_status_service = source_status_service
        self._operational_chain_service = operational_chain_service
        self._clock = clock
        self._attestation_providers = dict(attestation_providers or {})
        self._signature_verifier = signature_verifier or _ed25519_verify

    async def assess(self, session: AsyncSession, revision_id: uuid.UUID, purpose: str) -> IntegrityAssessment:
        if purpose not in _PURPOSES:
            msg = f"purpose must be one of {sorted(_PURPOSES)!r}, not {purpose!r}"
            raise ValidationError(msg)

        identity = await proposal_queries.load_version_by_revision_id(session, revision_id)
        if identity is None or identity.revision_id is None:
            # Nothing to recompute S, R, or A from -- the digest chain
            # cannot even be established, which is itself an integrity
            # failure, not a "not found" a caller should be able to
            # distinguish from any other refusal.
            return _refused(
                REASON_OPERATIONAL_INTEGRITY_FAILED, revision_id=revision_id, purpose=purpose, axis="identity"
            )

        source_status_refusal = await self._check_source_status(
            identity.source_evidence_id, revision_id=revision_id, purpose=purpose
        )
        # mutation-axis: source_status
        if source_status_refusal is not None:
            return source_status_refusal
        # end-mutation-axis: source_status

        cached_state_result = await self._check_cached_state(
            session, identity, revision_id=revision_id, purpose=purpose
        )
        # mutation-axis: cached_state
        if isinstance(cached_state_result, IntegrityAssessment):
            return cached_state_result
        # end-mutation-axis: cached_state
        digests = cached_state_result

        projection_refusal = await self._check_projection_evidence(
            session, identity, revision_id, digests, purpose=purpose
        )
        # mutation-axis: projection_evidence
        if projection_refusal is not None:
            return projection_refusal
        # end-mutation-axis: projection_evidence

        chain_refusal = await self._check_operational_chain(session, revision_id, purpose=purpose)
        # mutation-axis: operational_chain
        if chain_refusal is not None:
            return chain_refusal
        # end-mutation-axis: operational_chain

        checkpoint_refusal = await self._check_durable_checkpoint(session, revision_id, purpose=purpose)
        # mutation-axis: durable_checkpoint
        if checkpoint_refusal is not None:
            return checkpoint_refusal
        # end-mutation-axis: durable_checkpoint

        return IntegrityAssessment(valid=True, reason_code=None)

    # -- axis 1: source status --------------------------------------------

    async def _check_source_status(
        self, source_evidence_id: uuid.UUID, *, revision_id: uuid.UUID, purpose: str
    ) -> IntegrityAssessment | None:
        try:
            await self._source_status_service.check_status(source_evidence_id)
        except (SourceStatusUnavailable, NotFoundError):
            return _refused(
                REASON_SOURCE_STATUS_UNAVAILABLE, revision_id=revision_id, purpose=purpose, axis="source_status"
            )
        return None

    # -- axis 2: cached derived state --------------------------------------

    async def _check_cached_state(
        self,
        session: AsyncSession,
        identity: proposal_queries.VersionRow,
        *,
        revision_id: uuid.UUID,
        purpose: str,
    ) -> IntegrityAssessment | ReviewPackageDigests:
        """Recomputes `S` and `R` and, inside that same call, cross-checks
        the sticky risk classification and the frozen envelope digest
        against what recomputing them from their own authoritative rows
        produces (`ReviewPackageService.assemble`'s own job -- see this
        module's docstring). Returns the digests themselves on success --
        deliberately a single-signal union, not a `(refusal, digests)` pair
        with a separate `digests is None` fallback in the caller: two
        independent signals for the same outcome is exactly what would let
        a mutation of one be silently compensated by the other, undermining
        the very mutation proof this axis exists to pass. The
        projection-evidence axis never has to call `assemble` a second time
        to get `S`/`R` for its own `A` recomputation.
        """
        try:
            return await self._review_package_service.assemble(
                session, proposal_id=identity.proposal_id, proposal_version=identity.proposal_version
            )
        except ReviewPackageIntegrityError:
            return _refused(
                REASON_OPERATIONAL_INTEGRITY_FAILED, revision_id=revision_id, purpose=purpose, axis="cached_state"
            )
        except ReviewPackageUnavailable:
            return _refused(
                REASON_PROPOSAL_STATE_CONFLICT, revision_id=revision_id, purpose=purpose, axis="cached_state"
            )

    # -- axis 3: projection evidence ---------------------------------------

    async def _check_projection_evidence(
        self,
        session: AsyncSession,
        identity: proposal_queries.VersionRow,
        revision_id: uuid.UUID,
        digests: ReviewPackageDigests,
        *,
        purpose: str,
    ) -> IntegrityAssessment | None:
        try:
            await self._assert_projection_evidence(session, identity, revision_id, digests)
        except ApprovalVerificationFailed:
            return _refused(
                REASON_PROJECTION_EVIDENCE_INVALID,
                revision_id=revision_id,
                purpose=purpose,
                axis="projection_evidence",
            )
        return None

    async def _assert_projection_evidence(
        self,
        session: AsyncSession,
        identity: proposal_queries.VersionRow,
        revision_id: uuid.UUID,
        digests: ReviewPackageDigests,
    ) -> None:
        """Raises `ApprovalVerificationFailed` on any disagreement -- never
        discloses which one, matching that exception's own stated
        collapsed-failure convention (no code discloses a cryptographic
        oracle signal).
        """
        evidence = await approval_queries.load_live_evidence_by_revision(session, revision_id)
        if evidence is None or evidence.revoked_at is not None:
            msg = "no live projection approval evidence for this revision"
            raise ApprovalVerificationFailed(msg)

        verifier = await approval_queries.load_verifier_for_share(session, evidence.approval_verifier_id)
        if verifier is None:
            msg = "the verifier this evidence names no longer exists"
            raise ApprovalVerificationFailed(msg)

        material = VerifierMaterial(
            approval_verifier_id=verifier.approval_verifier_id,
            allowed_evidence_types=frozenset(verifier.allowed_evidence_types),
            valid_from=verifier.valid_from,
            valid_to=verifier.valid_to,
            revoked_at=verifier.revoked_at,
            principal_binding_kind=verifier.principal_binding_kind,
            principal_issuer=verifier.principal_issuer,
            principal_subject=verifier.principal_subject,
            provider_id=verifier.provider_id,
            algorithm=verifier.algorithm,
            public_key=verifier.public_key,
            credential_fingerprint=verifier.credential_fingerprint,
        )
        now = self._clock.now()
        if not material.usable_at(now):
            msg = "the verifier is no longer usable"
            raise ApprovalVerificationFailed(msg)

        # Credential drift: the fingerprint snapshotted at approval time no
        # longer matches the verifier's current one -- a rotated or
        # re-enrolled credential since this evidence was accepted.
        if (
            verifier.credential_fingerprint is None
            or verifier.credential_fingerprint != evidence.credential_fingerprint_at_approval
        ):
            msg = "the verifier's current credential fingerprint disagrees with the one snapshotted at approval"
            raise ApprovalVerificationFailed(msg)

        canonical_evidence_bytes = build_canonical_evidence(
            artifact_id=identity.artifact_id,
            revision_id=revision_id,
            artifact_semantics_digest=digests.artifact_semantics_digest,
            review_package_digest=digests.review_package_digest,
        )
        recomputed_digest = hashlib.sha256(canonical_evidence_bytes).hexdigest()
        if recomputed_digest != evidence.approved_payload_digest:
            msg = "the recomputed approval-target digest disagrees with the one committed at completion"
            raise ApprovalVerificationFailed(msg)

        if evidence.verification_method == "detached_signature" and evidence.signature_algorithm is not None:
            # The one completion shape this deployment's evidence schema
            # keeps enough material to re-derive: `arc_projection_approval_
            # evidence` has no column for a provider attestation's own
            # `assertion_format`, so a `verifier_attestation` completion
            # cannot be replayed through `verify_proof` after the fact --
            # only detached-signature completions can. Re-verifying against
            # the verifier's *current* public key (not the one used at
            # completion time) is what catches a public key mutated in
            # place without its fingerprint column being updated to match,
            # a gap the fingerprint comparison above alone would miss.
            try:
                proof = DetachedSignatureProofInput(
                    signature_algorithm=evidence.signature_algorithm,
                    signature_base64=base64.b64encode(evidence.proof_bytes).decode("ascii"),
                )
            except (binascii.Error, ValueError) as exc:
                msg = "stored proof bytes could not be re-encoded for signature re-verification"
                raise ApprovalVerificationFailed(msg) from exc
            verify_proof(
                verifier=material,
                proof=proof,
                canonical_evidence_bytes=canonical_evidence_bytes,
                as_of=now,
                attestation_providers=self._attestation_providers,
                signature_verifier=self._signature_verifier,
            )

    # -- axis 4: operational chain ------------------------------------------

    async def _check_operational_chain(
        self, session: AsyncSession, revision_id: uuid.UUID, *, purpose: str
    ) -> IntegrityAssessment | None:
        try:
            await self._operational_chain_service.verify_chain(session, revision_id)
        except OperationalChainIntegrityError:
            return _refused(
                REASON_OPERATIONAL_INTEGRITY_FAILED,
                revision_id=revision_id,
                purpose=purpose,
                axis="operational_chain",
            )
        return None

    # -- axis 5: durable checkpoint ------------------------------------------

    async def _check_durable_checkpoint(
        self, session: AsyncSession, revision_id: uuid.UUID, *, purpose: str
    ) -> IntegrityAssessment | None:
        checkpoint = await operational_chain_queries.load_latest_checkpoint(session, revision_id)
        if (
            checkpoint is None
            or checkpoint.exported_at is None
            or checkpoint.sink_receipt_digest is None
            or checkpoint.sink_receipt_signature is None
        ):
            return _refused(
                REASON_OPERATIONAL_INTEGRITY_PENDING,
                revision_id=revision_id,
                purpose=purpose,
                axis="durable_checkpoint",
            )
        return None


__all__ = [
    "PURPOSE_ACTIVATION",
    "PURPOSE_AUTHORIZATION",
    "PURPOSE_CORPUS_ASSEMBLY",
    "PURPOSE_SELECTION",
    "REASON_OPERATIONAL_INTEGRITY_FAILED",
    "REASON_OPERATIONAL_INTEGRITY_PENDING",
    "REASON_PROJECTION_EVIDENCE_INVALID",
    "REASON_PROPOSAL_STATE_CONFLICT",
    "REASON_SOURCE_STATUS_UNAVAILABLE",
    "ChainVerifier",
    "IntegrityAssessment",
    "RevisionIntegrityService",
    "SourceStatusChecker",
]
