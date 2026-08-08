"""Verifier enrollment and projection approval components (Appendix A.6,
"Verifier enrollment and projection approval" section).

Sibling of `arc_authoring.py`; see that module's docstring for why the
full Appendix A transcription is split across files.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Literal

from pydantic import model_validator

from registry.api.schemas.arc_authoring_enums import EvidenceType, PrincipalBindingKind, SignatureAlgorithm
from registry.api.schemas.arc_authoring_shared import ApprovalProof, Base64Str, Digest, _ClosedModel, _ScopeColumnsMixin


class EnrollmentChallengeRequest(_ScopeColumnsMixin, _ClosedModel):
    """`principal_issuer`/`principal_subject`/`provider_allowed_principal_issuer`
    are legitimate *target*-principal fields (Appendix A.4) naming the
    principal being enrolled -- not the reserved *actor* fields the caller
    is refused for supplying.
    """

    binding_kind: PrincipalBindingKind
    principal_issuer: str | None = None
    principal_subject: str | None = None
    provider_id: str | None = None
    provider_allowed_principal_issuer: str | None = None
    evidence_types: list[EvidenceType]
    signature_algorithm: SignatureAlgorithm
    public_key_base64: Base64Str
    valid_from: datetime.datetime
    valid_to: datetime.datetime

    @model_validator(mode="after")
    def _check_binding_kind(self) -> EnrollmentChallengeRequest:
        if self.binding_kind is PrincipalBindingKind.EXACT_PRINCIPAL:
            if self.principal_issuer is None or self.principal_subject is None:
                raise ValueError("exact_principal binding requires principal_issuer and principal_subject")
            if self.provider_id is not None or self.provider_allowed_principal_issuer is not None:
                raise ValueError("exact_principal binding forbids provider_id and provider_allowed_principal_issuer")
        else:
            if self.provider_id is None or self.provider_allowed_principal_issuer is None:
                raise ValueError(
                    "provider_delegated binding requires provider_id and provider_allowed_principal_issuer"
                )
        return self


class EnrollmentChallengeResponse(_ClosedModel):
    """The signing challenge a verifier enrollment must complete: the
    caller signs `canonical_enrollment_bytes_base64` and submits that
    signature via `VerifierRegistrationRequest`."""

    enrollment_challenge_id: uuid.UUID
    canonical_enrollment_bytes_base64: Base64Str
    signing_domain: str
    expires_at: datetime.datetime


class VerifierRegistrationRequest(_ClosedModel):
    """Body for `POST /v1/arc/admin/approval-verifiers`: completes a
    previously created enrollment challenge with proof of possession."""

    enrollment_challenge_id: uuid.UUID
    proof: ApprovalProof


class ApprovalVerifierResponse(_ScopeColumnsMixin, _ClosedModel):
    """An enrolled projection-approval verifier."""

    approval_verifier_id: uuid.UUID
    binding_kind: PrincipalBindingKind
    principal_issuer: str
    principal_subject: str
    provider_id: str | None = None
    credential_fingerprint: Digest
    evidence_types: list[EvidenceType]
    valid_from: datetime.datetime
    valid_to: datetime.datetime
    enrolled_at: datetime.datetime
    revoked_at: datetime.datetime | None = None


class ApprovalChallengeRequest(_ClosedModel):
    """Body for `POST {PV}/approval-challenges`: names the enrolled
    verifier who will complete the challenge."""

    approval_verifier_id: uuid.UUID


class ApprovalChallengeResponse(_ClosedModel):
    """The signing challenge a projection approval must complete: the
    verifier signs `canonical_evidence_bytes_base64` and submits that
    signature via `ApprovalCompletionRequest`."""

    approval_challenge_id: uuid.UUID
    canonical_evidence_bytes_base64: Base64Str
    signing_domain: str
    approval_nonce: str
    expires_at: datetime.datetime


class ApprovalCompletionRequest(_ClosedModel):
    """Body for `POST /v1/arc/approval-challenges/{id}/complete`: the
    verifier's proof over the challenge's canonical evidence bytes."""

    proof: ApprovalProof


class ProjectionApprovalEvidenceResponse(_ClosedModel):
    """Verified projection-approval evidence binding a proposal version's
    revision to its approving principal."""

    evidence_id: uuid.UUID
    proposal_id: uuid.UUID
    proposal_version: int
    revision_id: uuid.UUID
    approved_payload_digest: Digest
    approval_verifier_id: uuid.UUID
    approving_principal_issuer: str
    approving_principal_subject: str
    verified_at: datetime.datetime
    revoked_at: datetime.datetime | None = None


class ExceptionApprovalEvidenceRequest(_ClosedModel):
    """The only evidence type this route accepts; every other value is
    refused with `arc_evidence_type_not_writable`. This route used to also
    accept a direct `artifact_activation` write, bypassing verification
    entirely; that path was removed, so activation evidence can now only
    come from the verified challenge/proof round trip.
    """

    evidence_type: Literal[EvidenceType.EXCEPTION_APPROVAL] = EvidenceType.EXCEPTION_APPROVAL
    exception_id: uuid.UUID
    verifier_id: str
    proof: ApprovalProof


class ApprovalEvidenceResponse(_ClosedModel):
    """Recorded evidence from the legacy exception-approval-evidence route."""

    evidence_id: uuid.UUID
    revision_id: uuid.UUID
    evidence_type: EvidenceType
    verified_at: datetime.datetime
    revoked_at: datetime.datetime | None = None


__all__ = [
    "ApprovalChallengeRequest",
    "ApprovalChallengeResponse",
    "ApprovalCompletionRequest",
    "ApprovalEvidenceResponse",
    "ApprovalVerifierResponse",
    "EnrollmentChallengeRequest",
    "EnrollmentChallengeResponse",
    "ExceptionApprovalEvidenceRequest",
    "ProjectionApprovalEvidenceResponse",
    "VerifierRegistrationRequest",
]
