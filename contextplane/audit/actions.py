"""Canonical audit action name constants used across audit callsites."""

from typing import Final

__all__ = [
    "CLAIM_CONSOLIDATED_ADD",
    "CLAIM_CONSOLIDATED_UPDATE",
    "CLAIM_CONSOLIDATED_NOOP",
    "CLAIM_SUPERSEDED",
    "CLAIM_CONTESTED",
    "CLAIM_CONTAINMENT_REFUSED",
    "CONTEXT_ADMISSION_REFUSED",
    "CLAIM_CONFIRMED",
    "CLAIM_ADJUDICATED",
    "CLAIM_LINKED",
    "CLAIM_DISCARDED",
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
    "SIGNAL_INGESTED",
    "SIGNAL_REJECTED",
    "SOURCE_AUTHORITY_DECLARED",
    "SOURCE_BREAKER_OPENED",
    "PROMOTION_POLICY_SET",
    "ARC_CHALLENGE_ISSUED",
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
    "ARC_ARTIFACT_INVALIDATED",
    "ARC_EXCEPTION_APPROVED",
    "ARC_EXCEPTION_REVOKED",
    "ARC_APPROVAL_VERIFIER_REGISTERED",
    "ARC_APPROVAL_VERIFIER_REVOKED",
    "ARC_APPROVAL_EVIDENCE_REVOKED",
    "ARC_VERIFIER_ENROLLMENT_CHALLENGE_ISSUED",
    "ARC_RECEIPT_INTEGRITY_FAILED",
    "ARC_REVIEW_EXPIRED",
    "ARC_ARTIFACT_FAMILY_CREATED",
    "ARC_PROPOSAL_OPENED",
    "ARC_PROPOSAL_WITHDRAWN",
    "ARC_PROPOSAL_REJECTED",
    "ARC_PROPOSAL_SUPERSEDED",
    "ARC_PROPOSAL_SUBMITTED",
    "ARC_PROPOSAL_ACTIVATED",
    "ARC_PROPOSAL_STALE",
    "ARC_FIELD_PROVENANCE_UPDATED",
    "ARC_SEMANTIC_TESTS_EXECUTED",
    "ARC_APPROVAL_CHALLENGE_ISSUED",
    "ARC_APPROVAL_CHALLENGE_FAILED",
    "ARC_PROJECTION_APPROVAL_EVIDENCE_RECORDED",
    "ARC_REACH_CONFIRMATION_UPDATED",
    "ARC_SOURCE_STATUS_REVOKED",
    "ARC_SOURCE_STATUS_EXPIRED",
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
    # Workspace expiry worker
    "WORKSPACE_ENTRY_EXPIRED",
    # Right-to-be-forgotten physical purge
    "RTBF_PURGE",
]

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

# Workspace expiry worker — emitted per batch of soft-invalidated entries.
# Tenant context is synthetic (system actor) because the worker spans all tenants.
WORKSPACE_ENTRY_EXPIRED: Final[str] = "workspace.entry.expired"

# Right-to-be-forgotten physical purge. Cross-cutting concern; noun.verb taxonomy
# uses the operation's domain ("rtbf") rather than the service that executes it,
# because future phases may consolidate purge operations across content tables.
# Past-tense value ("purged") to match every other action in this vocabulary —
# an audit log records what happened, not an instruction to act.
RTBF_PURGE: Final[str] = "rtbf.purged"

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

# Challenge lifecycle. `issued` is the request-side signal: a host asking for
# a challenge and never following through is visible the moment it happens,
# not only after the cleanup worker eventually catches up to it. `expired`
# is that worker's own trace of what it purged.
#
# There is no `arc.challenge.consumed` here. A consumed challenge is exactly
# a challenge that backs a receipt, and `arc_receipts.challenge_id` is a
# NOT NULL UNIQUE foreign key -- there is no state a dedicated consumed event
# could report that the receipt it was consumed for, plus whichever of
# `arc.context.resolved`/`arc.context.blocked`/`arc.context.degraded` that
# same transaction already emits, does not already say. The two are not
# merely correlated: `ChallengeService.consume_challenge` and that
# resolution-outcome event are called back to back in the same commit, so a
# consumed challenge with no resolution event, or a resolution event with no
# consumption, cannot occur. A separate event here would restate the
# receipt, not add to it.
ARC_CHALLENGE_ISSUED: Final[str] = "arc.challenge.issued"
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
ARC_ARTIFACT_INVALIDATED: Final[str] = "arc.artifact.invalidated"

# Approved lower-scope exceptions to a delegable directive.
ARC_EXCEPTION_APPROVED: Final[str] = "arc.exception.approved"
ARC_EXCEPTION_REVOKED: Final[str] = "arc.exception.revoked"

# Approval-trust administration. Verifier revocation cascades to every active
# projection that depended on it, so it is audited in its own right.
ARC_APPROVAL_VERIFIER_REGISTERED: Final[str] = "arc.approval_verifier.registered"
ARC_APPROVAL_VERIFIER_REVOKED: Final[str] = "arc.approval_verifier.revoked"
ARC_APPROVAL_EVIDENCE_REVOKED: Final[str] = "arc.approval_evidence.revoked"

# D1's proof-of-possession challenge, issued before any verifier row exists.
# Successful completion reuses ARC_APPROVAL_VERIFIER_REGISTERED above -- the
# resulting row is the same fact whether an operator registered it directly
# or a challenge/proof round trip produced it -- so only this earlier,
# challenge-specific step needs its own action.
ARC_VERIFIER_ENROLLMENT_CHALLENGE_ISSUED: Final[str] = "arc.verifier_enrollment_challenge.issued"

# A receipt whose event chain no longer verifies. Terminal: the receipt cannot
# authorize actions afterwards, so this is the last thing said about it.
ARC_RECEIPT_INTEGRITY_FAILED: Final[str] = "arc.receipt.integrity_failed"

# Mandatory review expiry, materialized by the lifecycle worker. Request-time
# checks remain authoritative; this records the materialization.
ARC_REVIEW_EXPIRED: Final[str] = "arc.review.expired"

# Artifact families and proposal threads. Distinct from
# ARC_ARTIFACT_REGISTERED above: that one records an already-approved
# revision landing under an existing family; this one records the family
# itself coming into existence, the first write `arc_artifacts` ever had.
ARC_ARTIFACT_FAMILY_CREATED: Final[str] = "arc.artifact_family.created"

# Proposal-version lifecycle. `opened` covers both a brand-new thread's
# first version and a later version opened after a prior one went terminal
# -- the state machine treats both identically, so the audit trail does
# too. Withdrawn/rejected/superseded are separate actions rather than one
# generic "proposal.terminalized", because a reviewer scanning the log
# wants to know which of the three happened without opening the payload.
ARC_PROPOSAL_OPENED: Final[str] = "arc.proposal.opened"
ARC_PROPOSAL_WITHDRAWN: Final[str] = "arc.proposal.withdrawn"
ARC_PROPOSAL_REJECTED: Final[str] = "arc.proposal.rejected"
ARC_PROPOSAL_SUPERSEDED: Final[str] = "arc.proposal.superseded"

# `approved -> activated`: the ten-predicate atomic activation transaction's
# own transition, distinct from `ARC_ARTIFACT_ACTIVATED` (which records the
# revision itself entering force) -- one activation call writes both facts,
# and a reviewer scanning the log gains from telling "the version's own
# state machine advanced" apart from "a revision started binding agents"
# even though today they always happen together, in the same transaction.
ARC_PROPOSAL_ACTIVATED: Final[str] = "arc.proposal.activated"

# ADR 041's one write-bearing predicate failure: a proposal version's bound
# risk reducer was retired before it reached a terminal state. Distinct from
# the four transitions above (all human- or evidence-driven); this one is a
# system-detected drift the activation attempt itself discovers.
ARC_PROPOSAL_STALE: Final[str] = "arc.proposal.stale"

# `POST {PV}/submit`'s materialisation transaction. Unreachable on every
# deployment today (see `contextplane.arc.service.submission`'s own docstring),
# but named now so the task that enables submission emits this action
# rather than inventing one at the moment it stops refusing.
ARC_PROPOSAL_SUBMITTED: Final[str] = "arc.proposal.submitted"

# `PATCH {PV}` field-provenance writes and `POST {PV}/semantic-tests` runs.
# Both record the touched identifiers (field paths / test ids) rather than
# the values themselves -- the audit row proves *that* an edit or a test
# run happened and what it touched, not a duplicate of the row it wrote.
ARC_FIELD_PROVENANCE_UPDATED: Final[str] = "arc.field_provenance.updated"
ARC_SEMANTIC_TESTS_EXECUTED: Final[str] = "arc.semantic_tests.executed"

# D2's two-call `artifact_activation` approval writer (`approval_challenge
# .py`). `ISSUED` covers challenge creation; `FAILED` is the third invalid
# signature attempt that terminalizes a challenge. A losing (superseded or
# expired) challenge emits neither -- it never mutated any state a caller
# needs an audit trail for. `RECORDED` is the one call that both writes
# `arc_projection_approval_evidence` and compare-and-swaps the bound
# proposal version to `approved`, in the same transaction. Unreachable on
# every deployment today -- see that module's own docstring -- but named
# now so the task that wires the real review-package digest chain emits
# these rather than inventing new ones at the moment it stops being dormant.
ARC_APPROVAL_CHALLENGE_ISSUED: Final[str] = "arc.approval_challenge.issued"
ARC_APPROVAL_CHALLENGE_FAILED: Final[str] = "arc.approval_challenge.failed"
ARC_PROJECTION_APPROVAL_EVIDENCE_RECORDED: Final[str] = "arc.projection_approval_evidence.recorded"

# `POST {PV}/reach-confirmations` writes. Records the touched field paths,
# same reasoning as the two constants above.
ARC_REACH_CONFIRMATION_UPDATED: Final[str] = "arc.reach_confirmation.updated"

# `SourceStatusService.record_revocation`/`record_expiry`'s own cascade --
# the source-status flip, not the per-revision lifecycle transition
# (`ArtifactService.revoke` already owns `ARC_ARTIFACT_REVOKED` for the
# human-driven path; this is the source-triggered one, with a different
# initiator and a different set of facts worth recording: which source,
# how many dependent revisions the cascade touched, and why).
ARC_SOURCE_STATUS_REVOKED: Final[str] = "arc.source_status.revoked"
ARC_SOURCE_STATUS_EXPIRED: Final[str] = "arc.source_status.expired"


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

# A curator gave a subjectless claim a home, or refused it outright. Both are
# the queue's own decisions on an unlinked or staged claim, distinct from
# anything promotion or confirmation records about it afterward.
CLAIM_LINKED: Final[str] = "claim.linked"
CLAIM_DISCARDED: Final[str] = "claim.discarded"

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

# Written by the sweep alongside the ordinary promotion row when a promotion happened
# without a human reviewing it. Two rows rather than a flag on one, because the
# question "was this ever reviewed by a person" must be answerable from the action
# vocabulary alone, without parsing payloads.
CLAIM_AUTO_PROMOTED: Final[str] = "claim.auto_promoted"

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

# An operator configured a tenant's review posture -- the confidence floor, the
# blast-radius threshold, or the always-review list. Widening what promotes
# without review is a more consequential act than most individual promotions
# it governs, so the configuration change is recorded in the same log.
PROMOTION_POLICY_SET: Final[str] = "promotion_policy.set"

#: Content carrying a prohibited class was refused before it reached storage.
#: Distinct from the containment refusal above, which is about a claim's content
#: rather than its handling class -- an artifact or workspace refusal recorded
#: under a claim-shaped action would be filed where nobody auditing handling
#: classes would look for it.
CONTEXT_ADMISSION_REFUSED: Final = "context.admission_refused"

#: One external observation entered the signal ledger. Written for a submission
#: this call stored *and* for one it recognised as already stored -- the
#: `outcome` field tells them apart, because "a producer reported this" and "a
#: producer reported this for the second time" are different facts and an
#: auditor asking how often a source retries cannot answer it from a log that
#: recorded only the first of the two.
SIGNAL_INGESTED: Final[str] = "signal.ingested"

#: An observation was refused before it reached the ledger. Its own action rather
#: than an `error_code` on the line above: a refusal leaves no signal row, so the
#: ingested action would have nothing to point at, and the two questions an
#: operator asks -- what did this source record, and what is it being turned away
#: for -- are answered by different queries. The `reason_class` field carries
#: which of the closed set fired; the offending content never appears, because
#: the audit log is the one place a record is guaranteed to be retained.
SIGNAL_REJECTED: Final[str] = "signal.rejected"
