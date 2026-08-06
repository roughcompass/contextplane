"""Scalar wire types and the handful of components shared across more than
one section of Appendix A.6. Sibling of `arc_authoring.py`; see that
module's docstring for why this got split out.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from registry.api.schemas.arc_authoring_enums import (
    OwningScope,
    ProvenanceClass,
    ReasonCode,
    SignatureAlgorithm,
    VerificationMethod,
)

# `uuid` uses `uuid.UUID` (renders as a canonical lowercase string, matching
# the rest of `api/schemas/`). `timestamp` uses plain `datetime.datetime` at
# the field-definition sites in the sibling modules below (verified against
# this pydantic version to already serialize a UTC-aware value as RFC 3339
# with a `Z` suffix and no numeric offset -- exactly the stated convention
# -- so no wrapper type is needed; callers must pass timezone-aware UTC
# values, same discipline `arc/schemas/canonical.py` already requires).
# `digest` and `base64` get dedicated pattern-constrained scalars here
# because nothing in the existing codebase declares either shape yet.

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Base64Str = Annotated[
    str, StringConstraints(pattern=r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")
]


class _ClosedModel(BaseModel):
    """Every authoring-surface component forbids unknown fields.

    `additionalProperties: false` on every object is stated once, in
    Appendix A.6's own preamble, for every component -- so it is enforced
    once, here, rather than repeated as a `model_config` line on every
    class across every sibling module that could individually forget it.
    """

    model_config = ConfigDict(extra="forbid")


class _ScopeColumnsMixin(BaseModel):
    """Appendix A.6 preamble: every component carrying `owning_scope`
    requires `target_tenant_id` exactly when the scope is `tenant`. No
    later task claims a specific refusal code for this rule (see
    `arc_authoring.py`'s docstring for the two rules that do, and why this
    module does not enforce those), so enforcing it here as ordinary shape
    validation does not preempt anyone.
    """

    owning_scope: OwningScope
    target_tenant_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check_scope_columns(self) -> _ScopeColumnsMixin:
        if self.owning_scope is OwningScope.TENANT and self.target_tenant_id is None:
            raise ValueError("target_tenant_id is required when owning_scope is 'tenant'")
        if self.owning_scope is OwningScope.GLOBAL and self.target_tenant_id is not None:
            raise ValueError("target_tenant_id is forbidden when owning_scope is 'global'")
        return self


class EmptyRequest(_ClosedModel):
    """A request body with no fields -- used by routes whose action needs
    no input beyond the resource path."""


class ReasonRequest(_ClosedModel):
    """Body shared by every bare state-transition route (withdraw, reject,
    supersede, revoke)."""

    reason_code: ReasonCode
    note: str | None = Field(default=None, max_length=2000)


class ActorRef(_ClosedModel):
    """Response-only. Never accepted in a request body -- see
    `arc_authoring_enums.RESERVED_ACTOR_FIELDS`.
    """

    issuer: str
    subject: str


class DetachedSignatureProof(_ClosedModel):
    """One `ApprovalProof` variant: a signature computed directly over a
    named profile's canonical bytes."""

    verification_method: Literal[VerificationMethod.DETACHED_SIGNATURE] = VerificationMethod.DETACHED_SIGNATURE
    signature_algorithm: SignatureAlgorithm
    signature_base64: Base64Str


class VerifierAttestationProof(_ClosedModel):
    """The other `ApprovalProof` variant: a trusted provider's own
    attestation format, rather than a signature this system verifies
    directly."""

    verification_method: Literal[VerificationMethod.VERIFIER_ATTESTATION] = VerificationMethod.VERIFIER_ATTESTATION
    provider_id: str
    assertion_format: str
    assertion_base64: Base64Str


ApprovalProof = Annotated[
    DetachedSignatureProof | VerifierAttestationProof,
    Field(discriminator="verification_method"),
]


class FieldProvenanceInput(_ClosedModel):
    """Request-only. Deliberately not one of the profile-aliased
    components in `arc_authoring_profiles.py`: it is a different shape
    than the persisted `arc_field_provenance_v1` canonical profile (no
    `author_issuer`/`author_subject` split, an `excerpt_digest` name
    instead of `quoted_excerpt_digest`, and a `source_evidence_id` the
    profile does not carry). Carries no `author`: for a `human_judgment`
    field the author is the authenticated caller of the `PATCH`, written
    server-side, never client-supplied.
    """

    field_path: str
    provenance_class: ProvenanceClass
    source_evidence_id: uuid.UUID | None = None
    source_anchor: str | None = None
    excerpt_digest: Digest | None = None
    author_role: str | None = None
    derivation_profile: str | None = None


class FieldProvenance(FieldProvenanceInput):
    """Response projection of `FieldProvenanceInput` that adds the recorded
    author. Appears only in read models -- never accepted as input.
    """

    author: ActorRef | None = None


class Citation(_ClosedModel):
    """A pointer from one semantic field back to the source excerpt that
    justifies it."""

    field_path: str
    source_evidence_id: uuid.UUID
    source_anchor: str
    excerpt_digest: Digest


__all__ = [
    "ApprovalProof",
    "ActorRef",
    "Base64Str",
    "Citation",
    "DetachedSignatureProof",
    "Digest",
    "EmptyRequest",
    "FieldProvenance",
    "FieldProvenanceInput",
    "ReasonRequest",
    "VerifierAttestationProof",
    "_ClosedModel",
    "_ScopeColumnsMixin",
]
