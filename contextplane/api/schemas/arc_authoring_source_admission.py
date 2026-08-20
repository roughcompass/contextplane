"""Source admission components (Appendix A.6, "Source admission" section).

Sibling of `arc_authoring.py`; see that module's docstring for why the
full Appendix A transcription is split across files.
"""

from __future__ import annotations

import datetime
import uuid

from pydantic import Field

from contextplane.api.schemas.arc_authoring_enums import AdmissionMethod, SourceApprovalStatus, VerificationMethod
from contextplane.api.schemas.arc_authoring_profiles import SourceApprovalClaim
from contextplane.api.schemas.arc_authoring_shared import ApprovalProof, Digest, _ClosedModel, _ScopeColumnsMixin


class UploadAdmissionRequest(_ClosedModel):
    """The `metadata` part of the multipart upload; the `body` part is raw
    bytes handled by the route, not a JSON component.
    """

    policy_id: str
    source_system: str
    source_revision_locator: str
    source_content_type: str
    claim: SourceApprovalClaim
    verifier_id: str
    proof: ApprovalProof


class ConnectorFetchRequest(_ClosedModel):
    """Body for `POST /v1/arc/sources/connector-fetches`: admits a source
    body a configured connector fetches, rather than one the caller
    uploads directly."""

    connector_id: str
    source_revision_locator: str
    claim: SourceApprovalClaim
    verifier_id: str
    proof: ApprovalProof


class GraphPromotionRequest(_ClosedModel):
    """Body for `POST /v1/arc/sources/graph-promotions`: admits a claim the
    canonical graph already carries, vouched for by its promotion rather
    than by a signature over bytes this deployment fetched.

    No `claim`, `verifier_id`, or `proof`: every field those carry is read
    from the promotion itself. Accepting them from the caller would let a
    request assert an approving authority the graph does not record.
    """

    claim_id: uuid.UUID
    source_system: str = Field(
        description="The upstream system the promoted claim's evidence points into, "
        "e.g. `bitbucket.org/acme/adr`. Not derived: an evidence ref names a "
        "revision, not the system that issued it."
    )
    review_expires_at: datetime.datetime = Field(
        description="When this citation must be revisited. A graph fact carries no "
        "deadline of its own, and every source evidence row has one."
    )


class SourceConnectorRegistration(_ScopeColumnsMixin, _ClosedModel):
    """Body for registering a configured source connector: the closed set
    of schemes, hosts, media types, and verifiers it may use."""

    connector_id: str
    allowed_schemes: list[str]
    allowed_hosts: list[str]
    allowed_media_types: list[str]
    allowed_verifier_ids: list[str]
    max_bytes: int = Field(le=10_485_760)
    credential_ref: str | None = None


class SourceUploadPolicyRegistration(_ScopeColumnsMixin, _ClosedModel):
    """Body for registering an authorized-upload policy: the closed set of
    media types and verifiers a direct upload under this policy may use."""

    policy_id: str
    allowed_media_types: list[str]
    allowed_verifier_ids: list[str]
    max_bytes: int = Field(le=10_485_760)


class SourceEvidenceResponse(_ClosedModel):
    """Never contains signature bytes, credentials, or the claim's raw proof."""

    source_evidence_id: uuid.UUID
    source_system: str
    source_revision_locator: str
    source_content_digest: Digest
    source_content_type: str
    source_content_bytes: int
    admission_method: AdmissionMethod
    connector_id: str | None = None
    policy_id: str | None = None
    verification_method: VerificationMethod
    verifier_id: str
    admitted_at: datetime.datetime
    verified_at: datetime.datetime
    expires_at: datetime.datetime | None = None
    status: SourceApprovalStatus
    status_checked_at: datetime.datetime
    next_check_at: datetime.datetime


class SourceConnectorResponse(SourceConnectorRegistration):
    """`SourceConnectorRegistration` plus the timestamp the registration
    was accepted."""

    registered_at: datetime.datetime


class SourceUploadPolicyResponse(SourceUploadPolicyRegistration):
    """`SourceUploadPolicyRegistration` plus the timestamp the
    registration was accepted."""

    registered_at: datetime.datetime


__all__ = [
    "ConnectorFetchRequest",
    "GraphPromotionRequest",
    "SourceConnectorRegistration",
    "SourceConnectorResponse",
    "SourceEvidenceResponse",
    "SourceUploadPolicyRegistration",
    "SourceUploadPolicyResponse",
    "UploadAdmissionRequest",
]
