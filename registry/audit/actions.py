"""Canonical audit action name constants used across audit callsites."""

from typing import Final

__all__ = [
    "CLAIM_CONSOLIDATED_ADD",
    "CLAIM_CONSOLIDATED_UPDATE",
    "CLAIM_CONSOLIDATED_NOOP",
    "CLAIM_SUPERSEDED",
    "CLAIM_CLUSTER_COLLAPSED",
    "CLAIM_CONTESTED",
    "CLAIM_CONTAINMENT_REFUSED",
    "CLAIM_CONFIRMED",
    "CLAIM_ADJUDICATED",
    "CLAIM_PROPOSAL_ROUTED",
    "CLAIM_PROMOTION_PROPOSED",
    "CLAIM_PROMOTED",
    "CLAIM_PROMOTION_REJECTED",
    "CLAIM_PROMOTION_REVERSED",
    "CLAIM_AUTOPROMOTE_ALLOWED",
    "CLAIM_AUTOPROMOTE_REVOKED",
    "REQUEST_RAISED",
    "REQUEST_ACKNOWLEDGED",
    "REQUEST_ACCEPTED",
    "REQUEST_DECLINED",
    "REQUEST_MARKED_DUPLICATE",
    "REQUEST_RESOLVED",
    "REQUEST_TRANSITIONED",
    "REQUEST_LINKED_TO_CHANGE",
    "SOURCE_AUTHORITY_DECLARED",
    "SOURCE_BREAKER_OPENED",
    "ARC_CHALLENGE_ISSUED",
    "ARC_CHALLENGE_CONSUMED",
    "ARC_CHALLENGE_EXPIRED",
    "ARC_CONTEXT_RESOLVED",
    "ARC_CONTEXT_BLOCKED",
    "ARC_CONTEXT_DEGRADED",
    "ARC_MANIFEST_UNVERIFIED",
    "ARC_JIT_GRANTED",
    "ARC_JIT_DENIED",
    "ARC_JIT_ATTEMPT_REJECTED",
    "ARC_ARTIFACT_REGISTERED",
    "ARC_ARTIFACT_ACTIVATED",
    "ARC_ARTIFACT_REVOKED",
    "ARC_ARTIFACT_SUPERSEDED",
    "ARC_ARTIFACT_INVALIDATED",
    "ARC_EXCEPTION_APPROVED",
    "ARC_EXCEPTION_REVOKED",
    "ARC_APPROVAL_VERIFIER_REGISTERED",
    "ARC_APPROVAL_VERIFIER_REVOKED",
    "ARC_APPROVAL_EVIDENCE_REVOKED",
    "ARC_HOST_KEY_REGISTERED",
    "ARC_HOST_KEY_REVOKED",
    "ARC_RECEIPT_INTEGRITY_FAILED",
    "ARC_REVIEW_EXPIRED",
    "ARC_CONTENT_DELETION_VERIFIED",
    "ENTITY_UPDATED",
    "ENTITY_DELETED",
    "ADOPTION_REVOKED",
    "ENTITY_VISIBILITY_SET",
    "EXTERNAL_ID_DELETED",
    "PROGRESSION_DEFINITION_PUBLISHED",
    "PROGRESSION_DEFINITION_SOFT_DELETED",
    "PROGRESSION_OVERRIDE_CREATED",
    "PROGRESSION_TRANSITION_ACCEPTED",
    "PROGRESSION_TRANSITION_REJECTED",
    "PROGRESSION_TRANSITION_WARNED",
    "PROGRESSION_TRANSITION_OVERRIDDEN",
    # Workspace actions
    "WORKSPACE_CREATED",
    "WORKSPACE_UPDATED",
    "WORKSPACE_DELETED",
    # Workspace entry actions
    "WORKSPACE_ENTRY_CREATED",
    "WORKSPACE_ENTRY_UPDATED",
    "WORKSPACE_ENTRY_DELETED",
    # Workspace share actions
    "WORKSPACE_SHARE_GRANTED",
    "WORKSPACE_SHARE_REVOKED",
    # Workspace expiry worker
    "WORKSPACE_ENTRY_EXPIRED",
    # Right-to-be-forgotten physical purge
    "RTBF_PURGE",
]

ENTITY_UPDATED: Final[str] = "entity.updated"
ENTITY_DELETED: Final[str] = "entity.deleted"
ADOPTION_REVOKED: Final[str] = "adoption.revoked"
ENTITY_VISIBILITY_SET: Final[str] = "entity.visibility_set"
EXTERNAL_ID_DELETED: Final[str] = "external_id.deleted"
PROGRESSION_DEFINITION_PUBLISHED: Final[str] = "progression.definition.published"
PROGRESSION_DEFINITION_SOFT_DELETED: Final[str] = "progression.definition.soft_deleted"
PROGRESSION_OVERRIDE_CREATED: Final[str] = "progression.override.created"
PROGRESSION_TRANSITION_ACCEPTED: Final[str] = "progression.transition.accepted"
PROGRESSION_TRANSITION_REJECTED: Final[str] = "progression.transition.rejected"
PROGRESSION_TRANSITION_WARNED: Final[str] = "progression.transition.warned"
PROGRESSION_TRANSITION_OVERRIDDEN: Final[str] = "progression.transition.overridden"

# Workspace lifecycle actions — used by WorkspaceService.
# Named noun.verb to match the registry-wide audit action convention.
WORKSPACE_CREATED: Final[str] = "workspace.created"
WORKSPACE_UPDATED: Final[str] = "workspace.updated"
WORKSPACE_DELETED: Final[str] = "workspace.deleted"

# Workspace entry lifecycle actions — used by WorkspaceService entry CRUD methods.
WORKSPACE_ENTRY_CREATED: Final[str] = "workspace.entry.created"
WORKSPACE_ENTRY_UPDATED: Final[str] = "workspace.entry.updated"
WORKSPACE_ENTRY_DELETED: Final[str] = "workspace.entry.deleted"

# Workspace share lifecycle actions — used by WorkspaceService share methods.
WORKSPACE_SHARE_GRANTED: Final[str] = "workspace.share.granted"
WORKSPACE_SHARE_REVOKED: Final[str] = "workspace.share.revoked"

# Workspace expiry worker — emitted per batch of soft-invalidated entries.
# Tenant context is synthetic (system actor) because the worker spans all tenants.
WORKSPACE_ENTRY_EXPIRED: Final[str] = "workspace.entry.expired"

# Right-to-be-forgotten physical purge. Cross-cutting concern; noun.verb taxonomy
# uses the operation's domain ("rtbf") rather than the service that executes it,
# because future phases may consolidate purge operations across content tables.
RTBF_PURGE: Final[str] = "rtbf.purge"

# ---------------------------------------------------------------------------
# ARC — attested context resolution
# ---------------------------------------------------------------------------
#
# The vocabulary gate (tests/conformance/test_audit_action_vocabulary.py) fails
# on any bare string literal in an `action=` kwarg, so every ARC audit event
# needs a constant here before the emitting code can be written.
#
# ARC does not write audit_log inline the way the rest of the codebase does:
# these names are carried on arc_audit_outbox rows and reach audit_log through
# the drain worker. The name is what an auditor greps for, so it must not depend
# on which path delivered it.

# Challenge lifecycle. Issued and consumed are separate events on purpose —
# a challenge issued but never consumed is a signal, not an absence.
ARC_CHALLENGE_ISSUED: Final[str] = "arc.challenge.issued"
ARC_CHALLENGE_CONSUMED: Final[str] = "arc.challenge.consumed"
ARC_CHALLENGE_EXPIRED: Final[str] = "arc.challenge.expired"

# Context resolution outcomes. One per resolution status, so a count by action
# answers "how often are agents blocked" without parsing payloads.
ARC_CONTEXT_RESOLVED: Final[str] = "arc.context.resolved"
ARC_CONTEXT_BLOCKED: Final[str] = "arc.context.blocked"
ARC_CONTEXT_DEGRADED: Final[str] = "arc.context.degraded"

# A rejected attempt: no trusted attestation, so no receipt exists to carry
# the record. Separate from the three outcomes above because those all
# describe a request that *was* authenticated — this one never was, and a
# spike in it is an attack signal rather than a policy signal.
ARC_MANIFEST_UNVERIFIED: Final[str] = "arc.manifest.unverified"

# Just-in-time detail retrieval. A denial is as auditable as a grant: it is the
# evidence that authorization held.
ARC_JIT_GRANTED: Final[str] = "arc.jit.granted"
ARC_JIT_DENIED: Final[str] = "arc.jit.denied"

# An attempt that never reached an authorization decision — an invalid or
# replayed page token, or a reused idempotency key. Separate from
# `jit.denied` because that one describes a caller who *was* evaluated and
# refused; this one could not safely be allowed to touch the receipt chain
# at all, so it exists only in the audit trail.
ARC_JIT_ATTEMPT_REJECTED: Final[str] = "arc.jit.attempt_rejected"

# Governed artifact lifecycle. Registration and activation are distinct because
# only activation makes a revision selectable.
ARC_ARTIFACT_REGISTERED: Final[str] = "arc.artifact.registered"
ARC_ARTIFACT_ACTIVATED: Final[str] = "arc.artifact.activated"
ARC_ARTIFACT_REVOKED: Final[str] = "arc.artifact.revoked"
ARC_ARTIFACT_SUPERSEDED: Final[str] = "arc.artifact.superseded"
ARC_ARTIFACT_INVALIDATED: Final[str] = "arc.artifact.invalidated"

# Approved lower-scope exceptions to a delegable directive.
ARC_EXCEPTION_APPROVED: Final[str] = "arc.exception.approved"
ARC_EXCEPTION_REVOKED: Final[str] = "arc.exception.revoked"

# Approval-trust administration. Verifier revocation cascades to every active
# projection that depended on it, so it is audited in its own right.
ARC_APPROVAL_VERIFIER_REGISTERED: Final[str] = "arc.approval_verifier.registered"
ARC_APPROVAL_VERIFIER_REVOKED: Final[str] = "arc.approval_verifier.revoked"
ARC_APPROVAL_EVIDENCE_REVOKED: Final[str] = "arc.approval_evidence.revoked"

# Host attestation keys. Revocation is ordered against receipt creation by a row
# lock, and the audit event is what makes that ordering reviewable afterwards.
ARC_HOST_KEY_REGISTERED: Final[str] = "arc.host_key.registered"
ARC_HOST_KEY_REVOKED: Final[str] = "arc.host_key.revoked"

# A receipt whose event chain no longer verifies. Terminal: the receipt cannot
# authorize actions afterwards, so this is the last thing said about it.
ARC_RECEIPT_INTEGRITY_FAILED: Final[str] = "arc.receipt.integrity_failed"

# Mandatory review expiry, materialized by the lifecycle worker. Request-time
# checks remain authoritative; this records the materialization.
ARC_REVIEW_EXPIRED: Final[str] = "arc.review.expired"

# Physical deletion, key destruction, or legal-hold release against governed
# content — the evidence that a deletion actually happened.
ARC_CONTENT_DELETION_VERIFIED: Final[str] = "arc.content.deletion_verified"


# --- staged-claim consolidation ---------------------------------------------
#
# Every consolidation decision writes exactly one row. That includes the decision
# to do nothing: a sweep that audited only its changes would be indistinguishable
# from a sweep that never ran, and "why was this claim left alone" is a question
# somebody eventually asks.
#
# Named for the decision rather than for the mechanism, because the decision is
# what a reviewer is looking for. A single "consolidated" action with the outcome
# buried in a payload would make "show me every supersession" a text search.
CLAIM_CONSOLIDATED_ADD: Final[str] = "claim.consolidated_add"
CLAIM_CONSOLIDATED_UPDATE: Final[str] = "claim.consolidated_update"
CLAIM_CONSOLIDATED_NOOP: Final[str] = "claim.consolidated_noop"

# One claim closed in favour of another. Distinct from the update that caused it:
# an update names the claim that arrived, a supersession names the claim that
# stopped being current, and a reviewer asking "what did we stop believing" wants
# the second.
CLAIM_SUPERSEDED: Final[str] = "claim.superseded"

# Near-duplicate phrasings collapsed to one surviving claim with merged
# provenance. Separate from a supersession because nothing was contradicted --
# the claims agreed, and the collapse is about volume rather than truth.
CLAIM_CLUSTER_COLLAPSED: Final[str] = "claim.cluster_collapsed"

# Two claims that cannot both hold. Recorded even though neither is removed:
# a disagreement is the event, and its resolution is a later one.
CLAIM_CONTESTED: Final[str] = "claim.contested"

# A candidate refused because its content instructed a reader rather than
# describing a capability. Audited as well as counted, because a metric shows a
# rate and an investigation needs the individual attempt.
CLAIM_CONTAINMENT_REFUSED: Final[str] = "claim.containment_refused"

# A person put their name to a claim, or judged whether one turned out correct.
# Both are human acts on a specific claim, which is exactly what an audit log is
# for.
CLAIM_CONFIRMED: Final[str] = "claim.confirmed"
CLAIM_ADJUDICATED: Final[str] = "claim.adjudicated"

# A claim about another tenant's capability that conflicts with the owner's
# assertion. It does not supersede; it becomes something the owner is asked
# about, and this records the routing rather than an outcome.
CLAIM_PROPOSAL_ROUTED: Final[str] = "claim.proposal_routed"

# A claim queued for its owner to decide on. Distinct from the routed action above:
# routing names a claim that crossed a tenant boundary, which is a different fact
# about the proposal than that one exists.
CLAIM_PROMOTION_PROPOSED: Final[str] = "claim.promotion_proposed"

# The graph changed. This is the only action in the vocabulary that names a write
# outside the staging store.
CLAIM_PROMOTED: Final[str] = "claim.promoted"
CLAIM_PROMOTION_REJECTED: Final[str] = "claim.promotion_rejected"
CLAIM_PROMOTION_REVERSED: Final[str] = "claim.promotion_reversed"

# Guardrail configuration is itself audited: widening what may promote without review
# is a more consequential act than most individual promotions.
CLAIM_AUTOPROMOTE_ALLOWED: Final[str] = "claim.autopromote_allowed"
CLAIM_AUTOPROMOTE_REVOKED: Final[str] = "claim.autopromote_revoked"

# A consuming team asking an owning team for something. Named for the act rather than
# for the artefact, so the log reads as a history of what people did.
REQUEST_RAISED: Final[str] = "request.raised"
REQUEST_ACKNOWLEDGED: Final[str] = "request.acknowledged"
REQUEST_ACCEPTED: Final[str] = "request.accepted"
REQUEST_DECLINED: Final[str] = "request.declined"
REQUEST_MARKED_DUPLICATE: Final[str] = "request.marked_duplicate"
REQUEST_RESOLVED: Final[str] = "request.resolved"

# The fallback for a transition with no dedicated action. Present so an unmapped
# status still audits rather than silently writing nothing.
REQUEST_TRANSITIONED: Final[str] = "request.transitioned"

# The request produced a canonical change. This is the row that proves the loop
# closed rather than merely that somebody agreed it should.
REQUEST_LINKED_TO_CHANGE: Final[str] = "request.linked_to_change"

# A source declared what its claims are worth before it was allowed to write any.
SOURCE_AUTHORITY_DECLARED: Final[str] = "source.authority_declared"

# A connector exceeded its ingest ceiling and was cut off. Audited rather than only
# counted: a breaker that opened and left no record looks like a quiet outage.
SOURCE_BREAKER_OPENED: Final[str] = "source.breaker_opened"
