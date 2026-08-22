"""The two decisions a human curator makes about an existing claim.

Split out of the module that used to hold every claim-lifecycle concern in one
file, once that file crossed the line-count ceiling this program enforces on
service modules. `link_subject` and `discard` are grouped separately from the
machine/system write path in `claim_writer.py` because they answer a different
question: `stage_claim` decides what a claim *is*, from evidence the caller
cannot forge; these two decide what happens to a claim that already exists,
and both require the standing of a producer or admin rather than following
from anything a machine's own evidence implies.

This module is a mixin (`_ClaimCuratorActionsMixin`), composed into
`ClaimService` in `claim_writer.py` alongside `_ClaimResolutionMixin` from
`claim_authority.py`. It depends on `claim_authority.py` for the resolution
helpers it calls (`_resolve_subject`, `_derive_visibility`,
`_rederive_authority`) and, by convention rather than import, on
`claim_writer.py`'s own `_rescore`/`rescore_existing` -- both reached through
`self`, resolved at the composed class's method-resolution order rather than
through any import here. That is why this file imports neither sibling: a
mixin's methods run against whatever `self` the composing class provides, and
`ClaimService` is the only place all three are actually joined together.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any, Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.audit import actions
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.memory.claim_authority import (
    STATUS_STAGED,
    STATUS_UNLINKED,
    UNCALIBRATED,
    StagedClaim,
    _Subject,
)
from contextplane.service.memory.confidence import SCORER_VERSION, ConfidencePolicy
from contextplane.service.memory.confidence import score as score_confidence
from contextplane.service.memory.confidence_decay import half_life_days
from contextplane.service.memory.confidence_read import subject_change_profile
from contextplane.service.memory.contest import detect_for_claim
from contextplane.service.memory.predicate_churn import inspected_half_lives
from contextplane.service.retrieval.embedding_index import project_claim
from contextplane.types import Clock, TenantContext

# Who may act on the queue's two curator decisions -- linking a subjectless
# claim to an entity, or discarding one outright. Both assert something no
# machine's own evidence implies, so both require the same standing
# `PromotionService`'s review roles do rather than a bespoke notion of who
# may curate.
_CURATOR_ROLES: Final[frozenset[str]] = frozenset({"producer", "admin"})


class _ClaimServiceHost(Protocol):
    """The composed class's own state and sibling-mixin methods this mixin's
    methods call through `self`.

    Spelled out as a structural type (rather than left as an implicit
    duck-typed `self`) because `mypy --strict` checks each mixin's method
    bodies against its own class, not against whatever class eventually
    composes it -- without this, every cross-mixin call reads as an unknown
    attribute. `ClaimService` (`claim_writer.py`) satisfies this structurally
    by inheriting `_ClaimResolutionMixin` (for the three resolution methods)
    and defining `__init__`/`_rescore`/`rescore_existing` itself; nothing here
    imports either of those, this protocol is the only coupling.
    """

    _session_factory: async_sessionmaker[AsyncSession]
    _clock: Clock

    async def _resolve_subject(self, session: AsyncSession, ctx: TenantContext, reference: str) -> _Subject: ...

    async def _derive_visibility(self, session: AsyncSession, subject: _Subject, *, requested: str | None) -> str: ...

    async def _rederive_authority(
        self,
        session: AsyncSession,
        *,
        claim_id: uuid.UUID,
        subject: _Subject,
        linking_tenant_id: uuid.UUID,
    ) -> str: ...

    async def _rescore(
        self,
        session: AsyncSession,
        *,
        claim_id: uuid.UUID,
        status: str,
        authority: str,
        is_contested: bool,
        policy: ConfidencePolicy,
        now: datetime.datetime,
    ) -> None: ...

    async def rescore_existing(
        self,
        session: AsyncSession,
        *,
        claim_id: uuid.UUID,
        policy: ConfidencePolicy,
        now: datetime.datetime,
    ) -> None: ...

    # Same-class, not cross-mixin -- `_audit` is defined below in
    # `_ClaimCuratorActionsMixin` itself. Listed here anyway because a typed
    # `self` replaces the inferred type entirely: mypy checks `link_subject`
    # and `discard`'s bodies against exactly this protocol, nothing wider.
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
    ) -> None: ...


class _ClaimCuratorActionsMixin:
    """`link_subject`, `discard`, and the audit write both of them make.

    Assumes composition with a class that provides `_session_factory`,
    `_clock` (set by `ClaimService.__init__` in `claim_writer.py`) and
    `_resolve_subject`/`_derive_visibility`/`_rederive_authority` (from
    `_ClaimResolutionMixin`) and `_rescore`/`rescore_existing` (from
    `claim_writer.py`'s own `ClaimService` methods) -- the same set of
    cooperating pieces `stage_claim` itself depends on, called the same way.
    Typed against `_ClaimServiceHost` below rather than left implicit, so
    `mypy --strict` can verify these calls without seeing `ClaimService` itself.
    """

    async def link_subject(
        self: _ClaimServiceHost,
        ctx: TenantContext,
        *,
        claim_id: uuid.UUID,
        subject_reference: str,
    ) -> StagedClaim:
        """The unlinked-to-staged transition: give a subjectless claim a home.

        Everything that follows from having a subject -- who owns it, how visible
        the claim may be, which authority tier it carries, whether it disagrees
        with something already stored -- was undecidable while the reference did
        not resolve. This re-derives each of those exactly as staging would have,
        now that resolution has succeeded, and writes the result in one statement
        so the row is never observable half-linked: the CHECK constraints this
        module already relies on tie subject, owner, and every paired confidence
        column together, and a write that set only some of them would be refused.

        Curator-only: linking asserts what a claim is about, which is a decision a
        producer or admin makes, not something a machine's own evidence implies.
        """
        if not (set(ctx.roles) & _CURATOR_ROLES):
            raise PermissionError("linking a claim to a subject requires the producer or admin role")

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            claim = (
                await session.execute(
                    text(
                        "SELECT author_tenant_id, status, predicate, value_jsonb AS value, "
                        "       claim_category "
                        "FROM memory_claims WHERE claim_id = :cid FOR UPDATE"
                    ),
                    {"cid": claim_id},
                )
            ).one_or_none()
            if claim is None:
                raise NotFoundError(f"claim {claim_id} not found")
            # An unlinked claim has no owner yet to scope its queue to, so it is
            # the author's queue it sits in -- the same rule the queue's own
            # listing uses.
            if claim.author_tenant_id != ctx.tenant_id:
                raise PermissionError("only the tenant this claim's queue belongs to may link it")
            if claim.status != STATUS_UNLINKED:
                raise ConflictError(f"claim {claim_id} is already {claim.status}, not unlinked")

            subject = await self._resolve_subject(session, ctx, subject_reference)
            if subject.entity_id is None or subject.owning_tenant_id is None:
                raise ValidationError(f"{subject_reference!r} still does not resolve to any entity; nothing to link")
            owning_tenant_id = subject.owning_tenant_id

            # Never broader than the subject describes. Passing no requested value
            # is deliberate: whatever visibility was asked for when the claim was
            # first staged was already discarded in favour of 'private' the moment
            # the subject failed to resolve, and reviving it here would let a
            # request nobody could evaluate at the time take effect retroactively.
            resolved_visibility = await self._derive_visibility(session, subject, requested=None)
            authority = await self._rederive_authority(
                session, claim_id=claim_id, subject=subject, linking_tenant_id=ctx.tenant_id
            )

            policy = ConfidencePolicy()
            initial = score_confidence(authority=authority, policy=policy)
            if initial is None:  # pragma: no cover - every tier _rederive_authority returns has a base score
                raise ValidationError(f"authority {authority!r} has no base confidence to score against")

            # Knowable only now: how fast this subject actually changes, which
            # needs the subject's own history and had no subject to read until
            # this moment.
            median_change, observations = await subject_change_profile(session, entity_id=subject.entity_id, now=now)
            half_life = half_life_days(
                claim.claim_category,
                predicate=claim.predicate,
                fitted_half_lives=await inspected_half_lives(session),
                subject_median_change_days=median_change,
                subject_change_observations=observations,
                tenant_multiplier=policy.decay_multiplier,
            )

            await session.execute(
                text(
                    "UPDATE memory_claims SET "
                    "  subject_entity_id = :sid, "
                    "  owning_tenant_id = :owner, "
                    "  status = 'staged', "
                    "  visibility = :vis, "
                    "  source_authority = :auth, "
                    "  confidence = CAST(:conf AS NUMERIC), "
                    "  confidence_scored_at = CAST(:now AS TIMESTAMPTZ), "
                    "  confidence_inputs = CAST(:conf_in AS JSONB), "
                    "  scorer_version = :scorer, "
                    "  calibration_version = :calib, "
                    "  decay_half_life_days = CAST(:half_life AS NUMERIC) "
                    "WHERE claim_id = :cid AND status = 'unlinked'"
                ),
                {
                    "sid": subject.entity_id,
                    "owner": owning_tenant_id,
                    "vis": resolved_visibility,
                    "auth": authority,
                    "conf": initial.value,
                    "now": now,
                    "conf_in": json.dumps(initial.inputs.as_json(), sort_keys=True),
                    "scorer": SCORER_VERSION,
                    "calib": UNCALIBRATED,
                    "half_life": half_life,
                    "cid": claim_id,
                },
            )

            # Only reachable now: a claim excluded from every neighbourhood query
            # by having no subject can, the moment it gets one, disagree with
            # something that was there all along.
            contest = await detect_for_claim(session, claim_id=claim_id, now=now)
            await self._rescore(
                session,
                claim_id=claim_id,
                status=STATUS_STAGED,
                authority=authority,
                is_contested=contest.is_contested,
                policy=policy,
                now=now,
            )
            # The other side of every newly-detected pair loses the same ground
            # this claim does; leaving it on its previous, uncontested score would
            # show a conflicted pair where only one side reads as disputed.
            for other in contest.counterparties(claim_id):
                await self.rescore_existing(session, claim_id=other, policy=policy, now=now)

            await self._audit(
                session,
                action=actions.CLAIM_LINKED,
                tenant_id=owning_tenant_id,
                actor_id=ctx.actor_id,
                target_id=claim_id,
                payload={
                    "subject_entity_id": str(subject.entity_id),
                    "subject_reference": subject_reference,
                    "source_authority": authority,
                },
                now=now,
            )

        return StagedClaim(
            claim_id=claim_id,
            subject_entity_id=subject.entity_id,
            predicate=claim.predicate,
            value=claim.value,
            status=STATUS_STAGED,
            visibility=resolved_visibility,
            owning_tenant_id=owning_tenant_id,
            source_authority=authority,
            is_contested=contest.is_contested,
        )

    async def discard(self: _ClaimServiceHost, ctx: TenantContext, *, claim_id: uuid.UUID, reason: str) -> None:
        """Refuse a claim outright: `status='rejected'`, and it never serves again.

        Distinct from a promotion rejection, which refuses one proposed write to
        the canonical graph while the claim itself stays staged and keeps serving.
        This is the queue's own verdict on the claim -- wrong, spurious, not worth
        pursuing -- closing it the way a lost contest or a human confirmation
        would, just without a survivor to point at.

        Also the unlinked claim's own way out. A reference that will never
        resolve -- a typo, a decommissioned system, a name nobody will ever
        create -- would otherwise sit in the queue forever: it cannot be scored
        (nothing has determined what it would even be scored against), and
        `link_subject` only ever moves a claim *to* a real subject, never to a
        deliberate dead end. The schema permits exactly this one subjectless
        terminal shape -- `rejected`, subject and confidence still both NULL --
        so an unlinked claim discards the same way a staged one does, leaving
        every field that would assert what it is about untouched.

        Curator-only, the same bar `link_subject` sets: rejecting what somebody
        else observed is a decision, not something the evidence implies on its own.
        """
        if not (set(ctx.roles) & _CURATOR_ROLES):
            raise PermissionError("discarding a claim requires the producer or admin role")

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            claim = (
                await session.execute(
                    text(
                        "SELECT owning_tenant_id, author_tenant_id, subject_entity_id, status "
                        "FROM memory_claims WHERE claim_id = :cid FOR UPDATE"
                    ),
                    {"cid": claim_id},
                )
            ).one_or_none()
            if claim is None:
                raise NotFoundError(f"claim {claim_id} not found")

            # The tenant whose queue this claim occupies: the subject's owner once
            # one is known, the author until then -- the same COALESCE the queue's
            # own listing scopes itself by.
            claim_tenant_id = claim.owning_tenant_id if claim.owning_tenant_id is not None else claim.author_tenant_id
            if claim_tenant_id != ctx.tenant_id:
                raise PermissionError("only the tenant this claim's queue belongs to may discard it")

            if claim.status not in (STATUS_STAGED, STATUS_UNLINKED):
                raise ConflictError(f"claim {claim_id} is {claim.status}, not staged or unlinked; nothing to discard")

            await session.execute(
                text(
                    "UPDATE memory_claims SET status = 'rejected' "
                    "WHERE claim_id = :cid AND status IN ('staged', 'unlinked')"
                ),
                {"cid": claim_id},
            )
            # A discarded claim is no longer servable, so its vectors have to go
            # -- the same call `close_superseded` and `mark_consolidated` make,
            # for the same reason. Left behind they cannot produce a wrong
            # answer, because every read filters on `status`; but each dead
            # vector occupies a candidate slot in `ORDER BY vector <-> q LIMIT k`,
            # which is a silent recall loss on the queries that do matter.
            #
            # A `staged` claim reaching here may well be indexed, so this is not
            # defensive: `embedding_index.project_claim` described itself as
            # "Called from the two places that change whether a claim is
            # servable" while this was a third, and the vectors it left were
            # bounded only by retention expiry.
            #
            # In the same transaction as the status write, so the row and the
            # index cannot disagree if the request dies between them.
            await project_claim(session, claim_id=claim_id, now=now)
            await self._audit(
                session,
                action=actions.CLAIM_DISCARDED,
                tenant_id=claim_tenant_id,
                actor_id=ctx.actor_id,
                target_id=claim_id,
                payload={"reason": reason},
                now=now,
            )

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


__all__ = ["_ClaimCuratorActionsMixin"]
