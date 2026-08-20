"""A human putting their name to a claim, and what that changes.

Confirmation is the strongest thing that can happen to a claim short of promotion.
It raises authority to the human tier, sets confidence to the confirmed value, and
holds decay off for a bounded interval.

**It supersedes rather than mutates.** A person confirming a claim is a new, stronger
observation, so it produces a new row with its own authority, score, and decay
origin. The original keeps its score and its provenance, which is what lets a reader
see both what a machine estimated and what a human then said. Mutating in place
would erase the first half of that, and the audit record would show a claim that had
always been confirmed.

**Its decay origin is the confirmation, not the original assertion.** Resuming from
where decay would have been would make a confirmation worthless the moment its hold
expired -- a long-lived claim confirmed today would snap back toward the floor on
that day, and somebody who spent time reviewing it would rightly call that a bug.

**A machine claim can contest a confirmed claim but cannot supersede it.** Contesting
is what a disagreement does: it lowers both scores and routes the pair for review.
Superseding requires equal-or-higher authority, and no machine tier is equal to a
human one, so model output cannot quietly overturn a human decision. That is enforced
by comparing authority ranks, not by trusting a caller to check.

**Only a human principal may confirm.** The tier comes from the authenticated
actor's kind, never from what the caller says it is doing -- otherwise the human tier
is reachable by any worker that calls this method.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any

from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.audit import actions
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.governance.authority import (
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OWNER_HUMAN,
    SOURCE_AUTHORITY_RANK,
)
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.memory.confidence import (
    ConfidenceInputs,
    ConfidencePolicy,
    bucket_for,
)
from contextplane.service.memory.confidence_decay import confirmation_hold_days
from contextplane.types import Clock, TenantContext

_CONFIRMED = Counter(
    "contextplane_claim_confirmed_total",
    "Claims confirmed by a human, by the authority tier the confirmation carries.",
    ["authority"],
)

_ADJUDICATED = Counter(
    "contextplane_claim_adjudicated_total",
    "Claims judged correct or otherwise by a reviewer, by verdict.",
    ["verdict"],
)

VERDICT_CORRECT = "correct"
VERDICT_INCORRECT = "incorrect"
# A reviewer who cannot tell has said something. Folding it into "incorrect" would
# bias every calibration fit downward.
VERDICT_UNDECIDABLE = "undecidable"

VERDICTS = frozenset({VERDICT_CORRECT, VERDICT_INCORRECT, VERDICT_UNDECIDABLE})


@dataclasses.dataclass(frozen=True)
class Confirmation:
    """The new claim a confirmation produced."""

    claim_id: uuid.UUID
    confirms_claim_id: uuid.UUID
    source_authority: str
    confidence: float
    bucket: str
    hold_until: datetime.datetime


class ConfirmationService:
    """Confirms claims, and records judged outcomes."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        claims: ClaimService,
        *,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        # Injected rather than constructed, so the claim write path is one object
        # with one configuration however many services reach it.
        self._claims = claims
        self._clock = clock

    async def confirm(
        self,
        ctx: TenantContext,
        *,
        claim_id: uuid.UUID,
        policy: ConfidencePolicy | None = None,
    ) -> Confirmation:
        """Confirm a claim, producing a new one that supersedes it.

        Refuses a service principal: the human tier comes from the authenticated
        actor's kind, so a worker calling this would otherwise mint human-tier
        authority for itself.
        """
        active = policy or ConfidencePolicy()
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            if not await self._actor_is_human(session, ctx):
                msg = (
                    "only a human principal may confirm a claim; the human authority tier "
                    "records that a person reviewed this, and a service account calling this "
                    "method would be asserting something nobody did"
                )
                raise PermissionError(msg)

            original = (
                await session.execute(
                    text(
                        "SELECT owning_tenant_id, author_tenant_id, subject_entity_id, "
                        "       subject_reference, predicate, value_type, claim_category, "
                        "       value_jsonb, value_cardinality, value_entity_id, "
                        "       asserted_valid_from, asserted_valid_to, visibility, "
                        "       size_bytes, namespace, strategy_id, status, superseded_by "
                        "FROM memory_claims WHERE claim_id = :cid FOR UPDATE"
                    ),
                    {"cid": claim_id},
                )
            ).one_or_none()

            if original is None:
                msg = f"claim {claim_id} not found"
                raise NotFoundError(msg)
            if original.superseded_by is not None:
                msg = (
                    f"claim {claim_id} was already superseded by {original.superseded_by}; "
                    "confirm the current claim instead"
                )
                raise ConflictError(msg)
            if original.subject_entity_id is None:
                msg = (
                    f"claim {claim_id} has no resolved subject, so there is nothing to confirm "
                    "about a capability; link it first"
                )
                raise ConflictError(msg)

            # Standing is the confirming tenant against the subject's owner, the
            # same rule the write path uses. A non-owner human confirmation is
            # real and worth recording, and it still cannot outrank an owner.
            is_owner = original.owning_tenant_id == ctx.tenant_id
            authority = AUTHORITY_OWNER_HUMAN if is_owner else AUTHORITY_OBSERVER_HUMAN

            hold_days = confirmation_hold_days(original.claim_category, configured=float(active.confirmation_hold_days))
            hold_until = now + datetime.timedelta(days=hold_days)

            inputs = ConfidenceInputs(
                authority=authority,
                base=active.base_by_authority[authority],
                corroborating_classes=0,
                corroboration_probability=0.0,
                is_contested=False,
                is_confirmed=True,
                provider_confidence=None,
                provider_applied=False,
            )
            confidence = active.confirmed_confidence

            # The write itself belongs to the one module that creates claims. This
            # service decides *whether* a confirmation may happen and what tier it
            # carries; it does not insert rows, because a second inserter would be a
            # second writer however careful it was.
            new_claim_id = await self._claims.stage_confirmation(
                session,
                confirms_claim_id=claim_id,
                authority=authority,
                confidence=confidence,
                confidence_inputs=json.dumps(inputs.as_json(), sort_keys=True),
                hold_until=hold_until,
                confirming_tenant_id=ctx.tenant_id,
                confirming_actor_id=ctx.actor_id,
                now=now,
            )

            # Two rows, not one: the confirmation and the supersession it causes are
            # different facts about different claims. Keying the second to the
            # original claim is what lets "what happened to this specific claim"
            # stay a lookup on its own target_id rather than a join through the one
            # that superseded it.
            confirmed_bucket = bucket_for(confidence)
            await self._audit(
                session,
                action=actions.CLAIM_CONFIRMED,
                tenant_id=original.owning_tenant_id,
                actor_id=ctx.actor_id,
                target_id=new_claim_id,
                payload={
                    "confirms_claim_id": str(claim_id),
                    "bucket": confirmed_bucket,
                    "source_authority": authority,
                },
                now=now,
            )
            await self._audit(
                session,
                action=actions.CLAIM_SUPERSEDED,
                tenant_id=original.owning_tenant_id,
                actor_id=ctx.actor_id,
                target_id=claim_id,
                payload={"superseded_by": str(new_claim_id)},
                now=now,
            )

        _CONFIRMED.labels(authority=authority).inc()
        return Confirmation(
            claim_id=new_claim_id,
            confirms_claim_id=claim_id,
            source_authority=authority,
            confidence=confidence,
            bucket=confirmed_bucket,
            hold_until=hold_until,
        )

    async def adjudicate(
        self,
        ctx: TenantContext,
        *,
        claim_id: uuid.UUID,
        verdict: str,
        observed_confidence: float,
        note: str | None = None,
    ) -> None:
        """Record whether a claim turned out to be correct.

        The only input a calibration can ever be fitted from, which is why this
        exists before anything consumes it: without judged outcomes there is no
        path out of the uncalibrated state, ever.

        `observed_confidence` is what the reviewer was looking at, aged to that
        moment. Taken from the caller rather than recomputed here because a score
        works out differently at a different instant, and calibrating against a
        number nobody saw would measure the wrong thing.
        """
        if verdict not in VERDICTS:
            msg = f"unknown verdict {verdict!r}; expected one of {sorted(VERDICTS)}"
            raise ValidationError(msg)
        if not 0.0 <= observed_confidence <= 1.0:
            msg = f"observed confidence must be within [0,1], got {observed_confidence}"
            raise ValidationError(msg)

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            claim = (
                await session.execute(
                    text(
                        "SELECT calibration_version, provider_confidence, source_authority "
                        "FROM memory_claims WHERE claim_id = :cid"
                    ),
                    {"cid": claim_id},
                )
            ).one_or_none()
            if claim is None:
                msg = f"claim {claim_id} not found"
                raise NotFoundError(msg)

            await session.execute(
                text(
                    "INSERT INTO memory_claim_adjudication "
                    "  (tenant_id, claim_id, adjudicated_by, verdict, observed_confidence, "
                    "   observed_bucket, calibration_version, provider_confidence, "
                    "   source_authority, note, adjudicated_at) "
                    "VALUES (:tid, :cid, :actor, :verdict, CAST(:conf AS NUMERIC), :bucket, "
                    "        :calib, CAST(:prov AS NUMERIC), :auth, :note, "
                    "        CAST(:now AS TIMESTAMPTZ)) "
                    "ON CONFLICT (claim_id, adjudicated_by) DO UPDATE "
                    "SET verdict = EXCLUDED.verdict, "
                    "    observed_confidence = EXCLUDED.observed_confidence, "
                    "    observed_bucket = EXCLUDED.observed_bucket, "
                    "    note = EXCLUDED.note, "
                    "    adjudicated_at = EXCLUDED.adjudicated_at"
                ),
                {
                    "tid": ctx.tenant_id,
                    "cid": claim_id,
                    "actor": ctx.actor_id,
                    "verdict": verdict,
                    "conf": round(observed_confidence, 3),
                    "bucket": bucket_for(observed_confidence),
                    "calib": claim.calibration_version or "uncalibrated",
                    "prov": claim.provider_confidence,
                    "auth": claim.source_authority,
                    "note": note,
                    "now": now,
                },
            )
            # The note's presence is recorded, not its text -- a free-form review
            # comment can carry whatever the reviewer typed, and the audit trail
            # only needs to answer "was one left", not repeat it.
            await self._audit(
                session,
                action=actions.CLAIM_ADJUDICATED,
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                target_id=claim_id,
                payload={
                    "verdict": verdict,
                    "observed_confidence": round(observed_confidence, 3),
                    "note_present": note is not None,
                },
                now=now,
            )

        _ADJUDICATED.labels(verdict=verdict).inc()

    async def can_supersede(self, *, candidate_authority: str, incumbent_authority: str) -> bool:
        """Whether one authority tier may replace another.

        Equal or higher only. No machine tier is equal to a human one, so model
        output cannot quietly overturn a human decision -- it can contest, which
        lowers both scores and routes the pair for review, but that is a different
        thing from replacing it.

        Compared by rank rather than by name, so adding a tier cannot accidentally
        create a pair that compares the wrong way.
        """
        candidate = SOURCE_AUTHORITY_RANK.get(candidate_authority)
        incumbent = SOURCE_AUTHORITY_RANK.get(incumbent_authority)
        if candidate is None or incumbent is None:
            return False
        return candidate <= incumbent

    async def _actor_is_human(self, session: AsyncSession, ctx: TenantContext) -> bool:
        kind = (
            await session.execute(
                text("SELECT actor_kind FROM actors WHERE actor_id = :aid AND tenant_id = :tid"),
                {"aid": ctx.actor_id, "tid": ctx.tenant_id},
            )
        ).scalar_one_or_none()
        return bool(kind == "human")

    async def _audit(
        self,
        session: AsyncSession,
        *,
        action: str,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        target_id: uuid.UUID,
        payload: dict[str, Any],
        now: datetime.datetime,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO audit_log "
                "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
                "   before_jsonb, after_jsonb, ts, request_id, error_code) "
                "VALUES (:audit_id, :tid, :aid, :action, 'memory_claim', :target, NULL, "
                "        CAST(:after AS JSONB), :now, NULL, NULL)"
            ),
            {
                "audit_id": uuid.uuid4(),
                "tid": tenant_id,
                "aid": actor_id,
                "action": action,
                "target": target_id,
                "after": json.dumps(payload, sort_keys=True),
                "now": now,
            },
        )


__all__ = [
    "VERDICTS",
    "VERDICT_CORRECT",
    "VERDICT_INCORRECT",
    "VERDICT_UNDECIDABLE",
    "Confirmation",
    "ConfirmationService",
]
