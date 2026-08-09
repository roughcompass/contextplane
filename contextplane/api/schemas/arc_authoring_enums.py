"""Closed enums for the authoring-surface wire contract.

Sibling of `arc_authoring.py`, split out because the two source tables in
Appendix A.3 -- twelve enums given as literal lists, plus six vocabularies
closed "by reference" to a source elsewhere in the codebase -- are one
cohesive unit (every wire model in the sibling modules types a field
against one of these) that was making the top-level module exceed the
repo's file-size ceiling on its own. See `arc_authoring.py`'s module
docstring for the derive-vs-transcribe reasoning behind the six
by-reference vocabularies below; this file only defines them.
"""

from __future__ import annotations

import enum

from contextplane.arc import (
    APPROVAL_VERIFIER_ENROLLMENT_PROFILE,
    DELTA_CODES,
    RISK_CLASSIFICATIONS,
    SCHEMA_BY_PROFILE,
)

type ReasonCode = str  # see arc_authoring.py's module docstring: intentionally not a closed enum yet

RESERVED_ACTOR_FIELDS: frozenset[str] = frozenset(
    {
        "actor_id",
        "actor_issuer",
        "actor_subject",
        "caller_issuer",
        "caller_subject",
        "authenticated_issuer",
        "authenticated_subject",
        "acting_principal",
        "role",
        "roles",
        "on_behalf_of",
    }
)


# ---------------------------------------------------------------------------
# Appendix A.3, first table -- literal values transcribed exactly as given.
# ---------------------------------------------------------------------------


class ProposalState(enum.StrEnum):
    """The closed proposal-version lifecycle state (ADR 040 Section 5)."""

    OPEN = "open"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    ACTIVATED = "activated"
    REJECTED = "rejected"
    STALE = "stale"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


class OperationalIntegrityState(enum.StrEnum):
    """Whether a revision's operational-chain checkpoint is trustworthy."""

    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class ProvenanceClass(enum.StrEnum):
    """The three mutually exclusive ways a semantic field can be justified."""

    SOURCE_BACKED = "source_backed"
    HUMAN_JUDGMENT = "human_judgment"
    SERVER_DERIVED = "server_derived"


class AdmissionMethod(enum.StrEnum):
    """How a source's bytes entered the system."""

    CONNECTOR_FETCH = "connector_fetch"
    AUTHORIZED_UPLOAD = "authorized_upload"


class VerificationMethod(enum.StrEnum):
    """How an `ApprovalProof` is checked: a detached signature or a
    trusted provider's attestation."""

    DETACHED_SIGNATURE = "detached_signature"
    VERIFIER_ATTESTATION = "verifier_attestation"


class PrincipalBindingKind(enum.StrEnum):
    """Whether an enrolled verifier is bound to one exact principal or to
    any principal a trusted provider vouches for."""

    EXACT_PRINCIPAL = "exact_principal"
    PROVIDER_DELEGATED = "provider_delegated"


class SourceApprovalStatus(enum.StrEnum):
    """The freshness state of a source's upstream approval."""

    CURRENT = "current"
    EXPIRED = "expired"
    REVOKED = "revoked"
    UNKNOWN = "unknown"
    OVERDUE = "overdue"


class OwningScope(enum.StrEnum):
    """Whether a resource is global or bound to one tenant."""

    GLOBAL = "global"
    TENANT = "tenant"


class ObservationDecision(enum.StrEnum):
    """The outcome of comparing observed behavior against a candidate's
    expected-impact envelope."""

    QUALIFIED = "qualified"
    INSUFFICIENT = "insufficient"
    FAILED = "failed"


class EvidenceType(enum.StrEnum):
    """The two kinds of approval evidence the system records."""

    ARTIFACT_ACTIVATION = "artifact_activation"
    EXCEPTION_APPROVAL = "exception_approval"


class ChangeKind(enum.StrEnum):
    """How one field changed between a baseline and a candidate revision."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


class AvailableAction(enum.StrEnum):
    """The closed set of actions a proposal-version read model may report
    as available to the caller; see `AVAILABLE_ACTION_ROUTE_ACTIONS`."""

    EDIT = "edit"
    VALIDATE = "validate"
    RUN_SEMANTIC_TESTS = "run_semantic_tests"
    CONFIRM_REACH = "confirm_reach"
    DRAFT = "draft"
    SUBMIT = "submit"
    WITHDRAW = "withdraw"
    REJECT = "reject"
    SUPERSEDE = "supersede"
    REQUEST_APPROVAL = "request_approval"
    QUALIFY = "qualify"
    ACCEPT_QUALIFICATION = "accept_qualification"
    ACTIVATE = "activate"


class ActivationPredicateName(enum.StrEnum):
    """The ten activation predicates, in their fixed evaluation order."""

    LATEST_VERSION = "latest_version"
    STATE_APPROVED = "state_approved"
    DIGEST_CHAIN = "digest_chain"
    BASELINE_CURRENT = "baseline_current"
    SOURCE_VALID = "source_valid"
    RISK_REPRODUCIBLE = "risk_reproducible"
    OBSERVATION_QUALIFIED = "observation_qualified"
    PROJECTION_EVIDENCE_VALID = "projection_evidence_valid"
    ACTOR_SEPARATION = "actor_separation"
    OPERATIONAL_INTEGRITY = "operational_integrity"


class RefusalCode(enum.StrEnum):
    """The twenty-six closed refusal codes; see `REFUSAL_CODE_STATUS` for
    each one's HTTP status (Appendix A.5)."""

    ARC_SOURCE_ADMISSION_REFUSED = "arc_source_admission_refused"
    ARC_SOURCE_STATUS_UNAVAILABLE = "arc_source_status_unavailable"
    ARC_EVIDENCE_TYPE_NOT_WRITABLE = "arc_evidence_type_not_writable"
    ARC_ENROLLMENT_CHALLENGE_REQUIRED = "arc_enrollment_challenge_required"
    ARC_ENROLLMENT_VERIFICATION_FAILED = "arc_enrollment_verification_failed"
    ARC_APPROVAL_CHALLENGE_EXPIRED = "arc_approval_challenge_expired"
    ARC_APPROVAL_CHALLENGE_FAILED = "arc_approval_challenge_failed"
    ARC_APPROVAL_CHALLENGE_SUPERSEDED = "arc_approval_challenge_superseded"
    ARC_APPROVAL_ALREADY_COMPLETED = "arc_approval_already_completed"
    ARC_APPROVAL_VERIFICATION_FAILED = "arc_approval_verification_failed"
    ARC_APPROVAL_CHALLENGE_LIMIT_REACHED = "arc_approval_challenge_limit_reached"
    ARC_PROPOSAL_STATE_CONFLICT = "arc_proposal_state_conflict"
    ARC_PROPOSAL_VALIDATION_FAILED = "arc_proposal_validation_failed"
    ARC_PROVENANCE_INVALID = "arc_provenance_invalid"
    ARC_REACH_CONFIRMATION_REQUIRED = "arc_reach_confirmation_required"
    ARC_ENVELOPE_INVALID = "arc_envelope_invalid"
    ARC_OBSERVATION_INSUFFICIENT = "arc_observation_insufficient"
    ARC_OBSERVATION_FAILED = "arc_observation_failed"
    ARC_QUALIFICATION_ACTOR_INVALID = "arc_qualification_actor_invalid"
    ARC_QUALIFICATION_EXPIRED = "arc_qualification_expired"
    ARC_ACTIVATION_PREDICATE_FAILED = "arc_activation_predicate_failed"
    ARC_OPERATIONAL_INTEGRITY_PENDING = "arc_operational_integrity_pending"
    ARC_OPERATIONAL_INTEGRITY_FAILED = "arc_operational_integrity_failed"
    ARC_DRAFTER_MODEL_DISABLED = "arc_drafter_model_disabled"
    ARC_IDEMPOTENCY_CONFLICT = "arc_idempotency_conflict"
    ARC_ACTOR_NOT_CALLER_SUPPLIED = "arc_actor_not_caller_supplied"


# The REST-status half of Appendix A.5's table. A conformance test asserts
# every code here maps to exactly the status Appendix A.5 states, and the
# canonical-examples test reuses this same mapping for every refusal
# example's (code, status) pair.
REFUSAL_CODE_STATUS: dict[RefusalCode, int] = {
    RefusalCode.ARC_SOURCE_ADMISSION_REFUSED: 400,
    RefusalCode.ARC_SOURCE_STATUS_UNAVAILABLE: 409,
    RefusalCode.ARC_EVIDENCE_TYPE_NOT_WRITABLE: 409,
    RefusalCode.ARC_ENROLLMENT_CHALLENGE_REQUIRED: 409,
    RefusalCode.ARC_ENROLLMENT_VERIFICATION_FAILED: 400,
    RefusalCode.ARC_APPROVAL_CHALLENGE_EXPIRED: 409,
    RefusalCode.ARC_APPROVAL_CHALLENGE_FAILED: 409,
    RefusalCode.ARC_APPROVAL_CHALLENGE_SUPERSEDED: 409,
    RefusalCode.ARC_APPROVAL_ALREADY_COMPLETED: 409,
    RefusalCode.ARC_APPROVAL_VERIFICATION_FAILED: 400,
    RefusalCode.ARC_APPROVAL_CHALLENGE_LIMIT_REACHED: 429,
    RefusalCode.ARC_PROPOSAL_STATE_CONFLICT: 409,
    RefusalCode.ARC_PROPOSAL_VALIDATION_FAILED: 422,
    RefusalCode.ARC_PROVENANCE_INVALID: 422,
    RefusalCode.ARC_REACH_CONFIRMATION_REQUIRED: 409,
    RefusalCode.ARC_ENVELOPE_INVALID: 422,
    RefusalCode.ARC_OBSERVATION_INSUFFICIENT: 409,
    RefusalCode.ARC_OBSERVATION_FAILED: 409,
    RefusalCode.ARC_QUALIFICATION_ACTOR_INVALID: 403,
    RefusalCode.ARC_QUALIFICATION_EXPIRED: 409,
    RefusalCode.ARC_ACTIVATION_PREDICATE_FAILED: 409,
    RefusalCode.ARC_OPERATIONAL_INTEGRITY_PENDING: 409,
    RefusalCode.ARC_OPERATIONAL_INTEGRITY_FAILED: 409,
    RefusalCode.ARC_DRAFTER_MODEL_DISABLED: 409,
    RefusalCode.ARC_IDEMPOTENCY_CONFLICT: 409,
    RefusalCode.ARC_ACTOR_NOT_CALLER_SUPPLIED: 400,
}


# ---------------------------------------------------------------------------
# Appendix A.3, second table -- closed *by reference*. See `arc_authoring.py`
# for which of these derive from an existing source by import (so they
# cannot drift) versus transcribe one (where the only existing source is
# not an importable Python symbol).
# ---------------------------------------------------------------------------


class ArtifactKind(enum.StrEnum):
    """Mirrors the `ck_arc_artifacts_kind` CHECK constraint on
    `arc_artifacts.kind` (`contextplane/storage/migrations/versions/
    0001_baseline_schema.py`) -- today's only source for this vocabulary.
    Transcribed rather than imported: that constraint is a raw SQL string
    in a migration, not an importable Python constant. This phase adds no
    new kind.
    """

    STANDARD = "standard"
    POLICY = "policy"
    ADR = "adr"
    RUNBOOK = "runbook"
    CAPABILITY_CONTRACT = "capability_contract"


class RevisionLifecycleState(enum.StrEnum):
    """Mirrors the `LIFECYCLE_*` constants in
    `contextplane.arc.service.artifact_integrity` (`draft`, `active`,
    `superseded`, `revoked`, `expired`). Transcribed rather than imported:
    `api/schemas/` does not depend on `arc/service/` anywhere else in this
    codebase, and importing a service module's constants here would be a
    new, one-off exception to that direction.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    EXPIRED = "expired"


RiskClassification = enum.StrEnum(  # type: ignore[misc]  # mypy needs a dict *literal* to infer members; this one is built from an imported tuple on purpose (see docstring below)
    "RiskClassification", {v.upper(): v for v in RISK_CLASSIFICATIONS}, module=__name__
)
RiskClassification.__doc__ = (
    "Derived by import from `authoring_profile_shapes.RISK_CLASSIFICATIONS` (ADR 041 Section 2) "
    "-- both live in the schema layer, so this is a same-layer import that cannot drift from that "
    "tuple, unlike the cross-layer import `RevisionLifecycleState`/`ArtifactKind` avoid."
)

DeltaCode = enum.StrEnum("DeltaCode", {v.upper(): v for v in DELTA_CODES}, module=__name__)  # type: ignore[misc]
DeltaCode.__doc__ = (
    "Derived by import from `authoring_profile_shapes.DELTA_CODES` (ADR 041 Section 4); see "
    "`RiskClassification` for why this is a same-layer import."
)

_SIGNATURE_ALGORITHMS: tuple[str, ...] = tuple(
    SCHEMA_BY_PROFILE[APPROVAL_VERIFIER_ENROLLMENT_PROFILE]["properties"]["signature_algorithm"]["enum"]
)
SignatureAlgorithm = enum.StrEnum(  # type: ignore[misc]
    "SignatureAlgorithm", {v.upper(): v for v in _SIGNATURE_ALGORITHMS}, module=__name__
)
SignatureAlgorithm.__doc__ = (
    "Derived by import from the `signature_algorithm` enum already closed on the "
    "`arc_approval_verifier_enrollment_v1` profile schema. Appendix A.3 names "
    "`arc/service/approval_trust.py` as this vocabulary's eventual home, but that module does not "
    "yet declare a registered algorithm set; re-point this derivation once it does."
)
