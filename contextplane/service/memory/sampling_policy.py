"""How much of a category's review queue has to be looked at, and why that much.

E5-T2. One governed policy per `(tenant, claim_category)`, with the sample size
derived from a stated defect tolerance and consumer's risk rather than chosen.

**The derivation, in one line.** For a zero-acceptance plan, the chance of
accepting a lot whose true defect rate is `p` after `n` independent draws is
`(1 - p)**n`. Requiring that to be at most the consumer's risk `beta` gives

    n >= ln(beta) / ln(1 - p)

and `min_sample` is the smallest integer satisfying it. At a 5% tolerance and
10% consumer's risk that is 45.

**What that arithmetic assumes, stated because the ADR's dissent asked for it.**
It is exact for a *representative draw*. E5's queue is ranked, and E5-T4 will
let a policy dispose of items without a human — so the reviewed subset is not a
random sample of the lot, and the true consumer's risk is not the one this
number was derived against. The figure is a floor on effort, not a guarantee
about the residue, and no caller should describe it as the second. Making it a
guarantee needs a model of how ranking and auto-disposition bias the draw, which
does not exist and is not this task.

**Unknown categories get the strictest policy, never the default.** The rule
E1's audit established for an unrecognised sensitivity tier, applied here for
the same reason: a value nobody registered escaping every rule that names one is
how a policy gets switched off by a typo. `policy_for` falls back to the
tenant's own heaviest plan rather than to a constant written here -- a tenant
that reviews everything closely should not have an unknown category drop to a
laxer number this module chose.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane import ranking
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.catalog.global_vocabulary import CLAIM_CATEGORIES
from contextplane.types import Clock, TenantContext

#: Who may set a sampling floor. The same bar quarantine holds: how much of a
#: queue goes unreviewed is a decision, not something a machine's own evidence
#: implies.
_OPERATOR_ROLES: Final[frozenset[str]] = frozenset({"producer", "admin"})

#: The stated inputs the unconfigured floor is derived from. Held here because
#: the registry governs the *result*, and a reader checking that 299 is right
#: needs the pair it came from in the same place the arithmetic is.
_UNCONFIGURED_TOLERANCE: Final = 0.01
_UNCONFIGURED_RISK: Final = 0.05

#: The sample a tenant with no configured policy falls back to, read from the
#: governed registry rather than computed here.
#:
#: Read at import, which is what `coupling: consumed` claims: an entry nothing
#: reads is governed in name only, and the loader's `requires_validated` refusal
#: never reaches a deployment for a magnitude no code path asks for.
#:
#: The number is still checked against its own derivation below, so the registry
#: and the arithmetic cannot drift apart silently -- a registry entry that
#: stopped following from its recorded `derived_from` would be a derivation
#: nobody can reproduce, which is the one thing `derived` status must not permit.
_UNCONFIGURED_SAMPLE: Final[int] = int(ranking.threshold("review-sampling-unconfigured-floor@1"))


def minimum_sample(defect_tolerance: float, consumers_risk: float) -> int:
    """The smallest zero-acceptance sample meeting this plan. Reproducible.

    Raises rather than clamping on an out-of-range input: a tolerance of 0
    demands an infinite sample and a tolerance of 1 accepts anything, and
    silently substituting a workable number for either would put a figure in the
    registry that no stated pair produces.
    """
    if not 0 < defect_tolerance < 1:
        raise ValidationError(
            f"defect_tolerance must be strictly between 0 and 1, got {defect_tolerance}; "
            "0 demands an infinite sample and 1 accepts anything"
        )
    if not 0 < consumers_risk < 1:
        raise ValidationError(f"consumers_risk must be strictly between 0 and 1, got {consumers_risk}")
    return math.ceil(math.log(consumers_risk) / math.log(1 - defect_tolerance))


@dataclasses.dataclass(frozen=True)
class SamplingPolicy:
    """What one category's review budget is, and the inputs it follows from."""

    claim_category: str
    defect_tolerance: float
    consumers_risk: float
    min_sample: int
    reason: str

    def recomputes(self) -> bool:
        """Whether the stored sample still follows from the stored inputs.

        Read on every load. A row whose three numbers disagree is a budget
        somebody edited in the database, and it would otherwise serve as though
        it were derived.
        """
        return self.min_sample == minimum_sample(self.defect_tolerance, self.consumers_risk)


class SamplingPolicyService:
    """Set, read and apply the review budget for a tenant's claim categories."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._factory = session_factory
        self._clock = clock

    async def set_policy(
        self,
        ctx: TenantContext,
        *,
        claim_category: str,
        defect_tolerance: float,
        consumers_risk: float,
        reason: str,
    ) -> SamplingPolicy:
        """Record what this tenant will accept for one category.

        `min_sample` is computed here rather than accepted from the caller: a
        budget a caller could state independently of its inputs is one that can
        disagree with them, and the whole point of the pair is that the third
        number follows.
        """
        self._require_operator(ctx)
        if claim_category not in CLAIM_CATEGORIES:
            raise ValidationError(
                f"unknown claim category {claim_category!r}; expected one of {sorted(CLAIM_CATEGORIES)}"
            )
        if len(reason.strip()) < 20:
            raise ValidationError(
                "a sampling policy needs a stated reason of at least 20 characters; "
                "a budget recorded with 'prod' beside it is one nobody can review"
            )
        sample = minimum_sample(defect_tolerance, consumers_risk)
        now = self._clock.now()

        async with self._factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO claim_sampling_policies "
                    "  (tenant_id, claim_category, defect_tolerance, consumers_risk, min_sample, "
                    "   set_by, set_at, reason) "
                    "VALUES (:tid, :cat, :tol, :risk, :n, :actor, CAST(:now AS TIMESTAMPTZ), :reason) "
                    "ON CONFLICT (tenant_id, claim_category) DO UPDATE SET "
                    "  defect_tolerance = EXCLUDED.defect_tolerance, "
                    "  consumers_risk = EXCLUDED.consumers_risk, "
                    "  min_sample = EXCLUDED.min_sample, "
                    "  set_by = EXCLUDED.set_by, set_at = EXCLUDED.set_at, reason = EXCLUDED.reason"
                ),
                {
                    "actor": ctx.actor_id,
                    "cat": claim_category,
                    "n": sample,
                    "now": now,
                    "reason": reason,
                    "risk": consumers_risk,
                    "tid": ctx.tenant_id,
                    "tol": defect_tolerance,
                },
            )
        return SamplingPolicy(
            claim_category=claim_category,
            defect_tolerance=defect_tolerance,
            consumers_risk=consumers_risk,
            min_sample=sample,
            reason=reason,
        )

    async def policy_for(self, ctx: TenantContext, *, claim_category: str) -> SamplingPolicy:
        """The budget governing this category, never a laxer one.

        Three cases, and the second is the rule this method exists for:

        1. A policy for this exact category: use it.
        2. **No policy, or a category this vocabulary does not have**: the
           strictest policy the tenant has set. An unregistered value must not
           escape every rule that names one, which is what a default would let
           it do.
        3. No policies at all: the module's own strictest plan, which is
           stricter than anything a tenant is likely to set. A tenant that has
           configured nothing has not decided that little review is acceptable.
        """
        async with self._factory() as session:
            if claim_category in CLAIM_CATEGORIES:
                exact = await self._load(session, ctx, claim_category)
                if exact is not None:
                    return exact
            return await self._strictest(session, ctx)

    async def _load(self, session: AsyncSession, ctx: TenantContext, claim_category: str) -> SamplingPolicy | None:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT claim_category, defect_tolerance, consumers_risk, min_sample, reason "
                        "  FROM claim_sampling_policies "
                        " WHERE tenant_id = :tid AND claim_category = :cat"
                    ),
                    {"cat": claim_category, "tid": ctx.tenant_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._checked(row)

    async def _strictest(self, session: AsyncSession, ctx: TenantContext) -> SamplingPolicy:
        """The tenant's own heaviest plan, or this module's if they have none.

        Ordered by `min_sample` rather than by tolerance, because the sample is
        what a reviewer's budget is spent in and two different pairs can produce
        the same one.
        """
        row = (
            (
                await session.execute(
                    text(
                        "SELECT claim_category, defect_tolerance, consumers_risk, min_sample, reason "
                        "  FROM claim_sampling_policies "
                        " WHERE tenant_id = :tid "
                        " ORDER BY min_sample DESC, claim_category LIMIT 1"
                    ),
                    {"tid": ctx.tenant_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return self._checked(row)
        return SamplingPolicy(
            claim_category="",
            defect_tolerance=_UNCONFIGURED_TOLERANCE,
            consumers_risk=_UNCONFIGURED_RISK,
            min_sample=_UNCONFIGURED_SAMPLE,
            reason="No sampling policy is configured for this tenant, so the strictest plan applies.",
        )

    @staticmethod
    def _checked(row: object) -> SamplingPolicy:
        """A stored row, refused if its three numbers no longer agree."""
        mapping = dict(row)  # type: ignore[call-overload]
        policy = SamplingPolicy(
            claim_category=str(mapping["claim_category"]),
            defect_tolerance=float(mapping["defect_tolerance"]),
            consumers_risk=float(mapping["consumers_risk"]),
            min_sample=int(mapping["min_sample"]),
            reason=str(mapping["reason"]),
        )
        if not policy.recomputes():
            raise ConflictError(
                f"the stored sample for {policy.claim_category!r} is {policy.min_sample}, and its "
                f"recorded tolerance and risk derive {minimum_sample(policy.defect_tolerance, policy.consumers_risk)}. "
                "A budget that no longer follows from its inputs is not a derived number"
            )
        return policy

    async def remove(self, ctx: TenantContext, *, claim_category: str) -> None:
        """Drop one category's policy, so it falls back to the strictest.

        Deleting is not the same as setting a lax policy, and the fallback is
        what makes that safe: a category with no row is governed by the
        tenant's heaviest plan rather than by nothing.
        """
        self._require_operator(ctx)
        async with self._factory() as session, session.begin():
            removed = await session.execute(
                text(
                    "DELETE FROM claim_sampling_policies "
                    " WHERE tenant_id = :tid AND claim_category = :cat RETURNING claim_category"
                ),
                {"cat": claim_category, "tid": ctx.tenant_id},
            )
            if removed.one_or_none() is None:
                raise NotFoundError(f"no sampling policy for {claim_category!r} in this tenant")

    async def policies(self, ctx: TenantContext) -> tuple[SamplingPolicy, ...]:
        """Every policy this tenant has set, heaviest first."""
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT claim_category, defect_tolerance, consumers_risk, min_sample, reason "
                            "  FROM claim_sampling_policies WHERE tenant_id = :tid "
                            " ORDER BY min_sample DESC, claim_category"
                        ),
                        {"tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(self._checked(row) for row in rows)

    async def acceptance_for(
        self,
        ctx: TenantContext,
        *,
        claim_category: str,
        inspected: int,
    ) -> AcceptanceState:
        """This category's floor, against a count of what a person inspected.

        The count is passed in rather than read here, and that is the seam: the
        floor is this module's, the count is `CurationCaseService`'s, and a
        module that computed both would be free to compute the second in a way
        that suited the first. `inspected_dispositions` already excludes
        automated disposals, and its docstring is the argument for why -- so a
        caller wanting to satisfy the floor cannot do it by automating more.

        Unknown categories fall back to the tenant's heaviest plan, the same way
        every other read here does: a category nobody registered must not escape
        the floor by not being named.
        """
        policy = await self.policy_for(ctx, claim_category=claim_category)
        return acceptance_state(policy, inspected=inspected)

    @staticmethod
    def _require_operator(ctx: TenantContext) -> None:
        if not (set(ctx.roles) & _OPERATOR_ROLES):
            raise PermissionError("setting a review sampling policy requires the producer or admin role")


# ---------------------------------------------------------------------------
# The halt
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AcceptanceState:
    """Whether a category's lot may be accepted, and by how much it falls short.

    Carries the shortfall rather than only a boolean, because "not yet" and "not
    nearly" are different operational situations and a caller told only that it
    may not proceed cannot tell an operator which one they are in.
    """

    claim_category: str
    #: The floor, derived from the policy's stated tolerance and consumer's risk.
    min_sample: int
    #: Dispositions a *person* made in the window. Automated disposals are
    #: excluded upstream, and that exclusion is the whole reason this number is
    #: the one acceptance sampling is entitled to use.
    inspected: int

    @property
    def met(self) -> bool:
        """Whether enough was actually looked at."""
        return self.inspected >= self.min_sample

    @property
    def shortfall(self) -> int:
        """How many more inspections the floor needs. Zero once it is met."""
        return max(0, self.min_sample - self.inspected)


class SampleTooSmall(ValidationError):
    """A lot was offered for acceptance on fewer inspections than its floor.

    Its own type rather than a bare `ValidationError`, because a caller has to
    treat it as terminal for the *lot* rather than for the request: the fix is
    more review, not a corrected argument.
    """


def acceptance_state(policy: SamplingPolicy, *, inspected: int) -> AcceptanceState:
    """The comparison this module was missing.

    E5-T2b. `min_sample` shipped with E5-T2 and `inspected_dispositions` shipped
    with E5-T4 -- the floor and the count acceptance sampling is entitled to use,
    each with its derivation written down, and **nothing compared them.** So a
    tenant could set a budget, review a tenth of it, and nothing anywhere said
    so.

    Pure, so the halt is testable without a database and cannot depend on how a
    caller happened to obtain either number.
    """
    return AcceptanceState(
        claim_category=policy.claim_category,
        min_sample=policy.min_sample,
        inspected=inspected,
    )


def require_minimum_sample(state: AcceptanceState) -> None:
    """Stop rather than accept a lot on a short sample.

    **Defined here, once, and not by whoever wants to accept something.** E12
    inherits this halt rather than defining its own, because a batch import that
    sampled itself under its own rules would be grading its own homework with a
    marking scheme it chose -- and acceptance sampling's arithmetic says nothing
    at all about a lot inspected fewer times than the plan requires. Proceeding
    on a short sample does not weaken the guarantee; it removes it, while leaving
    a number that still looks like one.

    Raises rather than returning a verdict, because the one thing a caller must
    not be able to do is read this and continue anyway without saying so.
    """
    if state.met:
        return
    msg = (
        f"the review sample for {state.claim_category!r} is {state.inspected} of a required "
        f"{state.min_sample}; {state.shortfall} more must be inspected by a person before this "
        "lot can be accepted. Acceptance sampling says nothing about a lot inspected fewer times "
        "than its plan requires."
    )
    raise SampleTooSmall(msg)
