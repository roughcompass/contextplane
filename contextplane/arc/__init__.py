"""Attested context resolution.

ARC gives an agent a deterministic, attested answer to "what am I obliged to
know before I act, and can I prove I was told?" It owns governed context
artifacts with structured applicability, attested task-manifest intake,
deterministic context-bundle assembly under a budget, immutable receipts, and
authorized just-in-time detail retrieval.

This is a logical subsystem inside the Registry monolith, not a separate
service: it uses the same FastAPI app, the same Postgres schema and Alembic
chain, the same MCP server, and the same scheduler. What it does not share is
CAP's authorization model — ARC artifacts and receipts have their own audience
rules, so `ArcAuthorizationService` is the chokepoint for those while CAP
capability visibility still delegates to `VisibilityService`.

Tables live under the `arc_` prefix and are created in
`0001_baseline_schema.py`, the repo's one Alembic migration.

The public surface
------------------

This module is ARC's front door. Everything outside `contextplane/arc/` imports
from `contextplane.arc` and nothing deeper; an import contract in
`pyproject.toml` ("ARC internals are private") enforces it for `api`, `wiring`,
`workers`, `ingest`, `service` and `extraction`. Code *inside* `arc/` still
imports its siblings directly, and tests may reach in — a unit test of an
internal is a legitimate reason to know where that internal lives.

The point is not secrecy. It is that ARC's internal layout — 48 submodules
today — stops being load-bearing for 16 files in other packages. Before this,
moving `service/proposal.py` was a repo-wide edit.

Why this list is as long as it is
---------------------------------

The 139 names below were measured from what external consumers actually import
today, not chosen from what ARC could offer. That measurement exceeds the
curation threshold this codebase sets for an `__all__`, and the deviation is
deliberate and stated rather than hidden: narrowing the set would have meant
either leaving deep imports standing (defeating the contract) or renaming and
regrouping ARC's internals (a behaviour-preserving refactor of a different
size). A long honest list beats a short list with exceptions carved around it.

The list is a measurement, so it should shrink as consumers stop needing the
names. The successor shape is a single `arc: ArcServices` field on the typed
container, which would retire most of the 32 service classes `api/container.py`
names individually. That is out of scope here because it would change the
service graph that the wiring-graph identity test pins, and narrowing an import
surface is not a licence to alter what the composition root builds.

`approval_challenge` and `approval_challenge_verification` are exported as
modules, not as flattened names, because both define `ProofInput`,
`AttestationProofInput` and `DetachedSignatureProofInput` — distinct classes
that collide with `enrollment`'s same-named ones. Their one consumer already
used them module-qualified; flattening would have required renaming classes,
which a move-only task does not do.

`build_arc_services` and the three startup names beside it are here for the
composition root, which is a consumer like any other: it assembles ARC without
knowing which module builds what.

A note for whoever extends this list. `from contextplane.arc import service`
looks like it uses the front door and does not: `service` is a real submodule,
so it resolves to `contextplane.arc.service` and is the same private import
spelled differently. The import contract catches that spelling; a grep for
`contextplane.arc.` does not, which is why the contract and not the grep is the
authority here.
"""

from __future__ import annotations

from contextplane.arc.schemas.authoring_profile_shapes import (
    APPROVAL_VERIFIER_ENROLLMENT_PROFILE,
    ARTIFACT_SEMANTICS_PROFILE,
    DELTA_CODES,
    EXPECTED_IMPACT_ENVELOPE_PROFILE,
    OBSERVATION_CLASS_PREDICATE_PROFILE,
    RISK_CLASSIFICATIONS,
    SCHEMA_BY_PROFILE,
    SOURCE_APPROVAL_CLAIM_PROFILE,
)
from contextplane.arc.schemas.canonical import (
    CANONICAL_PROFILE_VERSIONS,
    manifest_claims_digest,
)
from contextplane.arc.service import approval_challenge as approval_challenge
from contextplane.arc.service import (
    approval_challenge_verification as approval_challenge_verification,
)
from contextplane.arc.service.activation import (
    ActivationEligibility,
    ActivationError,
    ActivationPredicateFailed,
    ActivationRequestMismatch,
    ActivationService,
    RevisionActivation,
)
from contextplane.arc.service.approval_challenge import ApprovalChallengeService
from contextplane.arc.service.approval_trust import ApprovalTrustService
from contextplane.arc.service.approved_exceptions import (
    ExceptionApproval,
    ExceptionDraft,
    ExceptionNotPermitted,
    ExceptionService,
)
from contextplane.arc.service.artifact import (
    ArtifactLifecycleError,
    ArtifactService,
    EvidenceTypeNotWritableError,
)
from contextplane.arc.service.attestation import (
    AttestationEnvelope,
    AttestationService,
    ManifestClaims,
)
from contextplane.arc.service.authorization import (
    ArcAuthorizationError,
    ArcAuthorizationService,
)
from contextplane.arc.service.challenge import ChallengeService
from contextplane.arc.service.checkpoint_export import CheckpointExportService
from contextplane.arc.service.corpus import CorpusReader
from contextplane.arc.service.detail_retrieval import (
    DetailDenied,
    DetailIdempotencyConflict,
    DetailRequest,
    JitService,
)
from contextplane.arc.service.drafter import (
    DrafterModelDisabled,
    DrafterService,
    ReachConfirmationRecord,
)
from contextplane.arc.service.enrollment import (
    AttestationProofInput,
    DetachedSignatureProofInput,
    EnrollmentChallengeRequired,
    EnrollmentError,
    EnrollmentService,
    EnrollmentVerificationFailed,
    ProofInput,
)
from contextplane.arc.service.envelope import EnvelopeInvalid
from contextplane.arc.service.integrity import RevisionIntegrityService
from contextplane.arc.service.operational_chain import (
    OperationalChainIdempotencyConflict,
    OperationalChainIntegrityError,
    OperationalChainService,
)
from contextplane.arc.service.preflight import (
    PreflightError,
    PreflightRegistry,
    credential_fingerprint,
    new_connection_id,
    restriction_digest,
)
from contextplane.arc.service.proposal import (
    ArtifactFamily,
    ProposalService,
    ProposalStateConflict,
    ProposalThread,
    ProposalVersion,
)
from contextplane.arc.service.provenance import (
    ActorNotCallerSupplied,
    ProvenanceInvalid,
    ProvenanceService,
    SemanticsValidationFailed,
)
from contextplane.arc.service.qualification import (
    ObservationFailed,
    ObservationInsufficient,
    ObservationStatus,
    QualificationActorInvalid,
    QualificationComputation,
    QualificationService,
    QualificationUnavailable,
)
from contextplane.arc.service.queries.enrollment import VerifierRow
from contextplane.arc.service.queries.replay_corpus import ReplayCorpusRow
from contextplane.arc.service.queries.source_admission import (
    ConnectorRow,
    UploadPolicyRow,
)
from contextplane.arc.service.receipt import ReceiptService
from contextplane.arc.service.receipt_read import ReceiptReader
from contextplane.arc.service.replay_corpus import (
    ReplayCorpusApprovalConflict,
    ReplayCorpusService,
)
from contextplane.arc.service.resolution import (
    IdempotencyConflict,
    ManifestUnverified,
    ResolutionRequest,
    ResolutionService,
    parse_manifest,
)
from contextplane.arc.service.review_package import (
    BaselineDiff,
    ReviewPackage,
    ReviewPackageIntegrityError,
    ReviewPackageService,
    ReviewPackageUnavailable,
)
from contextplane.arc.service.risk import (
    RiskClassificationError,
    RiskEnvelopeValidator,
)
from contextplane.arc.service.semantic_tests import (
    SemanticTestResult,
    SemanticTestService,
)
from contextplane.arc.service.shadow import (
    ShadowError,
    ShadowService,
)
from contextplane.arc.service.signing import ReceiptSigningProvider
from contextplane.arc.service.source_admission import (
    ApprovalProof,
    ConnectorFetchAdmission,
    ConnectorRegistration,
    SourceAdmissionRefused,
    SourceAdmissionService,
    SourceEvidence,
    SourceIdempotencyConflict,
    UploadAdmission,
    UploadPolicyRegistration,
    iter_upload_file,
)
from contextplane.arc.service.source_status import (
    SourceStatusService,
    SourceStatusUnavailable,
)
from contextplane.arc.service.submission import (
    ArtifactMaterialisationService,
    CandidateSemanticsMissing,
    SubmissionPrerequisiteUnavailable,
)
from contextplane.arc.service.verifier_registry import (
    KIND_PROVIDER,
    VerifierRegistry,
)
from contextplane.arc.types import (
    ArcRequestContext,
    ArcVocabularyError,
    AuthorityScope,
)
from contextplane.arc.wiring import (
    ArcServices,
    assert_drafter_decision_permits_serving,
    assert_no_legacy_activation_evidence,
    build_arc_services,
    load_drafter_model_decision,
)
from contextplane.arc.workers.audit_drain import (
    AuditDrainWorker,
    DrainResult,
)
from contextplane.arc.workers.challenge_cleanup import (
    ChallengeCleanupWorker,
    CleanupResult,
)
from contextplane.arc.workers.checkpoint_exporter import (
    CheckpointExporterWorker,
    CheckpointExportResult,
)
from contextplane.arc.workers.observation_fingerprint_reaper import (
    ObservationFingerprintReaperResult,
    ObservationFingerprintReaperWorker,
)
from contextplane.arc.workers.observation_window_evaluator import (
    ObservationWindowEvaluatorResult,
    ObservationWindowEvaluatorWorker,
)
from contextplane.arc.workers.review_expiry import (
    ReviewExpiryResult,
    ReviewExpiryWorker,
)
from contextplane.arc.workers.source_status_refresh import (
    SourceStatusRefreshResult,
    SourceStatusRefreshWorker,
)

__all__ = [
    "APPROVAL_VERIFIER_ENROLLMENT_PROFILE",
    "ARTIFACT_SEMANTICS_PROFILE",
    "ActivationEligibility",
    "ActivationError",
    "ActivationPredicateFailed",
    "ActivationRequestMismatch",
    "ActivationService",
    "ActorNotCallerSupplied",
    "ApprovalChallengeService",
    "ApprovalProof",
    "ApprovalTrustService",
    "ArcAuthorizationError",
    "ArcAuthorizationService",
    "ArcRequestContext",
    "ArcServices",
    "ArcVocabularyError",
    "ArtifactFamily",
    "ArtifactLifecycleError",
    "ArtifactMaterialisationService",
    "ArtifactService",
    "AttestationEnvelope",
    "AttestationProofInput",
    "AttestationService",
    "AuditDrainWorker",
    "AuthorityScope",
    "BaselineDiff",
    "CANONICAL_PROFILE_VERSIONS",
    "CandidateSemanticsMissing",
    "ChallengeCleanupWorker",
    "ChallengeService",
    "CheckpointExportResult",
    "CheckpointExportService",
    "CheckpointExporterWorker",
    "CleanupResult",
    "ConnectorFetchAdmission",
    "ConnectorRegistration",
    "ConnectorRow",
    "CorpusReader",
    "DELTA_CODES",
    "DetachedSignatureProofInput",
    "DetailDenied",
    "DetailIdempotencyConflict",
    "DetailRequest",
    "DrafterModelDisabled",
    "DrafterService",
    "DrainResult",
    "EXPECTED_IMPACT_ENVELOPE_PROFILE",
    "EnrollmentChallengeRequired",
    "EnrollmentError",
    "EnrollmentService",
    "EnrollmentVerificationFailed",
    "EnvelopeInvalid",
    "EvidenceTypeNotWritableError",
    "ExceptionApproval",
    "ExceptionDraft",
    "ExceptionNotPermitted",
    "ExceptionService",
    "IdempotencyConflict",
    "JitService",
    "KIND_PROVIDER",
    "ManifestClaims",
    "ManifestUnverified",
    "OBSERVATION_CLASS_PREDICATE_PROFILE",
    "ObservationFailed",
    "ObservationFingerprintReaperResult",
    "ObservationFingerprintReaperWorker",
    "ObservationInsufficient",
    "ObservationStatus",
    "ObservationWindowEvaluatorResult",
    "ObservationWindowEvaluatorWorker",
    "OperationalChainIdempotencyConflict",
    "OperationalChainIntegrityError",
    "OperationalChainService",
    "PreflightError",
    "PreflightRegistry",
    "ProofInput",
    "ProposalService",
    "ProposalStateConflict",
    "ProposalThread",
    "ProposalVersion",
    "ProvenanceInvalid",
    "ProvenanceService",
    "QualificationActorInvalid",
    "QualificationComputation",
    "QualificationService",
    "QualificationUnavailable",
    "RISK_CLASSIFICATIONS",
    "ReachConfirmationRecord",
    "ReceiptReader",
    "ReceiptService",
    "ReceiptSigningProvider",
    "ReplayCorpusApprovalConflict",
    "ReplayCorpusRow",
    "ReplayCorpusService",
    "ResolutionRequest",
    "ResolutionService",
    "ReviewExpiryResult",
    "ReviewExpiryWorker",
    "ReviewPackage",
    "ReviewPackageIntegrityError",
    "ReviewPackageService",
    "ReviewPackageUnavailable",
    "RevisionActivation",
    "RevisionIntegrityService",
    "RiskClassificationError",
    "RiskEnvelopeValidator",
    "SCHEMA_BY_PROFILE",
    "SOURCE_APPROVAL_CLAIM_PROFILE",
    "SemanticTestResult",
    "SemanticTestService",
    "SemanticsValidationFailed",
    "ShadowError",
    "ShadowService",
    "SourceAdmissionRefused",
    "SourceAdmissionService",
    "SourceEvidence",
    "SourceIdempotencyConflict",
    "SourceStatusRefreshResult",
    "SourceStatusRefreshWorker",
    "SourceStatusService",
    "SourceStatusUnavailable",
    "SubmissionPrerequisiteUnavailable",
    "UploadAdmission",
    "UploadPolicyRegistration",
    "UploadPolicyRow",
    "VerifierRegistry",
    "VerifierRow",
    "approval_challenge",
    "approval_challenge_verification",
    "assert_drafter_decision_permits_serving",
    "assert_no_legacy_activation_evidence",
    "build_arc_services",
    "credential_fingerprint",
    "iter_upload_file",
    "load_drafter_model_decision",
    "manifest_claims_digest",
    "new_connection_id",
    "parse_manifest",
    "restriction_digest",
]
