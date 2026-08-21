"""From candidates to claims, or to a counted refusal.

A provider proposes. This decides. Nothing a provider returns reaches the claim
store without passing every check here, and the checks are meant to reject: a
strategy whose output is mostly refused is a defective prompt, not a failing
system, and the difference is only visible because every refusal is categorized
and counted.

**Order matters, and it is cheapest-and-most-decisive first.** Containment before
conformance, because a directive candidate must be refused regardless of whether
its predicate happens to be legal — running conformance first would sometimes
report "unknown predicate" for what was actually an injection attempt, and the
response to those two findings is not the same. PII last among the value checks,
because it is the only one that costs database queries.

**One candidate's failure never touches its siblings.** A batch is not a
transaction. If a model returns nine good claims and one that is not storable,
staging nine is strictly better than staging none, and the tenth is reported
rather than lost.

**Conformance is measured per strategy, not per batch.** A strategy silently
drifting below its conformance target is the failure mode this measurement
exists for, and a global rate would hide one bad prompt behind four good ones.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid

from prometheus_client import Counter, Histogram
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.extraction import salience as salience_module
from contextplane.extraction.containment import (
    CandidateRefused,
    assert_evidence_cited,
    assert_no_boundary_forgery,
    assert_not_directive,
)
from contextplane.extraction.provider import CandidateClaim, ExtractionRequest, ExtractionResult
from contextplane.extraction.strategies import Strategy
from contextplane.profile.scoring import resolve_weights
from contextplane.security.pii_guard import AdmissionRefused, admit_or_refuse
from contextplane.service.memory.claim_authority import ClaimRejected, Evidence, StagedClaim
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.types import TenantContext

_log = logging.getLogger(__name__)

# The field name the PII scanner policies key off. Distinct from the artifact
# field types so a tenant can set a different policy for model-generated values
# than for text a person submitted -- a generated value is the riskier of the
# two, because nobody reviewed it before it was written.
PII_FIELD_TYPE = "claim_value"

REJECT_PII = "pii_blocked"
REJECT_NOT_PERMITTED_PREDICATE = "predicate_not_in_strategy"
REJECT_CONFIDENCE_FLOOR = "below_confidence_floor"
REJECT_NON_SCALAR_VALUE = "value_not_scalar"

# Every way a candidate can fail to become a claim. Bounded, because it is a
# metric label -- and complete, because an uncounted refusal is a pipeline that
# has quietly stopped producing.
EXTRACTION_REJECTIONS = frozenset(
    {
        REJECT_PII,
        REJECT_NOT_PERMITTED_PREDICATE,
        REJECT_CONFIDENCE_FLOOR,
        REJECT_NON_SCALAR_VALUE,
    }
)

_CANDIDATES = Counter(
    "contextplane_extraction_candidates_total",
    "Candidate claims returned by a provider, by strategy.",
    ["strategy"],
)

_STAGED = Counter(
    "contextplane_extraction_staged_total",
    "Candidates that became staged claims, by strategy.",
    ["strategy"],
)

_REJECTED = Counter(
    "contextplane_extraction_rejected_total",
    "Candidates refused before staging, by strategy and reason.",
    ["strategy", "reason"],
)

# Per strategy, because a global rate hides one defective prompt behind four
# working ones -- which is the exact case the conformance target exists to catch.
_CONFORMANCE = Histogram(
    "contextplane_extraction_conformance_ratio",
    "Share of a batch's candidates that conformed, per strategy run.",
    ["strategy"],
    buckets=(0.0, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 1.0),
)

# Ingest to staged. The lag budget is measured here rather than in the worker
# because only this layer knows when the source event was written.
_LAG = Histogram(
    "contextplane_extraction_lag_seconds",
    "Seconds from event ingest to staged claim.",
    buckets=(1, 5, 15, 30, 60, 120, 300, 900),
)


@dataclasses.dataclass(frozen=True)
class ExtractionOutcome:
    """What one strategy run produced, and what it refused.

    Carries both because either alone is misleading. Staged claims without
    refusals look like a clean run when half the output was unusable; refusals
    without staged claims cannot distinguish a defective prompt from a
    transcript that genuinely contained nothing.
    """

    strategy_id: str
    staged: tuple[StagedClaim, ...]
    refusals: tuple[tuple[str, str], ...]
    candidates_seen: int

    @property
    def conformance_ratio(self) -> float:
        """Share of candidates that became claims.

        An empty batch is 1.0, not 0.0. A transcript with nothing to extract is
        not a conformance failure, and scoring it as one would drag a healthy
        strategy below its target on quiet days.
        """
        if self.candidates_seen == 0:
            return 1.0
        return len(self.staged) / self.candidates_seen


class ExtractionService:
    """Turns provider output into staged claims, refusing what it must."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        claims: ClaimService,
        *,
        pii_field_type: str = PII_FIELD_TYPE,
    ) -> None:
        self._session_factory = session_factory
        self._claims = claims
        self._pii_field_type = pii_field_type

    async def stage_result(
        self,
        ctx: TenantContext,
        *,
        strategy: Strategy,
        request: ExtractionRequest,
        result: ExtractionResult,
        known_event_ids: frozenset[str],
        confidence_floor: float | None = None,
        lag_seconds: float | None = None,
        namespace: str | None = None,
    ) -> ExtractionOutcome:
        """Validate and stage every candidate the provider returned.

        `known_event_ids` is the batch the provider was given. A citation outside
        it was not observed, so it is a fabrication rather than a mistake.

        The request is taken rather than a bare boundary so that the delimiter
        checked here is provably the one the bodies were wrapped in. A separate
        boundary argument is a second source of truth for a value that only
        means anything when both ends agree, and it silently disagreed.
        """
        # An empty delimiter is worse than none: `"" in text` is true of every
        # string, so the forgery check would refuse the whole batch and report a
        # containment attack that never happened.
        if not request.boundary:
            msg = "the staged request carries no containment boundary, so its output cannot be checked"
            raise ValueError(msg)

        floor = confidence_floor if confidence_floor is not None else strategy.default_confidence_floor
        _CANDIDATES.labels(strategy=strategy.strategy_id).inc(len(result.claims))

        # Salience is a property of the episode, not of any one claim, so it is
        # computed once over the window the provider was given and carried
        # identically onto every claim staged from it. Computing it per candidate
        # would produce the same number N times at N times the cost, and would
        # invite a future edit that made it differ per claim -- at which point
        # two claims from one conversation would disagree about how much that
        # conversation was worth keeping.
        signals = salience_module.signal_vector(request.events)
        # Resolved for this tenant, not read from the registry: a tenant that
        # published and activated a salience override should have it applied to
        # what it decides to keep. Resolved at write, so changing the weights
        # does not rescore what is already staged -- salience is a property of
        # the episode as it was judged, and a number that moved under an
        # unchanged episode would make the reliability curve meaningless.
        async with self._session_factory() as session:
            salience_weights = await resolve_weights(
                session, tenant_id=ctx.tenant_id, model_id=salience_module.WEIGHTS_MODEL_ID
            )
        episode_salience = salience_module.combine(signals, weights=salience_weights.value)

        staged: list[StagedClaim] = []
        refusals: list[tuple[str, str]] = []

        for candidate in result.claims:
            # One candidate's failure never touches its siblings. Nine good
            # claims and one bad one should stage nine, not zero.
            try:
                claim = await self._stage_one(
                    ctx,
                    strategy=strategy,
                    candidate=candidate,
                    known_event_ids=known_event_ids,
                    boundary=request.boundary,
                    floor=floor,
                    namespace=namespace,
                    salience=episode_salience,
                    signals=signals,
                )
            except CandidateRefused as refused:
                # Containment already counted its own trigger; this counts it
                # against the strategy so a poisoned source is attributable.
                refusals.append((refused.trigger, refused.detail))
                _REJECTED.labels(strategy=strategy.strategy_id, reason=refused.trigger).inc()
                continue
            except ClaimRejected as rejected:
                refusals.append((rejected.reason, str(rejected)))
                _REJECTED.labels(strategy=strategy.strategy_id, reason=rejected.reason).inc()
                continue
            except _NotStaged as skipped:
                refusals.append((skipped.reason, skipped.detail))
                _REJECTED.labels(strategy=strategy.strategy_id, reason=skipped.reason).inc()
                continue

            staged.append(claim)
            _STAGED.labels(strategy=strategy.strategy_id).inc()
            if lag_seconds is not None:
                _LAG.observe(lag_seconds)

        outcome = ExtractionOutcome(
            strategy_id=strategy.strategy_id,
            staged=tuple(staged),
            refusals=tuple(refusals),
            candidates_seen=len(result.claims),
        )
        _CONFORMANCE.labels(strategy=strategy.strategy_id).observe(outcome.conformance_ratio)

        if outcome.refusals:
            _log.info(
                "extraction.refusals strategy=%s staged=%d refused=%d reasons=%s",
                strategy.strategy_id,
                len(outcome.staged),
                len(outcome.refusals),
                sorted({r for r, _ in outcome.refusals}),
            )
        return outcome

    async def _stage_one(
        self,
        ctx: TenantContext,
        *,
        strategy: Strategy,
        candidate: CandidateClaim,
        known_event_ids: frozenset[str],
        boundary: str,
        floor: float,
        namespace: str | None = None,
        salience: float,
        signals: dict[str, float],
    ) -> StagedClaim:
        """Every check a candidate must pass, in the order it must pass them."""
        # 1. Citation. A candidate nobody can trace is indistinguishable from an
        #    invention, and a fabricated citation is worse than none.
        assert_evidence_cited(candidate.evidence_event_ids, known_event_ids)

        # 2. A value that is not a scalar is refused before anything reads it.
        #    `assert_not_directive` returns immediately for a non-string, so a
        #    directive buried in a list or an object would pass every content
        #    check without one of them looking at it. The only thing keeping
        #    that unreachable today is a provider enforcing its tool-argument
        #    schema, which is a guarantee no third-party backend owes us.
        if not isinstance(candidate.value, str | int | float | bool | None):
            raise _NotStaged(
                REJECT_NON_SCALAR_VALUE,
                f"value was {type(candidate.value).__name__}; a claim value is a scalar, and a "
                "structured one carries text no content check would read",
            )

        # 3. Containment, before conformance. A directive candidate is refused
        #    regardless of whether its predicate is legal -- and reporting
        #    "unknown predicate" for what was an injection attempt would route
        #    the finding to the wrong person.
        assert_no_boundary_forgery(str(candidate.value), boundary)
        if candidate.excerpt:
            assert_no_boundary_forgery(candidate.excerpt, boundary)
        assert_not_directive(candidate.value)
        if candidate.excerpt:
            # The excerpt is stored as provenance and read by humans and agents
            # alike, so it carries instructions just as effectively as a value.
            assert_not_directive(candidate.excerpt, field="excerpt")

        # 4. The strategy's own predicate set. Narrower than the ontology: a
        #    strategy that could emit any predicate would make its permitted set
        #    documentation rather than a boundary.
        if candidate.predicate not in strategy.permitted_predicates:
            raise _NotStaged(
                REJECT_NOT_PERMITTED_PREDICATE,
                f"predicate {candidate.predicate!r} is not in the {strategy.strategy_id} " f"strategy's permitted set",
            )

        # 5. Confidence floor, when one is configured. Skipped at zero, which is
        #    the honest default while confidence is uncalibrated -- a floor on an
        #    uncalibrated number filters by noise.
        if floor > 0.0 and candidate.provider_confidence is not None:
            if candidate.provider_confidence < floor:
                raise _NotStaged(
                    REJECT_CONFIDENCE_FLOOR,
                    f"confidence {candidate.provider_confidence} is below the floor {floor}",
                )

        # 6. PII, last among the value checks because it is the only one that
        #    costs queries. Scanned on the way out, not only on the way in: a
        #    model can reproduce a card number from a source body into its
        #    output, and that output has been reviewed by nobody.
        if isinstance(candidate.value, str):
            await self._assert_no_pii(ctx, candidate.value, field="value", strategy=strategy)
        if candidate.excerpt:
            await self._assert_no_pii(ctx, candidate.excerpt, field="excerpt", strategy=strategy)

        # 7. The single write path, which applies the ontology, resolves the
        #    subject, derives authority and visibility, and can still refuse.
        return await self._claims.stage_claim(
            ctx,
            subject_reference=candidate.subject_reference,
            predicate=candidate.predicate,
            value=candidate.value,
            evidence=tuple(
                Evidence(kind="session_event", ref=event_id, excerpt=candidate.excerpt)
                for event_id in candidate.evidence_event_ids
            ),
            namespace=namespace,
            strategy_id=strategy.strategy_id if namespace is not None else None,
            salience=salience,
            salience_signals=signals,
            salience_weights_id=salience_module.WEIGHTS_MODEL_ID,
        )

    async def _assert_no_pii(self, ctx: TenantContext, text: str, *, field: str, strategy: Strategy) -> None:
        """Admit an extracted value, or refuse to stage it.

        This path writes `claim_value`, a pilot field type, and for a long time
        it scanned without a floor -- so a generated value carrying a prohibited
        class was stored on any deployment that had configured no policy row. It
        was not on any list of the write surfaces; the enumeration that starts
        from the field types found it.

        `strategy_id` is passed because this is the one call site that has one:
        a refusal here is attributable to the strategy that produced the value,
        which is what makes a poisoned source traceable.
        """
        try:
            await admit_or_refuse(
                self._session_factory,
                ctx,
                text,
                self._pii_field_type,
                subject=field,
                strategy_id=strategy.strategy_id,
            )
        except AdmissionRefused as refused:
            raise _NotStaged(
                REJECT_PII,
                f"{field} carries a prohibited class: {sorted(refused.decision.classes)}",
            ) from refused


class _NotStaged(Exception):
    """A refusal this module owns, as opposed to containment's or the write path's.

    Private because the reason codes are the public surface; a caller should
    branch on `reason`, not on the exception type.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def event_id_set(event_ids: tuple[uuid.UUID, ...]) -> frozenset[str]:
    """The batch's ids as the provider sees them: strings."""
    return frozenset(str(e) for e in event_ids)


__all__ = [
    "EXTRACTION_REJECTIONS",
    "PII_FIELD_TYPE",
    "REJECT_CONFIDENCE_FLOOR",
    "REJECT_NON_SCALAR_VALUE",
    "REJECT_NOT_PERMITTED_PREDICATE",
    "REJECT_PII",
    "ExtractionOutcome",
    "ExtractionService",
    "event_id_set",
]
