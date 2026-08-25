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
import datetime
import json
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.audit import actions
from contextplane.exceptions import ValidationError
from contextplane.service.governance.authority import AUTHORITY_UNATTRIBUTED
from contextplane.service.memory import promotion_targets
from contextplane.types import JSONValue, TenantContext

# --- eligibility --------------------------------------------------------------

INELIGIBLE_UNLINKED: Final[str] = "no subject entity"
INELIGIBLE_NOT_SETTLED: Final[str] = "not consolidated"
INELIGIBLE_CONTESTED: Final[str] = "contested"
INELIGIBLE_BELOW_FLOOR: Final[str] = "below the tenant confidence floor"
INELIGIBLE_NO_TARGET: Final[str] = "no canonical target"
INELIGIBLE_UNATTRIBUTED: Final[str] = "no attributable source"
INELIGIBLE_ALREADY: Final[str] = "already promoted"
#: Withheld by an operator. Not a judgement about the claim's content -- a
#: quarantine says the *provenance* turned out to be wrong -- but a claim nobody
#: is allowed to read must not become the canonical answer while it is withheld.
INELIGIBLE_QUARANTINED: Final[str] = "withheld by a quarantine"

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
    """Whether a claim can be promoted, and if not, why."""

    eligible: bool
    reasons: tuple[str, ...]

    @property
    def blocked_by(self) -> str | None:
        """First blocking reason if any; None when eligible."""
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
        """Whether the claim narrows a surface."""
        return bool(self.reasons)


async def load_policy(session: AsyncSession, tenant_id: uuid.UUID) -> PromotionPolicy:
    """Load the tenant's promotion policy or fall back to defaults."""
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


async def set_policy(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    confidence_floor: float,
    blast_radius_threshold: int,
    always_review: frozenset[str],
    now: datetime.datetime,
) -> PromotionPolicy:
    """Configure a tenant's review posture. Without this, the floor, the
    blast-radius threshold, and the always-review list are the cautious
    defaults `PromotionPolicy` ships with and nothing an operator does can
    change that.

    Lives beside `load_policy` as a bare function rather than on a service
    class, matching the module's own shape: a `PromotionPolicyService` would
    hold no state beyond this one write, and folding it into
    `GuardrailService` would mix the allowlist (a genuinely different
    concern -- what may skip review) with the floor and threshold (what is
    eligible at all).

    Admin-only and audited: widening or narrowing what promotes without
    review is a more consequential act than any individual promotion it
    governs, so the caller's own transaction carries this write the same
    way it would carry any other privileged config change.
    """
    if "admin" not in ctx.roles:
        raise PermissionError("configuring the promotion policy requires the admin role")
    if not 0.0 <= confidence_floor <= 1.0:
        raise ValidationError("confidence_floor must be between 0 and 1")
    if blast_radius_threshold < 0:
        raise ValidationError("blast_radius_threshold must not be negative")

    review_list = sorted(always_review)
    await session.execute(
        text(
            "INSERT INTO memory_promotion_policy "
            "  (tenant_id, blast_radius_threshold, always_review, confidence_floor, "
            "   updated_at, updated_by) "
            "VALUES (:tid, :threshold, CAST(:always_review AS JSONB), :floor, :now, :actor) "
            "ON CONFLICT (tenant_id) DO UPDATE SET "
            "  blast_radius_threshold = EXCLUDED.blast_radius_threshold, "
            "  always_review = EXCLUDED.always_review, "
            "  confidence_floor = EXCLUDED.confidence_floor, "
            "  updated_at = EXCLUDED.updated_at, "
            "  updated_by = EXCLUDED.updated_by"
        ),
        {
            "tid": ctx.tenant_id,
            "threshold": blast_radius_threshold,
            "always_review": json.dumps(review_list),
            "floor": confidence_floor,
            "now": now,
            "actor": ctx.actor_id,
        },
    )
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
            "   before_jsonb, after_jsonb, ts, request_id, error_code) "
            "VALUES (:audit_id, :tid, :aid, :action, 'tenant', :tid, NULL, "
            "        CAST(:after AS JSONB), :now, NULL, NULL)"
        ),
        {
            "audit_id": uuid.uuid4(),
            "tid": ctx.tenant_id,
            "aid": ctx.actor_id,
            "action": actions.PROMOTION_POLICY_SET,
            "after": json.dumps(
                {
                    "confidence_floor": confidence_floor,
                    "blast_radius_threshold": blast_radius_threshold,
                    "always_review": review_list,
                },
                sort_keys=True,
            ),
            "now": now,
        },
    )
    return PromotionPolicy(
        blast_radius_threshold=blast_radius_threshold,
        always_review=frozenset(always_review),
        confidence_floor=confidence_floor,
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

    # Here rather than only in the sweep's candidate query, which is where the
    # gap was: that query is one propose path and the guard belongs where every
    # path passes. `quarantine.py` says no future *serving* path can forget the
    # column because the predicate is materialised -- true, and promotion is not
    # a serving path. It writes canon, which is worse to get wrong.
    if claim.get("quarantined_at") is not None:
        reasons.append(INELIGIBLE_QUARANTINED)

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


def _narrows_surface(predicate: str, value: JSONValue) -> bool:
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
    # "exposes_operation": adding an operation is additive by construction. Removal
    # is expressed by the claim's interval ending, not by its value. This function is
    # only called (below) when predicate is already a member of SURFACE_PREDICATES,
    # so every other member is handled by a branch above and this is the only value
    # that reaches here -- the trailing `return False` exists so this function's
    # declared bool return stays total for the type checker, not to cover a case
    # that actually occurs.
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
