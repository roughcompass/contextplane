"""Observation, qualification, and activation components (Appendix A.6,
"Observation, qualification, activation" section), plus the one MCP-only
result type Appendix A.2 defines (`PagedProposalSummaries`).

Sibling of `arc_authoring.py`; see that module's docstring for why the
full Appendix A transcription is split across files.
"""

from __future__ import annotations

import datetime
import uuid

from pydantic import Field

from contextplane.api.schemas.arc_authoring_enums import (
    ActivationPredicateName,
    DeltaCode,
    ObservationDecision,
    OperationalIntegrityState,
    ReasonCode,
    RefusalCode,
    RevisionLifecycleState,
)
from contextplane.api.schemas.arc_authoring_proposals import ProposalSummary
from contextplane.api.schemas.arc_authoring_shared import ActorRef, Digest, _ClosedModel, _ScopeColumnsMixin


class DeltaCodeCounter(_ClosedModel):
    """One row of `ObservationStatusResponse.counters_by_delta_code`."""

    delta_code: DeltaCode
    count: int


class ObservationStatusResponse(_ClosedModel):
    """Aggregate observation-window counters for one cohort. Carries no
    tenant detail; tenant-scoped detail requires its own authorization and
    is served separately (Appendix A.1)."""

    cohort_id: uuid.UUID
    cohort_digest: Digest
    window_started_at: datetime.datetime
    window_deadline: datetime.datetime
    eligible_count: int
    observed_count: int
    counters_by_delta_code: list[DeltaCodeCounter]
    unexplained_count: int
    out_of_envelope_count: int
    computed_decision: ObservationDecision
    reason_codes: list[ReasonCode]


class QualificationResponse(_ClosedModel):
    """The computed (and, once accepted, acknowledged) observation-
    qualification decision for a candidate proposal version."""

    qualification_id: uuid.UUID
    decision: ObservationDecision
    candidate_review_package_digest: Digest
    baseline_revision_id: uuid.UUID | None = None
    cohort_digest: Digest
    expected_impact_envelope_digest: Digest
    replay_corpus_digest: Digest | None = None
    qualification_algorithm_version: str
    computed_at: datetime.datetime
    accepted_at: datetime.datetime | None = None
    accepted_by: ActorRef | None = None
    expires_at: datetime.datetime | None = None


class QualificationAcceptanceRequest(_ClosedModel):
    """Body for `POST {PV}/observation/accept`: the accepting principal's
    acknowledgement of the reason codes attached to the qualification."""

    qualification_id: uuid.UUID
    acknowledged_reason_codes: list[ReasonCode]


class ReplayCorpusApprovalRequest(_ScopeColumnsMixin, _ClosedModel):
    """Body for `POST /v1/arc/admin/observation-replay-corpora`: approves a
    generated replay corpus for use in qualification."""

    corpus_digest: Digest
    generator_version: str


class ReplayCorpusResponse(ReplayCorpusApprovalRequest):
    """`ReplayCorpusApprovalRequest` plus who approved it and when."""

    approved_at: datetime.datetime
    approved_by: ActorRef


class ActivationPredicateStatus(_ClosedModel):
    """One row of `ActivationEligibilityResponse.predicates`."""

    name: ActivationPredicateName
    satisfied: bool
    reason_code: RefusalCode | None = None


class ActivationEligibilityResponse(_ClosedModel):
    """`predicates[]` always contains all ten entries in fixed order, so a
    client cannot mistake an omitted predicate for a satisfied one.
    """

    eligible: bool
    predicates: list[ActivationPredicateStatus] = Field(min_length=10, max_length=10)


class ActivateRequest(_ClosedModel):
    """Body for `POST /v1/arc/revisions/{id}/activate`. `qualification_id`
    is required when the risk classification demands observation and
    forbidden otherwise (Appendix A.6)."""

    proposal_id: uuid.UUID
    proposal_version: int
    qualification_id: uuid.UUID | None = None


class RevisionResponse(_ClosedModel):
    """A revision's lifecycle and operational-integrity state."""

    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    lifecycle_state: RevisionLifecycleState
    operational_integrity_state: OperationalIntegrityState
    activated_at: datetime.datetime | None = None
    revoked_at: datetime.datetime | None = None


class PagedProposalSummaries(_ClosedModel):
    """MCP-only result for `arc_list_proposals` (Appendix A.2). Reuses
    `ProposalSummary`, the same component a REST list view would use, per
    Appendix A.2's "one definition site for both surfaces" rule.
    """

    items: list[ProposalSummary]
    next_cursor: str | None = None


__all__ = [
    "ActivateRequest",
    "ActivationEligibilityResponse",
    "ActivationPredicateStatus",
    "DeltaCodeCounter",
    "ObservationStatusResponse",
    "PagedProposalSummaries",
    "QualificationAcceptanceRequest",
    "QualificationResponse",
    "ReplayCorpusApprovalRequest",
    "ReplayCorpusResponse",
    "RevisionResponse",
]
