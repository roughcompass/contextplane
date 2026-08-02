"""Approval-evidence verification: the trust chain behind every artifact
activation, exception approval, and gateway emergency bypass.

`ApprovalEvidenceVerifier` checks one `ApprovalEvidenceV1` object end to end:
its discriminated shape is exactly the fields its declared `evidence_type` and
`verification_method` permit and no others, its named verifier is purpose- and
scope-bound, within its trust window, and not revoked, its own review period
has not lapsed, and the evidence record itself has not been separately
withdrawn. For `operator_signed` evidence that means an Ed25519 signature over
the canonical evidence object; for `verifier_attested` evidence it means
handing that same canonical object to the registered in-process provider and
trusting its answer.

It deliberately does not compare the evidence against whatever it is meant to
approve -- an artifact revision's content digest, an exception's replacement
descriptor, a bypass's action instance. That comparison needs the specific
thing being approved, which this module has no reason to know how to build;
the caller that does know (the artifact, exception, or gateway service) can
compare its own value against the digest this module hands back. This module
answers one question only: is the evidence itself trustworthy.

Every failure here -- unknown evidence type or verification method, a
malformed discriminated shape, an unknown or wrongly-kind or out-of-scope or
expired or revoked verifier, a lapsed review period, revoked evidence, a bad
signature, a rejected attestation -- collapses to the same outward behavior:
reject, no partial trust, no conservative fallback. That is why one exception
type covers all of them.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy.ext.asyncio import AsyncSession

from registry.arc.schemas.canonical import CanonicalizationError, canonicalize_approval_evidence
from registry.arc.types import AuthorityScope

# Domain separation over the canonical evidence bytes, distinct from every
# other ARC signing profile's tag: a signature produced for, say, a host
# attestation must never verify here even if the canonical bytes happened to
# coincide.
_SIGNING_DOMAIN = b"ARC-APPROVAL-EVIDENCE-V1\x00"

_EVIDENCE_TYPES = frozenset(
    {
        "artifact_activation",
        "exception_approval",
        "global_exception_approval",
        "gateway_emergency_bypass",
    }
)
_EXCEPTION_EVIDENCE_TYPES = frozenset({"exception_approval", "global_exception_approval"})

_VERIFICATION_METHODS = frozenset({"operator_signed", "verifier_attested"})

_OPERATOR_KIND = "operator_public_key"
_PROVIDER_KIND = "trusted_attestation_provider"


class ApprovalEvidenceVerificationError(Exception):
    """Approval evidence failed one of the checks that make it trustworthy.

    One type for every failure, matching every other ARC verification module:
    the caller's outward response does not vary by which check failed, so the
    difference between them matters only for the message and for an operator
    reading logs, never for control flow.
    """


@dataclasses.dataclass(frozen=True)
class ApprovalEvidence:
    """One `ApprovalEvidenceV1` object, before verification.

    Mirrors `arc_approval_evidence`'s own columns except `evidence_id` and
    `created_at`. Both are assigned only once the row exists, and evidence
    cannot be signed over its own not-yet-existing primary key, so this type
    -- and the canonical form built from it -- has no field for either. Every
    column that *can* be signed has a field here, including the ones only the
    declared `evidence_type` or `verification_method` makes meaningful,
    because the discriminated-shape checks below need to see a field they do
    not expect populated in order to reject it.
    """

    evidence_type: str
    scope_kind: str
    approving_principal: str
    approving_role: str
    approval_timestamp: datetime.datetime
    approved_payload_digest: str
    verification_method: str
    audit_log_reference: str
    scope_tenant_id: uuid.UUID | None = None
    approved_artifact_id: uuid.UUID | None = None
    approved_revision_id: uuid.UUID | None = None
    approved_exception_id: uuid.UUID | None = None
    source_system_approval_locator: str | None = None
    expires_at: datetime.datetime | None = None
    policy_version: str | None = None
    action_instance_id: str | None = None
    signer_key_id: str | None = None
    signature: str | None = None  # base64, exactly as received
    approval_verifier_id: str | None = None
    # Provider-specific and opaque to this module: must already be JSON
    # primitives (str/int/float/bool/None/list/dict) all the way down, since
    # it travels through canonicalization along with everything else and
    # canonicalization rejects anything that is not a JSON type -- a UUID or
    # datetime nested inside must be stringified by the caller first.
    verifier_attestation: dict[str, Any] | None = None
    verifier_identity: str | None = None


@dataclasses.dataclass(frozen=True)
class ApprovalVerifierRecord:
    """One `arc_approval_verifiers` row, as verification needs it.

    Both `verifier_kind`s travel through this one shape rather than a union,
    matching the single underlying table: `operator_public_key` populates
    `algorithm`/`public_key` and leaves `provider_id` unset;
    `trusted_attestation_provider` is the reverse. This module checks
    `verifier_kind` against what the evidence declares -- it does not infer
    the kind from which of the other two fields happens to be set.
    """

    approval_verifier_id: str
    verifier_kind: str
    allowed_evidence_types: frozenset[str]
    scope_kind: str
    scope_tenant_id: uuid.UUID | None
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    revoked_at: datetime.datetime | None
    algorithm: str | None = None
    public_key: bytes | None = None
    provider_id: str | None = None

    def usable_at(self, moment: datetime.datetime) -> bool:
        """Whether this verifier was trusted at `moment`.

        Unlike a receipt-signing key, a revoked approval verifier does not
        stay usable for verifications that have not already happened:
        revocation here is meant to invalidate every dependent activation or
        exception that has not already been re-checked, so this folds
        `revoked_at` into the same answer as the validity window rather than
        treating revocation as a signing-only restriction.
        """
        if self.revoked_at is not None and moment >= self.revoked_at:
            return False
        if moment < self.valid_from:
            return False
        return not (self.valid_to is not None and moment >= self.valid_to)


class ApprovalVerifierLookup(Protocol):
    """The one capability this module needs from verifier storage.

    Takes the caller's own session and must run inside the caller's open
    transaction, matching every other ARC trust-registry lookup: the real
    implementation locks the row `FOR SHARE` so it can be revoked
    concurrently under a plain `UPDATE` (an implicit `FOR UPDATE` in
    Postgres) without an interleaving where a resolution verifies against a
    key whose revocation has already committed.
    """

    async def get(self, session: AsyncSession, verifier_id: str) -> ApprovalVerifierRecord | None: ...


class ApprovalEvidenceRevocationLookup(Protocol):
    """Whether one evidence record has itself been withdrawn.

    Independent of verifier revocation: revoking a verifier invalidates
    everything it ever approved, but revoking one piece of evidence must not
    touch anything else the same verifier is still trusted for. Returns the
    revocation moment so a caller building an audit trail or error message
    does not have to make a second call to learn it.
    """

    async def get(self, session: AsyncSession, evidence_id: uuid.UUID) -> datetime.datetime | None: ...


class SignatureVerifier(Protocol):
    def __call__(self, public_key: bytes, signature: bytes, payload: bytes) -> bool: ...


class VerifierAttestationProvider(Protocol):
    """One approved in-process provider for `verifier_attested` evidence.

    Invoked only after the named verifier has already resolved as
    `trusted_attestation_provider`, allowed for this evidence type, in scope,
    within its validity window, and not revoked. Answers exactly one
    question: does this provider's own trust basis cover this exact canonical
    evidence. "In-process" is the load-bearing word -- this is a local call,
    not a network round trip, which is why it is synchronous.
    """

    def __call__(self, *, canonical_evidence: bytes, verifier_attestation: Mapping[str, Any]) -> bool: ...


@dataclasses.dataclass(frozen=True)
class VerifiedApprovalEvidence:
    """What a successful verification hands back.

    Carries the discriminated target fields for every `evidence_type` rather
    than just the one the caller expects, so the caller -- not this module --
    decides whether the resolved target matches what it is actually trying to
    activate or approve.
    """

    evidence_type: str
    scope_kind: str
    scope_tenant_id: uuid.UUID | None
    approved_artifact_id: uuid.UUID | None
    approved_revision_id: uuid.UUID | None
    approved_exception_id: uuid.UUID | None
    approved_payload_digest: str
    approving_principal: str
    approving_role: str
    approval_timestamp: datetime.datetime
    expires_at: datetime.datetime | None
    policy_version: str | None
    action_instance_id: str | None
    verification_method: str
    verifier_id: str
    audit_log_reference: str


def _ed25519_verify(public_key: bytes, signature: bytes, payload: bytes) -> bool:
    """Verify an Ed25519 signature, returning False rather than raising.

    A malformed key, a malformed signature, and a wrong signature are all
    "not verified" to the caller, which is what lets every call site here
    treat verification as a boolean rather than catching a library exception.
    """
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, ValueError):
        return False
    return True


def _require_nonempty(value: str, *, field: str) -> None:
    if not value:
        msg = f"{field} is required and must not be empty"
        raise ApprovalEvidenceVerificationError(msg)


def _require_aware(moment: datetime.datetime, *, field: str) -> None:
    if moment.tzinfo is None:
        msg = f"{field} is a naive datetime; approval evidence requires an explicit UTC offset"
        raise ApprovalEvidenceVerificationError(msg)


def _check_scope_shape(evidence: ApprovalEvidence) -> None:
    try:
        scope = AuthorityScope(evidence.scope_kind)
    except ValueError as exc:
        msg = f"unknown scope_kind {evidence.scope_kind!r}"
        raise ApprovalEvidenceVerificationError(msg) from exc
    if scope is AuthorityScope.GLOBAL:
        if evidence.scope_tenant_id is not None:
            msg = "global-scope evidence must not carry a scope_tenant_id"
            raise ApprovalEvidenceVerificationError(msg)
    elif evidence.scope_tenant_id is None:
        msg = f"{scope!s}-scope evidence requires a scope_tenant_id"
        raise ApprovalEvidenceVerificationError(msg)


def _check_target_shape(evidence: ApprovalEvidence) -> None:
    """Exactly one target group populated, matching `evidence_type`.

    Enforced as a closed shape -- not just "the required fields are present"
    but "the other groups' fields are absent" -- because a row carrying, say,
    both an `approved_artifact_id` and an `approved_exception_id` is
    ambiguous about what was actually approved, and ambiguity here is exactly
    what a downstream consumer picking the wrong one would exploit.
    """
    activation = evidence.approved_artifact_id is not None or evidence.approved_revision_id is not None
    exception = evidence.approved_exception_id is not None
    bypass = evidence.action_instance_id is not None or evidence.policy_version is not None

    if evidence.evidence_type == "artifact_activation":
        if evidence.approved_artifact_id is None or evidence.approved_revision_id is None:
            msg = "artifact_activation evidence requires both approved_artifact_id and approved_revision_id"
            raise ApprovalEvidenceVerificationError(msg)
        if exception or bypass:
            msg = "artifact_activation evidence must not carry exception or bypass target fields"
            raise ApprovalEvidenceVerificationError(msg)
    elif evidence.evidence_type in _EXCEPTION_EVIDENCE_TYPES:
        if evidence.approved_exception_id is None:
            msg = f"{evidence.evidence_type} evidence requires approved_exception_id"
            raise ApprovalEvidenceVerificationError(msg)
        if activation or bypass:
            msg = f"{evidence.evidence_type} evidence must not carry activation or bypass target fields"
            raise ApprovalEvidenceVerificationError(msg)
    else:  # gateway_emergency_bypass -- evidence_type membership already checked
        if evidence.action_instance_id is None or evidence.policy_version is None:
            msg = "gateway_emergency_bypass evidence requires both action_instance_id and policy_version"
            raise ApprovalEvidenceVerificationError(msg)
        if activation or exception:
            msg = "gateway_emergency_bypass evidence must not carry activation or exception target fields"
            raise ApprovalEvidenceVerificationError(msg)


def _check_representation_shape(evidence: ApprovalEvidence) -> None:
    if evidence.verification_method == "operator_signed":
        if evidence.signer_key_id is None or evidence.signature is None:
            msg = "operator_signed evidence requires both signer_key_id and signature"
            raise ApprovalEvidenceVerificationError(msg)
        if evidence.approval_verifier_id is not None or evidence.verifier_attestation is not None:
            msg = "operator_signed evidence must not carry approval_verifier_id or verifier_attestation"
            raise ApprovalEvidenceVerificationError(msg)
    else:  # verifier_attested -- verification_method membership already checked
        if evidence.approval_verifier_id is None or evidence.verifier_attestation is None:
            msg = "verifier_attested evidence requires both approval_verifier_id and verifier_attestation"
            raise ApprovalEvidenceVerificationError(msg)
        if evidence.signer_key_id is not None or evidence.signature is not None:
            msg = "verifier_attested evidence must not carry signer_key_id or signature"
            raise ApprovalEvidenceVerificationError(msg)


def _canonical_evidence_dict(evidence: ApprovalEvidence) -> dict[str, Any]:
    """The exact object an operator signs or a verifier attestation covers.

    UUID fields are stringified first: `_canonical` already knows how to
    serialize a `datetime`, but a `uuid.UUID` is not a JSON type either, and
    canonicalizing what a party actually signs means representing it exactly
    as that party had to -- as a string.
    """

    def _uuid_str(value: uuid.UUID | None) -> str | None:
        return str(value) if value is not None else None

    return {
        "evidence_type": evidence.evidence_type,
        "scope_kind": evidence.scope_kind,
        "scope_tenant_id": _uuid_str(evidence.scope_tenant_id),
        "approved_artifact_id": _uuid_str(evidence.approved_artifact_id),
        "approved_revision_id": _uuid_str(evidence.approved_revision_id),
        "approved_exception_id": _uuid_str(evidence.approved_exception_id),
        "approved_payload_digest": evidence.approved_payload_digest,
        "approving_principal": evidence.approving_principal,
        "approving_role": evidence.approving_role,
        "source_system_approval_locator": evidence.source_system_approval_locator,
        "approval_timestamp": evidence.approval_timestamp,
        "expires_at": evidence.expires_at,
        "policy_version": evidence.policy_version,
        "action_instance_id": evidence.action_instance_id,
        "verification_method": evidence.verification_method,
        "signer_key_id": evidence.signer_key_id,
        "approval_verifier_id": evidence.approval_verifier_id,
        "verifier_attestation": evidence.verifier_attestation,
        "verifier_identity": evidence.verifier_identity,
        "audit_log_reference": evidence.audit_log_reference,
    }


class ApprovalEvidenceVerifier:
    """Verifies one `ApprovalEvidenceV1` object end to end.

    Takes its two lookups as injected async Protocols rather than opening a
    session of its own, for the same reason `AttestationService` does:
    `session` must be the caller's own open transaction, so a `FOR SHARE`
    lock either lookup takes is held for that transaction's lifetime, not
    released the instant this method returns.
    """

    def __init__(
        self,
        verifier_lookup: ApprovalVerifierLookup,
        evidence_revocation_lookup: ApprovalEvidenceRevocationLookup,
        *,
        attestation_providers: Mapping[str, VerifierAttestationProvider] | None = None,
        signature_verifier: SignatureVerifier | None = None,
    ) -> None:
        self._verifier_lookup = verifier_lookup
        self._evidence_revocation_lookup = evidence_revocation_lookup
        self._attestation_providers = dict(attestation_providers or {})
        self._signature_verifier = signature_verifier or _ed25519_verify

    async def verify(
        self,
        session: AsyncSession,
        *,
        evidence_id: uuid.UUID,
        evidence: ApprovalEvidence,
        as_of: datetime.datetime,
    ) -> VerifiedApprovalEvidence:
        """Verify `evidence`, identified by its own (already allocated) `evidence_id`.

        `as_of` is the caller's request-time instant, never read from a clock
        in here: review expiry and the verifier's own trust window are both
        checked against it, so replaying the same evidence through the same
        call with the same `as_of` always gives the same answer.
        """
        if as_of.tzinfo is None:
            msg = "as_of is a naive datetime; approval evidence verification requires an explicit UTC offset"
            raise ApprovalEvidenceVerificationError(msg)

        if evidence.evidence_type not in _EVIDENCE_TYPES:
            msg = f"unknown evidence_type {evidence.evidence_type!r}"
            raise ApprovalEvidenceVerificationError(msg)
        if evidence.verification_method not in _VERIFICATION_METHODS:
            msg = f"unknown verification_method {evidence.verification_method!r}"
            raise ApprovalEvidenceVerificationError(msg)

        _check_scope_shape(evidence)
        _check_target_shape(evidence)
        _check_representation_shape(evidence)

        _require_nonempty(evidence.approved_payload_digest, field="approved_payload_digest")
        _require_nonempty(evidence.approving_principal, field="approving_principal")
        _require_nonempty(evidence.approving_role, field="approving_role")
        _require_nonempty(evidence.audit_log_reference, field="audit_log_reference")
        _require_aware(evidence.approval_timestamp, field="approval_timestamp")

        expires_at = evidence.expires_at
        if expires_at is not None:
            _require_aware(expires_at, field="expires_at")
            if as_of >= expires_at:
                msg = f"approval evidence review period expired at {expires_at.isoformat()}"
                raise ApprovalEvidenceVerificationError(msg)

        operator_signed = evidence.verification_method == "operator_signed"
        verifier_id = evidence.signer_key_id if operator_signed else evidence.approval_verifier_id
        if verifier_id is None:
            # Unreachable once `_check_representation_shape` has passed; kept
            # so this stays fail-closed and mypy-narrowed without a bare
            # assert, even if the two checks are ever pulled apart.
            msg = f"{evidence.verification_method} evidence has no verifier identifier"
            raise ApprovalEvidenceVerificationError(msg)

        verifier = await self._verifier_lookup.get(session, verifier_id)
        if verifier is None:
            msg = f"no approval verifier registered for {verifier_id!r}"
            raise ApprovalEvidenceVerificationError(msg)

        expected_kind = _OPERATOR_KIND if operator_signed else _PROVIDER_KIND
        if verifier.verifier_kind != expected_kind:
            msg = (
                f"approval verifier {verifier_id!r} is registered as {verifier.verifier_kind!r}, "
                f"not {expected_kind!r}"
            )
            raise ApprovalEvidenceVerificationError(msg)
        if evidence.evidence_type not in verifier.allowed_evidence_types:
            msg = f"approval verifier {verifier_id!r} is not approved for {evidence.evidence_type!r} evidence"
            raise ApprovalEvidenceVerificationError(msg)

        try:
            verifier_scope = AuthorityScope(verifier.scope_kind)
        except ValueError as exc:
            msg = f"approval verifier {verifier_id!r} has unknown scope_kind {verifier.scope_kind!r}"
            raise ApprovalEvidenceVerificationError(msg) from exc
        if verifier_scope not in (AuthorityScope.GLOBAL, AuthorityScope.TENANT):
            msg = f"approval verifier {verifier_id!r} has non-verifier scope_kind {verifier.scope_kind!r}"
            raise ApprovalEvidenceVerificationError(msg)
        if verifier_scope is AuthorityScope.TENANT and evidence.scope_tenant_id != verifier.scope_tenant_id:
            msg = f"approval verifier {verifier_id!r} is scoped to a different tenant than this evidence"
            raise ApprovalEvidenceVerificationError(msg)

        if not verifier.usable_at(as_of):
            msg = f"approval verifier {verifier_id!r} is expired or revoked"
            raise ApprovalEvidenceVerificationError(msg)

        revoked_at = await self._evidence_revocation_lookup.get(session, evidence_id)
        if revoked_at is not None:
            msg = f"approval evidence {evidence_id} was revoked at {revoked_at.isoformat()}"
            raise ApprovalEvidenceVerificationError(msg)

        try:
            canonical_bytes = canonicalize_approval_evidence(_canonical_evidence_dict(evidence))
        except CanonicalizationError as exc:
            # A malformed evidence object (an unknown or missing field, a
            # non-NFC string, ...) is not trustworthy either -- it collapses
            # to the same outward failure as every other check here, not a
            # different error class.
            msg = f"approval evidence does not canonicalize: {exc}"
            raise ApprovalEvidenceVerificationError(msg) from exc

        if operator_signed:
            self._verify_operator_signed(evidence, verifier, canonical_bytes)
        else:
            self._verify_attested(evidence, verifier, canonical_bytes)

        return VerifiedApprovalEvidence(
            evidence_type=evidence.evidence_type,
            scope_kind=evidence.scope_kind,
            scope_tenant_id=evidence.scope_tenant_id,
            approved_artifact_id=evidence.approved_artifact_id,
            approved_revision_id=evidence.approved_revision_id,
            approved_exception_id=evidence.approved_exception_id,
            approved_payload_digest=evidence.approved_payload_digest,
            approving_principal=evidence.approving_principal,
            approving_role=evidence.approving_role,
            approval_timestamp=evidence.approval_timestamp,
            expires_at=evidence.expires_at,
            policy_version=evidence.policy_version,
            action_instance_id=evidence.action_instance_id,
            verification_method=evidence.verification_method,
            verifier_id=verifier_id,
            audit_log_reference=evidence.audit_log_reference,
        )

    def _verify_operator_signed(
        self,
        evidence: ApprovalEvidence,
        verifier: ApprovalVerifierRecord,
        canonical_bytes: bytes,
    ) -> None:
        algorithm = verifier.algorithm
        if algorithm is None or algorithm != "Ed25519":
            msg = f"approval verifier uses unsupported algorithm {algorithm!r}; only Ed25519 is accepted"
            raise ApprovalEvidenceVerificationError(msg)
        public_key = verifier.public_key
        if public_key is None:
            msg = f"approval verifier {verifier.approval_verifier_id!r} has no public key recorded"
            raise ApprovalEvidenceVerificationError(msg)

        # No purpose check here, deliberately. Elsewhere in this subsystem a
        # key is loaded through a purpose-bound provider so a key recorded for
        # one purpose cannot be used for another. `arc_approval_verifiers` has
        # no purpose column -- every row in it is an approval verifier by
        # construction -- so wrapping this key in such a provider would mean
        # asserting a purpose here and then checking our own assertion. That
        # reads like protection and is not, which is worse than its absence.
        signature_b64 = evidence.signature
        if signature_b64 is None:
            # Unreachable once `_check_representation_shape` has passed.
            msg = "operator_signed evidence has no signature"
            raise ApprovalEvidenceVerificationError(msg)
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            msg = "signature is not valid base64"
            raise ApprovalEvidenceVerificationError(msg) from exc

        signing_input = _SIGNING_DOMAIN + canonical_bytes
        if not self._signature_verifier(public_key, signature, signing_input):
            msg = "approval evidence signature did not verify"
            raise ApprovalEvidenceVerificationError(msg)

    def _verify_attested(
        self,
        evidence: ApprovalEvidence,
        verifier: ApprovalVerifierRecord,
        canonical_bytes: bytes,
    ) -> None:
        provider_id = verifier.provider_id
        if provider_id is None:
            msg = f"approval verifier {verifier.approval_verifier_id!r} has no provider_id recorded"
            raise ApprovalEvidenceVerificationError(msg)
        provider = self._attestation_providers.get(provider_id)
        if provider is None:
            msg = f"no in-process attestation provider registered for {provider_id!r}"
            raise ApprovalEvidenceVerificationError(msg)

        attestation = evidence.verifier_attestation
        if attestation is None:
            # Unreachable once `_check_representation_shape` has passed.
            msg = "verifier_attested evidence has no verifier_attestation"
            raise ApprovalEvidenceVerificationError(msg)
        if not provider(canonical_evidence=canonical_bytes, verifier_attestation=attestation):
            msg = f"in-process provider {provider_id!r} did not validate the attestation"
            raise ApprovalEvidenceVerificationError(msg)


__all__ = [
    "ApprovalEvidence",
    "ApprovalEvidenceRevocationLookup",
    "ApprovalEvidenceVerificationError",
    "ApprovalEvidenceVerifier",
    "ApprovalVerifierLookup",
    "ApprovalVerifierRecord",
    "SignatureVerifier",
    "VerifiedApprovalEvidence",
    "VerifierAttestationProvider",
]
