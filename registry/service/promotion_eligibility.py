"""Whether a claim may become canonical, and whether a human must look first.

Two separate questions, deliberately kept apart.

*Eligibility* asks whether the claim is well-formed enough to be promoted at all: is it
attached to a subject, settled, uncontested, above the floor, and of a kind the
canonical graph can hold. An ineligible claim is not rejected -- it stays in staging and
still serves. It simply has nowhere to go.

*High impact* asks whether the change is consequential enough that a person must decide.
This is the question confidence does not answer. Being certain that a capability is
about to be deprecated is a reason to make sure somebody sees it, not a reason to skip
review. Nothing here reads confidence, and a test holds that: the classifier's inputs
are the claim's consequences, not its score.

A claim can be eligible and high-impact at once. That combination is the normal path:
it may be promoted, by a human, after review.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.service import promotion_targets
from registry.service.authority import AUTHORITY_UNATTRIBUTED

# --- eligibility --------------------------------------------------------------

INELIGIBLE_UNLINKED: Final[str] = "no subject entity"
INELIGIBLE_NOT_SETTLED: Final[str] = "not consolidated"
INELIGIBLE_CONTESTED: Final[str] = "contested"
INELIGIBLE_BELOW_FLOOR: Final[str] = "below the tenant confidence floor"
INELIGIBLE_NO_TARGET: Final[str] = "no canonical target"
INELIGIBLE_UNATTRIBUTED: Final[str] = "no attributable source"
INELIGIBLE_ALREADY: Final[str] = "already promoted"

# --- high impact --------------------------------------------------------------

IMPACT_NARROWS_SURFACE: Final[str] = "narrows a surface others depend on"
IMPACT_BLAST_RADIUS: Final[str] = "blast radius exceeds the tenant threshold"
IMPACT_SUPERSEDES_CONFIRMED: Final[str] = "supersedes a human-confirmed claim"
IMPACT_CROSS_TENANT: Final[str] = "author is not the subject's owner"
IMPACT_CONTESTED: Final[str] = "contested or contradicted"
IMPACT_ALWAYS_REVIEW: Final[str] = "predicate is on the tenant's always-review list"

# The clause "would remove, deprecate, or narrow a surface other entities depend on"
# cannot be evaluated for an arbitrary predicate. A claim is an assertion about one
# property; whether that constitutes narrowing a surface is only decidable where the
# predicate actually denotes a dependency surface. Listing the subset is the honest
# form: the alternative is a check that quietly never fires while reading as though it
# governs everything.
#
# `lifecycle_state` and `deprecated_after` announce withdrawal directly.
# `interface_version` and `depends_on_version` narrow the range a consumer may rely on.
# `is_publicly_callable` removes reachability.
SURFACE_PREDICATES: Final[frozenset[str]] = frozenset(
    {
        "lifecycle_state",
        "deprecated_after",
        "interface_version",
        "depends_on_version",
        "is_publicly_callable",
        "exposes_operation",
    }
)

# Lifecycle states that withdraw a surface rather than extend it.
_WITHDRAWING_STATES: Final[frozenset[str]] = frozenset({"deprecated", "retired", "sunset"})


@dataclasses.dataclass(frozen=True)
class PromotionPolicy:
    """Per-tenant review policy. Absent means these defaults, which are the cautious
    ones: a low blast-radius threshold and no floor."""

    blast_radius_threshold: int = 5
    always_review: frozenset[str] = frozenset()
    confidence_floor: float = 0.0


@dataclasses.dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...]

    @property
    def blocked_by(self) -> str | None:
        return self.reasons[0] if self.reasons else None


@dataclasses.dataclass(frozen=True)
class ImpactAssessment:
    """Why a claim needs review, or that the question does not apply to it.

    `surface_evaluated` is the difference between "we checked and it does not narrow
    anything" and "this predicate does not describe a surface, so the question was
    never asked". Reporting the second as the first would claim a guarantee that was
    never checked.
    """

    reasons: tuple[str, ...]
    surface_evaluated: bool

    @property
    def high_impact(self) -> bool:
        return bool(self.reasons)


async def load_policy(session: AsyncSession, tenant_id: uuid.UUID) -> PromotionPolicy:
    row = (
        (
            await session.execute(
                text(
                    "SELECT blast_radius_threshold, always_review, confidence_floor "
                    "FROM memory_promotion_policy WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return PromotionPolicy()
    return PromotionPolicy(
        blast_radius_threshold=int(row["blast_radius_threshold"]),
        always_review=frozenset(row["always_review"] or ()),
        confidence_floor=float(row["confidence_floor"]),
    )


def assess_eligibility(claim: dict[str, Any], policy: PromotionPolicy) -> Eligibility:
    """Every blocking reason, not just the first.

    A curator looking at an unpromotable claim needs to know everything standing in
    the way; fixing one blocker only to be shown the next is how a queue stops being
    worked.
    """
    reasons: list[str] = []

    if claim.get("status") == "unlinked" or claim.get("subject_entity_id") is None:
        reasons.append(INELIGIBLE_UNLINKED)
    if claim.get("status") in {"superseded", "rejected"}:
        reasons.append(INELIGIBLE_NOT_SETTLED)
    if claim.get("consolidated_at") is None:
        reasons.append(INELIGIBLE_NOT_SETTLED)
    if claim.get("is_contested"):
        reasons.append(INELIGIBLE_CONTESTED)
    if claim.get("promotion_state") in {"proposed", "promoted"}:
        reasons.append(INELIGIBLE_ALREADY)

    confidence = claim.get("confidence")
    if confidence is not None and float(confidence) < policy.confidence_floor:
        reasons.append(INELIGIBLE_BELOW_FLOOR)

    if promotion_targets.target_for(str(claim.get("predicate"))) is None:
        reasons.append(INELIGIBLE_NO_TARGET)

    # An unattributed claim has no authority to weigh against the graph it would
    # overwrite. It stays readable; it cannot become canonical.
    if claim.get("source_authority") == AUTHORITY_UNATTRIBUTED:
        reasons.append(INELIGIBLE_UNATTRIBUTED)

    # Deduplicate while keeping order: two paths can both report "not consolidated".
    ordered = tuple(dict.fromkeys(reasons))
    return Eligibility(eligible=not ordered, reasons=ordered)


def _narrows_surface(predicate: str, value: Any) -> bool:
    """Whether this specific assertion withdraws rather than extends.

    Only asked for predicates that denote a dependency surface. Within those, the
    direction matters: announcing a new operation is not the same as removing one,
    and treating every surface claim as high-impact would put routine additions in
    front of a human until the queue was ignored.
    """
    if predicate == "lifecycle_state":
        return str(value).lower() in _WITHDRAWING_STATES
    if predicate == "deprecated_after":
        # Naming a deprecation date is the announcement itself.
        return True
    if predicate == "is_publicly_callable":
        return value is False
    if predicate in {"interface_version", "depends_on_version"}:
        # A version predicate that excludes rather than admits. Anything expressed as
        # an upper bound or an exclusion removes versions a consumer may be on.
        return any(token in str(value) for token in ("<", "!=", "!"))
    if predicate == "exposes_operation":
        # Adding an operation is additive by construction. Removal is expressed by
        # the claim's interval ending, not by its value.
        return False
    return False


async def assess_impact(
    session: AsyncSession,
    claim: dict[str, Any],
    policy: PromotionPolicy,
    *,
    blast_radius: int | None = None,
) -> ImpactAssessment:
    """Classify without reading confidence.

    Certainty about a consequential change is a reason to review it, not to skip
    review, so no branch here consults the claim's score.
    """
    reasons: list[str] = []
    predicate = str(claim.get("predicate"))

    surface_evaluated = predicate in SURFACE_PREDICATES
    if surface_evaluated and _narrows_surface(predicate, claim.get("value")):
        reasons.append(IMPACT_NARROWS_SURFACE)

    if blast_radius is not None and blast_radius > policy.blast_radius_threshold:
        reasons.append(IMPACT_BLAST_RADIUS)

    if claim.get("is_contested"):
        reasons.append(IMPACT_CONTESTED)

    owning = claim.get("owning_tenant_id")
    if owning is not None and claim.get("author_tenant_id") != owning:
        reasons.append(IMPACT_CROSS_TENANT)

    if predicate in policy.always_review:
        reasons.append(IMPACT_ALWAYS_REVIEW)

    if await _supersedes_a_confirmation(session, claim):
        reasons.append(IMPACT_SUPERSEDES_CONFIRMED)

    return ImpactAssessment(reasons=tuple(reasons), surface_evaluated=surface_evaluated)


async def _supersedes_a_confirmation(session: AsyncSession, claim: dict[str, Any]) -> bool:
    """Would promoting this displace something a human already vouched for?

    Asked over the claim's neighbourhood rather than its supersession pointer,
    because the claim may not have superseded anything yet -- the question is what
    promoting it *would* displace.
    """
    subject = claim.get("subject_entity_id")
    if subject is None:
        return False
    # A confirmation is not a flag on the confirmed claim -- it is a separate claim
    # row pointing back at it. So the question is whether the neighbourhood holds a
    # live claim that confirms something, which is what `confirms_claim_id` marks.
    found = (
        await session.execute(
            text(
                "SELECT 1 FROM memory_claims "
                " WHERE subject_entity_id = :sid "
                "   AND predicate = CAST(:pred AS TEXT) "
                "   AND claim_id <> :cid "
                "   AND confirms_claim_id IS NOT NULL "
                "   AND t_invalidated_at IS NULL "
                " LIMIT 1"
            ),
            {
                "sid": subject,
                "pred": claim.get("predicate"),
                "cid": claim.get("claim_id"),
            },
        )
    ).first()
    return found is not None


async def blast_radius_for(session: AsyncSession, entity_id: uuid.UUID) -> int:
    """How many other entities depend on this one.

    Counted over inbound dependency edges that are live now. A deeper traversal
    would count transitively, but the count is a review threshold rather than a
    correctness property, and a direct-dependant count is the one an owner can
    verify by looking.
    """
    count = (
        await session.execute(
            text(
                "SELECT count(DISTINCT src_entity_id) FROM edges "
                " WHERE dst_entity_id = :eid "
                "   AND rel IN ('depends_on', 'composes', 'provides_to') "
                "   AND t_invalidated_at IS NULL "
                "   AND (t_valid_to IS NULL OR t_valid_to > now())"
            ),
            {"eid": entity_id},
        )
    ).scalar_one()
    return int(count)
