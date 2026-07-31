"""Canonical audit action name constants used across audit callsites."""

from typing import Final

__all__ = [
    "ARC_CHALLENGE_ISSUED",
    "ARC_CHALLENGE_CONSUMED",
    "ARC_CHALLENGE_EXPIRED",
    "ARC_CONTEXT_RESOLVED",
    "ARC_CONTEXT_BLOCKED",
    "ARC_CONTEXT_DEGRADED",
    "ARC_JIT_GRANTED",
    "ARC_JIT_DENIED",
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
    "ANNOTATION_CREATED",
    "ANNOTATION_TRIAGED",
    "ANNOTATION_DELETED",
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

ANNOTATION_CREATED: Final[str] = "annotation.created"
ANNOTATION_TRIAGED: Final[str] = "annotation.triaged"
ANNOTATION_DELETED: Final[str] = "annotation.deleted"
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

# Just-in-time detail retrieval. A denial is as auditable as a grant: it is the
# evidence that authorization held.
ARC_JIT_GRANTED: Final[str] = "arc.jit.granted"
ARC_JIT_DENIED: Final[str] = "arc.jit.denied"

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
