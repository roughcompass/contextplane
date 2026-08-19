"""Artifact families and proposals components (Appendix A.6, "Artifact
families and proposals" section).

Sibling of `arc_authoring.py`; see that module's docstring for why the
full Appendix A transcription is split across files.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import Field

from contextplane.api.schemas.arc_authoring_enums import (
    ArtifactKind,
    AvailableAction,
    ChangeKind,
    OperationalIntegrityState,
    ProposalState,
    ReasonCode,
    RefusalCode,
    RiskClassification,
)
from contextplane.api.schemas.arc_authoring_profiles import (
    ArtifactSemantics,
    ArtifactSemanticsPartial,
    ExpectedImpactEnvelope,
    ObservationClassPredicate,
)
from contextplane.api.schemas.arc_authoring_shared import (
    ActorRef,
    Citation,
    Digest,
    FieldProvenance,
    FieldProvenanceInput,
    _ClosedModel,
    _ScopeColumnsMixin,
)


class ArtifactFamilyCreate(_ScopeColumnsMixin, _ClosedModel):
    """Body for `POST /v1/arc/artifacts`: creates the stable identity a
    proposal thread will revise."""

    slug: str
    kind: ArtifactKind
    title: str = Field(max_length=200)


class ArtifactFamilyResponse(_ScopeColumnsMixin, _ClosedModel):
    """An artifact family: the stable identity of a versioned artifact."""

    artifact_id: uuid.UUID
    slug: str
    kind: ArtifactKind
    title: str
    active_revision_id: uuid.UUID | None = None
    created_at: datetime.datetime
    created_by: ActorRef


class ArtifactFamilyListResponse(_ClosedModel):
    """Cursor-paginated artifact families visible to the requesting tenant."""

    items: list[ArtifactFamilyResponse]
    next_cursor: str | None = None


class ProposalOpenRequest(_ClosedModel):
    """Body for `POST /v1/arc/artifacts/{id}/proposals`: opens a new
    proposal version against an admitted source."""

    source_evidence_id: uuid.UUID
    reviewed_baseline_revision_id: uuid.UUID | None = None


class ProposalPatchRequest(_ClosedModel):
    """Body for `PATCH {PV}`: replaces a proposal version's candidate
    semantics and field provenance."""

    semantics: ArtifactSemantics
    field_provenance: list[FieldProvenanceInput]


class ProposalSummary(_ClosedModel):
    """The list-view row for one proposal version; used by both REST list
    responses and the MCP `PagedProposalSummaries` result."""

    proposal_id: uuid.UUID
    proposal_version: int
    artifact_id: uuid.UUID
    state: ProposalState
    risk_classification: RiskClassification | None = None
    created_at: datetime.datetime


class ProposalThreadResponse(_ClosedModel):
    """A proposal thread: the identity and version sequence for one
    artifact family's attempts to create or revise a member."""

    proposal_id: uuid.UUID
    artifact_id: uuid.UUID
    latest_version: int
    versions: list[ProposalSummary]


class ProposalVersionResponse(_ClosedModel):
    """The detail view of one immutable proposal version, including its
    current state, allowed transitions, and available actions."""

    proposal_id: uuid.UUID
    proposal_version: int
    artifact_id: uuid.UUID
    state: ProposalState
    revision_id: uuid.UUID | None = None
    source_evidence_id: uuid.UUID
    reviewed_baseline_revision_id: uuid.UUID | None = None
    risk_classification: RiskClassification | None = None
    risk_algorithm_version: str | None = None
    allowed_transitions: list[ProposalState]
    available_actions: list[AvailableAction]
    reason_codes: list[ReasonCode]
    operational_integrity_state: OperationalIntegrityState
    created_at: datetime.datetime
    frozen_at: datetime.datetime | None = None


class ValidationErrorItem(_ClosedModel):
    """One field-level validation failure in `ValidationResponse.errors`."""

    field_path: str
    code: RefusalCode
    message: str


class ValidationResponse(_ClosedModel):
    """Result of `POST {PV}/validate`: whether the current candidate
    passes closed-schema and conditional-requiredness checks."""

    valid: bool
    errors: list[ValidationErrorItem]


class SemanticTestCase(_ClosedModel):
    """One test case in a `SemanticTestRequest`: a predicate manifest to
    execute against the candidate semantics."""

    test_id: str
    manifest: ObservationClassPredicate


class SemanticTestRequest(_ClosedModel):
    """Body for `POST {PV}/semantic-tests`."""

    tests: list[SemanticTestCase]


class SemanticTestResultItem(_ClosedModel):
    """One executed test's frozen expected/actual result."""

    test_id: str
    passed: bool
    expected: dict[str, Any]
    actual: dict[str, Any]


class SemanticTestResultResponse(_ClosedModel):
    """Result of `POST {PV}/semantic-tests`, and the same shape embedded
    in `ReviewPackageResponse.semantic_tests`."""

    results: list[SemanticTestResultItem]


class BaselineDiffChange(_ClosedModel):
    """One field-level change in `BaselineDiffResponse.changes`."""

    field_path: str
    change_kind: ChangeKind
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class BaselineDiffResponse(_ClosedModel):
    """Result of `GET {PV}/baseline-diff`, and the same shape embedded in
    `ReviewPackageResponse.baseline_diff`."""

    baseline_revision_id: uuid.UUID | None = None
    changes: list[BaselineDiffChange]


class ReachConfirmationRequest(_ClosedModel):
    """Body for `POST {PV}/reach-confirmations`: the field paths the
    caller is confirming reach for."""

    field_paths: list[str]


class ReachConfirmationItem(_ClosedModel):
    """One field's confirmation state in `ReachConfirmationResponse`."""

    field_path: str
    confirmed: bool
    confirmed_at: datetime.datetime | None = None
    confirmed_by: ActorRef | None = None


class ReachConfirmationResponse(_ClosedModel):
    """Result of `POST {PV}/reach-confirmations`, and the same shape
    embedded in `ReviewPackageResponse.reach_confirmations`."""

    confirmations: list[ReachConfirmationItem]


class DraftRequest(_ClosedModel):
    """Body for `POST {PV}/draft`: asks the drafter for a patch covering
    the named field paths, sourced from the given evidence."""

    source_evidence_id: uuid.UUID
    target_field_paths: list[str]


class DraftPatchResponse(_ClosedModel):
    """Result of `POST {PV}/draft`: a proposed partial patch with its
    supporting citations."""

    patch: ArtifactSemanticsPartial  # type: ignore[valid-type]  # dynamically built by _make_partial(); see arc_authoring_profiles.py
    citations: list[Citation]
    declined_field_paths: list[str]


class SubmitRequest(_ClosedModel):
    """Body for `POST {PV}/submit`."""

    expected_impact_envelope: ExpectedImpactEnvelope


class JudgmentAuthor(_ClosedModel):
    """Response-only. See Appendix A.4: recorded authors, server-written."""

    field_path: str
    issuer: str
    subject: str
    role: str


class ReviewPackageResponse(_ClosedModel):
    """Result of `GET {PV}/review-package`: everything a projection
    approver needs to review before signing, and the same object whose
    digest the approval evidence binds."""

    review_package_digest: Digest
    artifact_semantics_digest: Digest
    artifact_revision_digest: Digest
    baseline_diff: BaselineDiffResponse
    field_provenance: list[FieldProvenance]
    citations: list[Citation]
    judgment_authors: list[JudgmentAuthor]
    prose_readback: str
    semantic_tests: SemanticTestResultResponse
    expected_impact_envelope: ExpectedImpactEnvelope
    risk_classification: RiskClassification
    risk_algorithm_version: str
    reach_confirmations: ReachConfirmationResponse
    submission_identity: ActorRef


__all__ = [
    "ArtifactFamilyCreate",
    "ArtifactFamilyListResponse",
    "ArtifactFamilyResponse",
    "BaselineDiffChange",
    "BaselineDiffResponse",
    "DraftPatchResponse",
    "DraftRequest",
    "JudgmentAuthor",
    "ProposalOpenRequest",
    "ProposalPatchRequest",
    "ProposalSummary",
    "ProposalThreadResponse",
    "ProposalVersionResponse",
    "ReachConfirmationItem",
    "ReachConfirmationRequest",
    "ReachConfirmationResponse",
    "ReviewPackageResponse",
    "SemanticTestCase",
    "SemanticTestRequest",
    "SemanticTestResultItem",
    "SemanticTestResultResponse",
    "SubmitRequest",
    "ValidationErrorItem",
    "ValidationResponse",
]
