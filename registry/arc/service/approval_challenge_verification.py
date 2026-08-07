"""Pure helpers for the D2 projection-approval challenge/writer protocol:
canonical evidence bytes, proof-of-possession verification, and idempotency
digests. No session, no service state, no I/O -- everything here is a
function of its arguments alone, which is what makes it independently unit
testable and safe to ground-truth against the AAS-T01 fixtures without a
database.

Sibling of `approval_challenge.py`, mirroring the split `artifact_
integrity.py` draws against `artifact.py`/`artifact_materialisation.py`: the
canonicalization and verification logic lives here, the stateful, session-
opening orchestration lives in the service module. Combining both into one
file is what pushed an earlier attempt at this module over the repo-wide
800-line ceiling; this split is deliberate, not incidental.

**Reuses the existing canonicalizer.** `A`'s preimage is exactly the
`arc_artifact_revision_v1` profile object -- `artifact_id`, `revision_id`,
`S` (artifact semantics digest), `R` (review package digest), and the fixed
`actor_separation_profile` discriminator -- so `build_canonical_evidence`
below calls `registry.arc.schemas.authoring_profiles.
canonicalize_artifact_revision_v1` directly rather than re-implementing
canonicalization. `test_canonical_bytes_match_the_authoritative_fixture` in
this module's test file ground-truths that call against the checked-in
`artifact_revision_v1` conformance vector, the same proof `AAS-T13` applied
to `arc_approval_verifier_enrollment_v1`.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import hashlib
import uuid
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from registry.arc.schemas import authoring_profiles
from registry.arc.schemas.authoring_profile_shapes import ACTOR_SEPARATION_PROFILE, ARTIFACT_REVISION_PROFILE
from registry.arc.schemas.canonical import CanonicalizationError
from registry.exceptions import RegistryError

# Domain separation over the canonical evidence bytes, distinct from every
# other ARC signing profile's own tag (including the legacy, pre-D2
# `arc_approval_evidence_v1` domain `approval.py` already defines) -- a
# signature produced for a different profile or protocol must never verify
# here even if the canonical bytes happened to coincide. `arc_artifact_
# revision_v1` is not one of AAS-T01's five pre-registered *signed* profile
# fixtures (it carries no `signing_domain` in the manifest), so this
# protocol mints its own rather than guessing at a reserved one.
_SIGNING_DOMAIN = b"ARC-PROJECTION-APPROVAL-EVIDENCE-V1\x00"

#: The human-readable domain label `ApprovalChallengeResponse.signing_domain`
#: (once `AAS-T15` registers the route) would report.
SIGNING_DOMAIN_LABEL = "arc.projection_approval_evidence.v1"

# D2: a five-minute, single-use challenge, matching D1 enrollment's own TTL.
CHALLENGE_TTL = datetime.timedelta(minutes=5)

# The third invalid signature attempt terminalizes the challenge (Appendix
# B.2's stated enforcement for this rule); the migration's own
# `CHECK attempt_count <= 3` is the DDL half of the same rule.
MAX_ATTEMPTS = 3

_ED25519_PUBLIC_KEY_BYTES = 32

# Only a verifier permitted for this evidence type may complete a
# projection-approval challenge -- the same permitted-types closure
# `VerifierRegistry` already enforces for the legacy evidence table.
ARTIFACT_ACTIVATION_EVIDENCE_TYPE = "artifact_activation"


class ApprovalChallengeError(RegistryError):
    """Base of every refusal this module and its stateful sibling raise."""


class ApprovalVerificationFailed(ApprovalChallengeError):
    """The presented proof does not verify against this challenge's
    committed canonical bytes.

    Covers a shape mismatch (a `VerifierAttestationProof` presented against
    an `exact_principal` verifier or vice versa), a wrong signature
    algorithm, a bad signature, an unconfigured or refusing provider, a
    revoked or out-of-window verifier, and a verifier not permitted for
    `artifact_activation` evidence -- all collapse to this one outward
    failure (`arc_approval_verification_failed`, 400) so a caller cannot
    distinguish "wrong key" from "unknown verifier" from any other
    cryptographic detail (Appendix A.5: no code discloses a cryptographic
    oracle signal).
    """


@dataclasses.dataclass(frozen=True)
class DetachedSignatureProofInput:
    """Adapted from the wire `DetachedSignatureProof`."""

    signature_algorithm: str
    signature_base64: str


@dataclasses.dataclass(frozen=True)
class AttestationProofInput:
    """Adapted from the wire `VerifierAttestationProof`."""

    provider_id: str
    assertion_format: str
    assertion_base64: str


ProofInput = DetachedSignatureProofInput | AttestationProofInput


@dataclasses.dataclass(frozen=True)
class VerifierMaterial:
    """The exact subset of one `arc_approval_verifiers` row this module's
    verification needs -- signing material and D1 principal binding, but
    none of the row's own persistence concerns (challenge linkage,
    enrollment timestamps). Built by the stateful service from its own
    locked read; kept separate from that row type so this module stays
    importable without the `queries` package.
    """

    approval_verifier_id: str
    allowed_evidence_types: frozenset[str]
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    revoked_at: datetime.datetime | None
    principal_binding_kind: str | None
    principal_issuer: str | None
    principal_subject: str | None
    provider_id: str | None
    algorithm: str | None
    public_key: bytes | None
    credential_fingerprint: str | None

    def usable_at(self, moment: datetime.datetime) -> bool:
        """Mirrors `approval.py`'s `ApprovalVerifierRecord.usable_at`
        exactly: a revoked verifier is unusable from the moment of
        revocation onward, not merely for signatures produced afterward.
        """
        if self.revoked_at is not None and moment >= self.revoked_at:
            return False
        if moment < self.valid_from:
            return False
        return not (self.valid_to is not None and moment >= self.valid_to)


@dataclasses.dataclass(frozen=True)
class VerifiedApproval:
    """What a successful proof verification hands back: the *approving*
    principal, verified from the signature or attestation -- never the
    authenticated caller of the HTTP request (Appendix A.6's own stated
    distinction; see the service module's `complete` for where the two are
    deliberately kept apart).
    """

    approving_principal_issuer: str
    approving_principal_subject: str
    credential_fingerprint: str


class VerifierAttestationProvider(Protocol):
    """One approved in-process provider for `provider_delegated` completion.

    Deliberately **not** boolean-only, unlike `enrollment.py`'s and
    `approval.py`'s own attestation-provider protocols. Enrollment can
    tolerate a `provider_delegated` verifier whose enrollment row carries no
    static principal (D1: the provider names the exact subject dynamically,
    at approval time) because a boolean answer is enough to decide whether
    to *write* that row. This module cannot: `arc_projection_approval_
    evidence.approving_principal_issuer/subject` are non-nullable, so a
    successful provider-delegated approval must produce a concrete
    principal from *somewhere*, and only the provider -- which knows who it
    is asserting for -- can supply one. Returning `None` means "did not
    validate"; returning `(issuer, subject)` means "validated, and this is
    who it names".
    """

    def __call__(
        self, *, canonical_evidence: bytes, assertion_format: str, assertion_base64: str
    ) -> tuple[str, str] | None: ...


class SignatureVerifier(Protocol):
    def __call__(self, public_key: bytes, signature: bytes, payload: bytes) -> bool: ...


def _ed25519_verify(public_key: bytes, signature: bytes, payload: bytes) -> bool:
    """Verify an Ed25519 signature, returning `False` rather than raising.

    A malformed key, a malformed signature, and a wrong signature are all
    "not verified" to the caller -- matching every other ARC verification
    module's own identically-named helper (`enrollment.py`, `approval.py`).
    """
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, ValueError):
        return False
    return True


def build_canonical_evidence(
    *,
    artifact_id: uuid.UUID,
    revision_id: uuid.UUID,
    artifact_semantics_digest: str,
    review_package_digest: str,
) -> bytes:
    """The exact `arc_artifact_revision_v1` object a verifier signs (or a
    provider attests over) -- `A`'s preimage: `A =
    sha256(canonical(arc_artifact_revision_v1(S, R, target identity)))`.

    Callers pass `S` and `R` from the injected `ReviewPackageService`
    (see `approval_challenge.py`); this function owns only the final
    `S, R -> A` step, which needs nothing beyond the target identity every
    caller already has from the proposal version's own bijection.
    """
    obj: dict[str, object] = {
        "profile": ARTIFACT_REVISION_PROFILE,
        "artifact_id": str(artifact_id),
        "revision_id": str(revision_id),
        "artifact_semantics_digest": artifact_semantics_digest,
        "review_package_digest": review_package_digest,
        "actor_separation_profile": ACTOR_SEPARATION_PROFILE,
    }
    try:
        return authoring_profiles.canonicalize_artifact_revision_v1(obj)
    except CanonicalizationError as exc:
        msg = f"approval-target object does not canonicalize: {exc}"
        raise ApprovalChallengeError(msg) from exc


def verify_proof(
    *,
    verifier: VerifierMaterial,
    proof: ProofInput,
    canonical_evidence_bytes: bytes,
    as_of: datetime.datetime,
    attestation_providers: dict[str, VerifierAttestationProvider],
    signature_verifier: SignatureVerifier,
) -> VerifiedApproval:
    """Verify *proof* against *canonical_evidence_bytes*, using *verifier*'s
    committed signing material -- never the caller's own claimed identity.

    Every failure path raises `ApprovalVerificationFailed` with no
    disclosure of which specific check failed (Appendix A.5), matching
    `ApprovalEvidenceVerifier.verify`'s and `EnrollmentService._verify_
    proof`'s own collapsed-failure convention.
    """
    if ARTIFACT_ACTIVATION_EVIDENCE_TYPE not in verifier.allowed_evidence_types:
        msg = f"verifier {verifier.approval_verifier_id!r} is not permitted for artifact_activation evidence"
        raise ApprovalVerificationFailed(msg)
    if not verifier.usable_at(as_of):
        msg = f"verifier {verifier.approval_verifier_id!r} is expired or revoked"
        raise ApprovalVerificationFailed(msg)
    if verifier.principal_binding_kind is None:
        # A pre-D1 verifier with no principal binding at all (the legacy
        # `exception_approval` path `VerifierRegistry.register` still
        # writes) cannot vouch for a principal-bound protocol.
        msg = f"verifier {verifier.approval_verifier_id!r} carries no D1 principal binding"
        raise ApprovalVerificationFailed(msg)

    signing_input = _SIGNING_DOMAIN + canonical_evidence_bytes

    if verifier.principal_binding_kind == "exact_principal":
        if not isinstance(proof, DetachedSignatureProofInput):
            msg = "exact_principal verifiers complete with a detached signature, not a provider attestation"
            raise ApprovalVerificationFailed(msg)
        if verifier.algorithm is None or proof.signature_algorithm != verifier.algorithm:
            msg = "signature algorithm does not match the verifier's registered algorithm"
            raise ApprovalVerificationFailed(msg)
        if verifier.public_key is None or len(verifier.public_key) != _ED25519_PUBLIC_KEY_BYTES:
            msg = f"verifier {verifier.approval_verifier_id!r} has no usable public key"
            raise ApprovalVerificationFailed(msg)
        try:
            signature = base64.b64decode(proof.signature_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            msg = "signature_base64 is not valid base64"
            raise ApprovalVerificationFailed(msg) from exc
        if not signature_verifier(verifier.public_key, signature, signing_input):
            msg = "approval evidence signature did not verify"
            raise ApprovalVerificationFailed(msg)
        if verifier.principal_issuer is None or verifier.principal_subject is None:
            # Unreachable given the D1 shape CHECK (`exact_principal`
            # requires both), kept so this stays fail-closed rather than a
            # bare assert if that invariant is ever weakened.
            msg = f"verifier {verifier.approval_verifier_id!r} has no exact_principal identity to record"
            raise ApprovalVerificationFailed(msg)
        if verifier.credential_fingerprint is None:
            msg = f"verifier {verifier.approval_verifier_id!r} has no credential fingerprint to snapshot"
            raise ApprovalVerificationFailed(msg)
        return VerifiedApproval(
            approving_principal_issuer=verifier.principal_issuer,
            approving_principal_subject=verifier.principal_subject,
            credential_fingerprint=verifier.credential_fingerprint,
        )

    # provider_delegated
    if not isinstance(proof, AttestationProofInput):
        msg = "provider_delegated verifiers complete with a provider attestation, not a detached signature"
        raise ApprovalVerificationFailed(msg)
    if proof.provider_id != verifier.provider_id:
        msg = "attestation names a different provider than the verifier is registered for"
        raise ApprovalVerificationFailed(msg)
    provider = attestation_providers.get(proof.provider_id)
    if provider is None:
        # The honest, real-today state of every deployment: no in-process
        # attestation provider is configured anywhere, matching `enrollment
        # .py`'s identical statement for D1 provider-delegated enrollment.
        # This branch is therefore exercised only by an injected test
        # double until an operator configures one.
        msg = f"no in-process attestation provider is configured for {proof.provider_id!r} on this deployment"
        raise ApprovalVerificationFailed(msg)
    asserted = provider(
        canonical_evidence=canonical_evidence_bytes,
        assertion_format=proof.assertion_format,
        assertion_base64=proof.assertion_base64,
    )
    if asserted is None:
        msg = f"in-process provider {proof.provider_id!r} did not validate the attestation"
        raise ApprovalVerificationFailed(msg)
    if verifier.credential_fingerprint is None:
        msg = f"verifier {verifier.approval_verifier_id!r} has no credential fingerprint to snapshot"
        raise ApprovalVerificationFailed(msg)
    asserted_issuer, asserted_subject = asserted
    return VerifiedApproval(
        approving_principal_issuer=asserted_issuer,
        approving_principal_subject=asserted_subject,
        credential_fingerprint=verifier.credential_fingerprint,
    )


def _length_prefixed(*parts: str) -> bytes:
    """Concatenate with each part's byte length prefixed, so no two
    different field splits can collide on the same digest input. Duplicated
    from `source_admission.py`'s own private helper rather than imported --
    matching every other module in this package's stated convention of
    re-deriving a three-line private helper instead of creating a cross-
    module import for it.
    """
    return b"".join(len(p.encode("utf-8")).to_bytes(4, "big") + p.encode("utf-8") for p in parts)


def idempotency_scope_digest(
    *, issuer: str, subject: str, proposal_id: uuid.UUID, proposal_version: int, idempotency_key: str
) -> str:
    """`sha256(canonical{issuer, subject, proposal_id, proposal_version,
    "approval_challenge", idempotency_key})` per Appendix A.5's stated
    scoping rule, specialised to this route's resource identity (the
    proposal version named by `{PV}/approval-challenges`)."""
    return hashlib.sha256(
        _length_prefixed(
            issuer, subject, str(proposal_id), str(proposal_version), "approval_challenge", idempotency_key
        )
    ).hexdigest()


def request_payload_digest(*, approval_verifier_id: str) -> str:
    """The retry-equivalence input for `ApprovalChallengeRequest`: its one
    field. A changed verifier under the same idempotency scope is a
    different request, not a retry of this one.
    """
    return hashlib.sha256(_length_prefixed(approval_verifier_id)).hexdigest()


__all__ = [
    "ARTIFACT_ACTIVATION_EVIDENCE_TYPE",
    "CHALLENGE_TTL",
    "MAX_ATTEMPTS",
    "SIGNING_DOMAIN_LABEL",
    "ApprovalChallengeError",
    "ApprovalVerificationFailed",
    "AttestationProofInput",
    "DetachedSignatureProofInput",
    "ProofInput",
    "SignatureVerifier",
    "VerifiedApproval",
    "VerifierAttestationProvider",
    "VerifierMaterial",
    "build_canonical_evidence",
    "idempotency_scope_digest",
    "request_payload_digest",
    "verify_proof",
]
