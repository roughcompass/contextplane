"""The closed REST/MCP wire contract for the authoring surface.

Every request, response, and MCP result component is transcribed here (and
in this module's siblings; see the split note below) before any router
exists. Route tasks import from ``contextplane.api.schemas.arc_authoring``
rather than inventing fields; the conformance test in
``tests/conformance/test_arc_authoring_schemas.py`` pins each model's
generated JSON Schema to a checked-in snapshot so none of them can drift
silently once a route starts depending on them.

**Split across sibling modules.** Appendix A.6 alone transcribes to
roughly seventy component classes plus eighteen enums; a single file
containing all of it exceeded this repo's file-size ceiling. The split
follows Appendix A.6's own section boundaries -- ``arc_authoring_enums``
(Appendix A.3), ``arc_authoring_shared`` (Appendix A.6's "Shared" section
plus the scalar wire types its preamble states), ``arc_authoring_profiles``
(the four profile-named aliases), ``arc_authoring_source_admission``,
``arc_authoring_proposals``, ``arc_authoring_approval``,
``arc_authoring_observation`` -- the same section headings Appendix A.6
itself uses, so a reader who knows which appendix section they need knows
which file to open. This mirrors the precedent already set by
`authoring_profiles.py`'s own split into `authoring_profiles.py` + sibling
`authoring_profile_shapes.py`: split by cohesion along a documented
boundary, not by an arbitrary line count, and keep one importable name at
the path callers already expect. Every name defined in every sibling is
re-exported from here, so ``from contextplane.api.schemas.arc_authoring import
X`` keeps working for any ``X`` regardless of which sibling module defines
it -- this module is the single public surface; the siblings are an
implementation detail route tasks do not need to know about.

**Wire model vs. canonicalization profile -- where the boundary sits.**
Four components (`arc_authoring_profiles.py`) are direct closed imports of
a canonicalization profile from
``contextplane.arc.schemas.authoring_profile_shapes``: ``SourceApprovalClaim``,
``ArtifactSemantics``, ``ObservationClassPredicate``, and
``ExpectedImpactEnvelope`` (plus ``ArtifactSemanticsPartial``, the same
field set with everything optional for a patch). Hand-duplicating a
profile's field list here would create a second place that same set could
silently drift from -- so this module does not do that. It also does not
generate a Pydantic model purely mechanically from the profile's schema
dict: that dict is a validator's internal representation (JSON-Schema-
shaped, with `x-array-kind`/`x-order-key` extensions no OpenAPI tool
reads), not a wire contract, and mechanically drawing a wire type from it
would produce either an under-typed passthrough (`dict[str, Any]`, which
fails the "every object is `additionalProperties: false`" rule Appendix
A.6 states for every component) or a bespoke schema-to-Pydantic compiler
doing real work for four models. The chosen middle ground: each of the
four is a normal, explicitly typed Pydantic model -- so route handlers and
the generated OpenAPI document get real field names, enums, and patterns
-- and `test_arc_authoring_schemas.py` asserts that model's *top-level
field-name set* equals `authoring_profiles.profile_field_names()` for the
matching profile literal, via the `PROFILE_ALIASED_COMPONENTS` registry.
That assertion is what stands in for "derive": any field added to or
removed from the profile's closed schema, or from this model, fails the
test rather than silently drifting. It does not (yet) recurse into nested
shapes (a profile's `directives[]`/`applicability[]`/`items[]` element
schemas): those are hand-transcribed from the named private schema
constants in `authoring_profile_shapes.py` and cross-checked by hand
against that module rather than by an automated recursive assertion -- a
real residual gap, called out in `arc_authoring_profiles.py` rather than
hidden, and a reasonable target for a follow-up that exports nested
field-name helpers the same way `profile_field_names` already does for the
top level.

**Business-rule checks this module deliberately does not enforce.** A
handful of cross-field rules named in Appendix A already have a *specific*
named refusal code assigned to a later task's service layer:
`FieldProvenanceInput`'s three-way conditional requiredness maps to
`arc_provenance_invalid`, and `ExpectedImpactEnvelope`'s item non-overlap
rule maps to `arc_envelope_invalid` (both already implemented, for the
persisted canonical profiles, in `authoring_profiles.py`). If this module
enforced those same rules again at the Pydantic layer, a violating request
would fail FastAPI's generic validation-error path before ever reaching
the service that is supposed to own the specific code -- silently
reassigning someone else's contract. So those two rules are intentionally
absent here. Two *other* cross-field rules named in Appendix A have no
later task claiming a specific refusal code for them
(`owning_scope`/`target_tenant_id` requiredness, and
`EnrollmentChallengeRequest.binding_kind`'s conditional principal/provider
fields) -- those are safe to enforce here as ordinary shape validation
(see `_ScopeColumnsMixin` and `EnrollmentChallengeRequest`).

**Six enums are closed by reference, not restated as a literal list.**
`ArtifactKind`, `RevisionLifecycleState`, `RiskClassification`,
`SignatureAlgorithm`, and `DeltaCode` each already have exactly one
normative source elsewhere in the codebase; `arc_authoring_enums.py` says,
per class, exactly which one and whether the class derives from it by
import (so it cannot drift) or is a literal transcription (where the only
existing source is SQL in a migration, not an importable Python symbol).
The parity test that checks these five against their sources by name
(rather than against this module) is out of this task's path scope, and no
task in the current plan claims it yet. `ReasonCode` is the sixth named
vocabulary and is deliberately *not* a closed enum here: its stated source
(the ADR's transition-reason table) states transition authorities and
effects in prose, not an enumerated code list, and no later task has yet
materialized concrete `reason_code` string constants either. Inventing a
plausible-looking list here is exactly the failure mode this phase exists
to prevent. It is `str` until whichever task implements the transition
writers closes it.
"""

from __future__ import annotations

from pydantic import BaseModel

import contextplane.api.schemas.arc_authoring_approval as _approval
import contextplane.api.schemas.arc_authoring_enums as _enums
import contextplane.api.schemas.arc_authoring_observation as _observation
import contextplane.api.schemas.arc_authoring_profiles as _profiles
import contextplane.api.schemas.arc_authoring_proposals as _proposals
import contextplane.api.schemas.arc_authoring_shared as _shared
import contextplane.api.schemas.arc_authoring_source_admission as _source_admission
from contextplane.api.schemas.arc_authoring_approval import (
    ApprovalChallengeRequest,
    ApprovalChallengeResponse,
    ApprovalCompletionRequest,
    ApprovalEvidenceResponse,
    ApprovalVerifierResponse,
    EnrollmentChallengeRequest,
    EnrollmentChallengeResponse,
    ExceptionApprovalEvidenceRequest,
    ProjectionApprovalEvidenceResponse,
    VerifierRegistrationRequest,
)
from contextplane.api.schemas.arc_authoring_enums import (
    REFUSAL_CODE_STATUS,
    RESERVED_ACTOR_FIELDS,
    ActivationPredicateName,
    AdmissionMethod,
    ArtifactKind,
    AvailableAction,
    ChangeKind,
    DeltaCode,
    EvidenceType,
    ObservationDecision,
    OperationalIntegrityState,
    OwningScope,
    PrincipalBindingKind,
    ProposalState,
    ProvenanceClass,
    ReasonCode,
    RefusalCode,
    RevisionLifecycleState,
    RiskClassification,
    SignatureAlgorithm,
    SourceApprovalStatus,
    VerificationMethod,
)
from contextplane.api.schemas.arc_authoring_observation import (
    ActivateRequest,
    ActivationEligibilityResponse,
    ActivationPredicateStatus,
    DeltaCodeCounter,
    ObservationStatusResponse,
    PagedProposalSummaries,
    QualificationAcceptanceRequest,
    QualificationResponse,
    ReplayCorpusApprovalRequest,
    ReplayCorpusResponse,
    RevisionResponse,
)
from contextplane.api.schemas.arc_authoring_profiles import (
    PROFILE_ALIASED_COMPONENTS,
    ArtifactApplicabilityRule,
    ArtifactDirective,
    ArtifactSemantics,
    ArtifactSemanticsPartial,
    ExpectedImpactEnvelope,
    ExpectedImpactEnvelopeItem,
    ObservationClassPredicate,
    SourceApprovalClaim,
)
from contextplane.api.schemas.arc_authoring_proposals import (
    ArtifactFamilyCreate,
    ArtifactFamilyListResponse,
    ArtifactFamilyResponse,
    BaselineDiffChange,
    BaselineDiffResponse,
    DraftPatchResponse,
    DraftRequest,
    JudgmentAuthor,
    ProposalOpenRequest,
    ProposalPatchRequest,
    ProposalSummary,
    ProposalThreadResponse,
    ProposalVersionResponse,
    ReachConfirmationItem,
    ReachConfirmationRequest,
    ReachConfirmationResponse,
    ReviewPackageResponse,
    SemanticTestCase,
    SemanticTestRequest,
    SemanticTestResultItem,
    SemanticTestResultResponse,
    SubmitRequest,
    ValidationErrorItem,
    ValidationResponse,
)
from contextplane.api.schemas.arc_authoring_shared import (
    ActorRef,
    ApprovalProof,
    Base64Str,
    Citation,
    DetachedSignatureProof,
    Digest,
    EmptyRequest,
    FieldProvenance,
    FieldProvenanceInput,
    ReasonRequest,
    VerifierAttestationProof,
)
from contextplane.api.schemas.arc_authoring_source_admission import (
    ConnectorFetchRequest,
    SourceConnectorRegistration,
    SourceConnectorResponse,
    SourceEvidenceResponse,
    SourceUploadPolicyRegistration,
    SourceUploadPolicyResponse,
    UploadAdmissionRequest,
)

# ---------------------------------------------------------------------------
# Component registry. Every public component any sibling module defines,
# keyed by name -- the single list `test_arc_authoring_schemas.py` walks to
# check snapshot parity, example validity, and closedness, so a model added
# to any sibling is covered automatically rather than by remembering to
# also edit a test. Built by introspecting the sibling modules themselves
# (not `globals()` here) so it does not depend on this file's re-export
# list staying exhaustive.
# ---------------------------------------------------------------------------

_SIBLING_MODULES = (_shared, _enums, _profiles, _source_admission, _proposals, _approval, _observation)
_MIXIN_BASE_CLASSES = frozenset({_shared._ClosedModel, _shared._ScopeColumnsMixin})

COMPONENTS: dict[str, type[BaseModel]] = {
    name: obj
    for module in _SIBLING_MODULES
    for name, obj in vars(module).items()
    if isinstance(obj, type)
    and issubclass(obj, BaseModel)
    and obj.__module__ == module.__name__
    and obj not in _MIXIN_BASE_CLASSES
}

# Request-shaped components (and the input types nested inside them) that
# `RESERVED_ACTOR_FIELDS` applies to. Response-only components such as
# `ActorRef`, `FieldProvenance`, and `JudgmentAuthor` legitimately carry an
# issuer/subject/role and are excluded on purpose -- see Appendix A.4.
REQUEST_COMPONENTS: tuple[type[BaseModel], ...] = (
    EmptyRequest,
    ReasonRequest,
    DetachedSignatureProof,
    VerifierAttestationProof,
    FieldProvenanceInput,
    UploadAdmissionRequest,
    ConnectorFetchRequest,
    SourceConnectorRegistration,
    SourceUploadPolicyRegistration,
    ArtifactFamilyCreate,
    ProposalOpenRequest,
    ProposalPatchRequest,
    SemanticTestCase,
    SemanticTestRequest,
    ReachConfirmationRequest,
    DraftRequest,
    SubmitRequest,
    EnrollmentChallengeRequest,
    VerifierRegistrationRequest,
    ApprovalChallengeRequest,
    ApprovalCompletionRequest,
    ExceptionApprovalEvidenceRequest,
    QualificationAcceptanceRequest,
    ReplayCorpusApprovalRequest,
    ActivateRequest,
)


# ---------------------------------------------------------------------------
# Section 5.3's authorization-check <-> route-action table, frozen here so
# `AvailableAction` can be checked against it before any route exists. Two
# `AvailableAction` members correspond to a route that is not on the
# proposal-version resource itself (Appendix A.6): `request_approval` maps
# to the `create` action on `POST {PV}/approval-challenges`, and `activate`
# maps to the `activate` action on `POST /v1/arc/revisions/{id}/activate`.
# `tests/conformance/test_arc_authoring_openapi_parity.py` checks this
# mapping against the real, fully registered router; this module only
# freezes it.
# ---------------------------------------------------------------------------

AVAILABLE_ACTION_ROUTE_ACTIONS: dict[AvailableAction, str] = {
    AvailableAction.EDIT: "edit",
    AvailableAction.VALIDATE: "validate",
    AvailableAction.RUN_SEMANTIC_TESTS: "run",
    AvailableAction.CONFIRM_REACH: "confirm",
    AvailableAction.DRAFT: "draft",
    AvailableAction.SUBMIT: "submit",
    AvailableAction.WITHDRAW: "withdraw",
    AvailableAction.REJECT: "reject",
    AvailableAction.SUPERSEDE: "supersede",
    AvailableAction.REQUEST_APPROVAL: "create",
    AvailableAction.QUALIFY: "qualify",
    AvailableAction.ACCEPT_QUALIFICATION: "accept",
    AvailableAction.ACTIVATE: "activate",
}

AVAILABLE_ACTION_OFF_RESOURCE_EXCEPTIONS: frozenset[AvailableAction] = frozenset(
    {AvailableAction.REQUEST_APPROVAL, AvailableAction.ACTIVATE}
)


__all__ = [
    "AVAILABLE_ACTION_OFF_RESOURCE_EXCEPTIONS",
    "AVAILABLE_ACTION_ROUTE_ACTIONS",
    "COMPONENTS",
    "PROFILE_ALIASED_COMPONENTS",
    "REFUSAL_CODE_STATUS",
    "REQUEST_COMPONENTS",
    "RESERVED_ACTOR_FIELDS",
    "ActivateRequest",
    "ActivationEligibilityResponse",
    "ActivationPredicateName",
    "ActivationPredicateStatus",
    "ActorRef",
    "AdmissionMethod",
    "ApprovalChallengeRequest",
    "ApprovalChallengeResponse",
    "ApprovalCompletionRequest",
    "ApprovalEvidenceResponse",
    "ApprovalProof",
    "ApprovalVerifierResponse",
    "ArtifactApplicabilityRule",
    "ArtifactDirective",
    "ArtifactFamilyCreate",
    "ArtifactFamilyListResponse",
    "ArtifactFamilyResponse",
    "ArtifactKind",
    "ArtifactSemantics",
    "ArtifactSemanticsPartial",
    "AvailableAction",
    "Base64Str",
    "BaselineDiffChange",
    "BaselineDiffResponse",
    "ChangeKind",
    "Citation",
    "ConnectorFetchRequest",
    "DeltaCode",
    "DeltaCodeCounter",
    "DetachedSignatureProof",
    "Digest",
    "DraftPatchResponse",
    "DraftRequest",
    "EmptyRequest",
    "EnrollmentChallengeRequest",
    "EnrollmentChallengeResponse",
    "EvidenceType",
    "ExceptionApprovalEvidenceRequest",
    "ExpectedImpactEnvelope",
    "ExpectedImpactEnvelopeItem",
    "FieldProvenance",
    "FieldProvenanceInput",
    "JudgmentAuthor",
    "ObservationClassPredicate",
    "ObservationDecision",
    "ObservationStatusResponse",
    "OperationalIntegrityState",
    "OwningScope",
    "PagedProposalSummaries",
    "PrincipalBindingKind",
    "ProjectionApprovalEvidenceResponse",
    "ProposalOpenRequest",
    "ProposalPatchRequest",
    "ProposalState",
    "ProposalSummary",
    "ProposalThreadResponse",
    "ProposalVersionResponse",
    "ProvenanceClass",
    "QualificationAcceptanceRequest",
    "QualificationResponse",
    "ReachConfirmationItem",
    "ReachConfirmationRequest",
    "ReachConfirmationResponse",
    "ReasonCode",
    "ReasonRequest",
    "RefusalCode",
    "ReplayCorpusApprovalRequest",
    "ReplayCorpusResponse",
    "RevisionLifecycleState",
    "RevisionResponse",
    "ReviewPackageResponse",
    "RiskClassification",
    "SemanticTestCase",
    "SemanticTestRequest",
    "SemanticTestResultItem",
    "SemanticTestResultResponse",
    "SignatureAlgorithm",
    "SourceApprovalClaim",
    "SourceApprovalStatus",
    "SourceConnectorRegistration",
    "SourceConnectorResponse",
    "SourceEvidenceResponse",
    "SourceUploadPolicyRegistration",
    "SourceUploadPolicyResponse",
    "SubmitRequest",
    "UploadAdmissionRequest",
    "ValidationErrorItem",
    "ValidationResponse",
    "VerificationMethod",
    "VerifierAttestationProof",
    "VerifierRegistrationRequest",
]
