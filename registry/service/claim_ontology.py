"""The seeded claim ontology: what a claim is allowed to say.

A claim is `(subject, predicate, value)`, and the predicate decides what the
value may be. This module is the list of predicates the deployment ships with,
covering the five categories the requirement names.

**Why the list lives in code rather than a seed file.** Every other seeded
vocabulary is tenant data an operator curates. These are not: a global predicate
is deployment-wide, its type is validated against the closed catalog here, and
getting one wrong silently retypes every claim written against it. Keeping the
definitions next to the validation that enforces them means the two cannot drift
into disagreeing about what `deprecated_after` means.

**Every predicate declares units in its type, not its name.** `duration_seconds`
rather than `duration`, `bytes` rather than `size`. A predicate meaning minutes
stores 900, not 15. This is the failure mode the requirement calls out
specifically, and it is invisible once data exists: two tenants both storing
"15" under a predicate one of them reads as minutes.

**Seeding is idempotent and additive.** Re-running adds what is missing and
touches nothing that exists, because a predicate already in use cannot be
safely redefined — a changed type reinterprets history. Removing a predicate
from this list does not delete it either; retirement is deprecation through the
operator surface, which leaves the row in place so existing claims still
resolve.
"""

from __future__ import annotations

import dataclasses
import logging

from registry.exceptions import ConflictError
from registry.service.global_vocabulary import GlobalVocabularyService

_log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class PredicateSeed:
    value: str
    value_type: str
    claim_category: str
    definition: str


# Interface and contract — what a capability exposes and promises.
_INTERFACE: tuple[PredicateSeed, ...] = (
    PredicateSeed(
        "exposes_operation", "string", "interface_contract",
        "Names an operation the capability exposes on its public interface.",
    ),
    PredicateSeed(
        "interface_version", "version_predicate", "interface_contract",
        "The version or version range of the capability's published interface.",
    ),
    PredicateSeed(
        "interface_specification_url", "url", "interface_contract",
        "Absolute URL of the machine-readable interface specification.",
    ),
    PredicateSeed(
        "request_timeout_seconds", "duration_seconds", "interface_contract",
        "Timeout a caller should apply, in seconds. A predicate meaning minutes stores 900.",
    ),
    PredicateSeed(
        "max_request_bytes", "bytes", "interface_contract",
        "Largest request body the capability accepts, in bytes.",
    ),
    PredicateSeed(
        "is_publicly_callable", "boolean", "interface_contract",
        "Whether the capability may be called from outside its owning tenant.",
    ),
)

# Dependency — what a capability needs from others.
_DEPENDENCY: tuple[PredicateSeed, ...] = (
    PredicateSeed(
        "depends_on", "entity_ref", "dependency",
        "The subject requires the referenced capability to function.",
    ),
    PredicateSeed(
        "composes", "entity_ref", "dependency",
        "The subject is assembled from the referenced capability as a component.",
    ),
    PredicateSeed(
        "provides_to", "entity_ref", "dependency",
        "The subject serves the referenced capability as a provider.",
    ),
    PredicateSeed(
        "conflicts_with", "entity_ref", "dependency",
        "The subject and the referenced capability cannot both be adopted.",
    ),
    PredicateSeed(
        "depends_on_version", "version_predicate", "dependency",
        "The version range of a dependency the subject requires.",
    ),
)

# Ownership and stewardship — who answers for it.
_OWNERSHIP: tuple[PredicateSeed, ...] = (
    PredicateSeed(
        "owned_by_team", "string", "ownership_stewardship",
        "The team accountable for the capability.",
    ),
    PredicateSeed(
        "on_call_rotation", "string", "ownership_stewardship",
        "Identifier of the rotation to page for this capability.",
    ),
    PredicateSeed(
        "escalation_contact", "string", "ownership_stewardship",
        "Where to escalate when the owning team does not respond.",
    ),
    PredicateSeed(
        "steward_entity", "entity_ref", "ownership_stewardship",
        "An entity that stewards the subject without owning it outright.",
    ),
)

# Operational and lifecycle — how it behaves and where it is in its life.
_OPERATIONAL: tuple[PredicateSeed, ...] = (
    PredicateSeed(
        "lifecycle_state", "enum", "operational_lifecycle",
        "Lifecycle stage, resolved against the lifecycle vocabulary.",
    ),
    PredicateSeed(
        "deprecated_after", "timestamp_utc", "operational_lifecycle",
        "The instant after which the capability is deprecated. UTC; offsets are rejected.",
    ),
    PredicateSeed(
        "target_availability", "decimal", "operational_lifecycle",
        "Availability target as a fixed-point fraction, for example 0.999.",
    ),
    PredicateSeed(
        "recovery_time_objective_seconds", "duration_seconds", "operational_lifecycle",
        "Time to restore after failure, in seconds.",
    ),
    PredicateSeed(
        "deployment_environment", "string", "operational_lifecycle",
        "An environment the capability is deployed into.",
    ),
    PredicateSeed(
        "runbook_url", "url", "operational_lifecycle",
        "Absolute URL of the operational runbook.",
    ),
)

# Decision and rationale — why it is the way it is.
_DECISION: tuple[PredicateSeed, ...] = (
    PredicateSeed(
        "decision_record_url", "url", "decision_rationale",
        "Absolute URL of the decision record governing the subject.",
    ),
    PredicateSeed(
        "decided_at", "timestamp_utc", "decision_rationale",
        "When the governing decision was taken. UTC.",
    ),
    PredicateSeed(
        "supersedes_decision", "url", "decision_rationale",
        "A prior decision this one replaces.",
    ),
    PredicateSeed(
        "decision_status", "enum", "decision_rationale",
        "Status of the governing decision, resolved against the vocabulary.",
    ),
)

# Session summary — the one category permitted a prose value, because a
# conversation has no typed decomposition. Barred from promotion to the graph
# for exactly that reason: prose cannot be compared or contradicted.
_SESSION: tuple[PredicateSeed, ...] = (
    PredicateSeed(
        "session_summary", "prose", "session_summary",
        "A natural-language summary of a session. Never promoted to the graph.",
    ),
)

ONTOLOGY: tuple[PredicateSeed, ...] = (
    *_INTERFACE,
    *_DEPENDENCY,
    *_OWNERSHIP,
    *_OPERATIONAL,
    *_DECISION,
    *_SESSION,
)


@dataclasses.dataclass(frozen=True)
class SeedResult:
    created: tuple[str, ...]
    already_present: tuple[str, ...]
    # A predicate this deployment could not create because a tenant already
    # uses the name locally. Reported rather than skipped silently: it means a
    # tenant's private meaning is blocking the shared one, and somebody has to
    # reconcile them.
    blocked_by_local: tuple[str, ...]


async def seed_ontology(
    service: GlobalVocabularyService, *, ontology: tuple[PredicateSeed, ...] = ONTOLOGY
) -> SeedResult:
    """Create any predicate this deployment is missing. Idempotent.

    Never updates an existing predicate. One already in use has claims
    validated against its declared type, and changing that in place
    reinterprets all of them — retirement is deprecation plus a successor,
    through the operator surface.
    """
    existing = {p.value for p in await service.list_predicates()}
    created: list[str] = []
    present: list[str] = []
    blocked: list[str] = []

    for seed in ontology:
        if seed.value in existing:
            present.append(seed.value)
            continue
        try:
            await service.create_predicate(
                value=seed.value,
                value_type=seed.value_type,
                claim_category=seed.claim_category,
                definition=seed.definition,
            )
        except ConflictError:
            # A tenant already means something by this name. Promoting it would
            # retype their claims, so the collision rule refuses and the
            # reconciliation is a human decision.
            _log.warning(
                "claim_ontology.blocked_by_local_predicate: %s is defined locally in at least "
                "one tenant and cannot be promoted until that is reconciled",
                seed.value,
            )
            blocked.append(seed.value)
            continue
        created.append(seed.value)

    return SeedResult(
        created=tuple(created),
        already_present=tuple(present),
        blocked_by_local=tuple(blocked),
    )


__all__ = ["ONTOLOGY", "PredicateSeed", "SeedResult", "seed_ontology"]
