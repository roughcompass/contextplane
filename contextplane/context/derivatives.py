"""The context subsystem's derivatives, and its participation in an erasure.

Every other erasure participant deletes rows it owns. This one does something
different and it is worth being clear about why it is not the same job: the rows a
context erasure has to reach are not all *rows*. A vector, a full-text document, a
cached answer, an export and a summary are each built from a record and stored
somewhere the record's own table knows nothing about, and each of them holds the
erased person's words verbatim. Deleting the source and stopping there leaves the
copies searchable.

So this participant does not delete artefacts directly. It writes the tombstone
that authorises the removal, then enqueues one propagation item per derivative
built from the erased records, and the drain in
`workers/derivative_propagation.py` applies them through the handler registered for
each kind. Three properties follow from that split, and all three are the reason
for it:

- **The tombstone is written first and in the same transaction as the enqueue.**
  An enqueue without a tombstone is work nobody authorised; a tombstone without an
  enqueue is an authorisation nobody acted on. Neither is recoverable by looking at
  the other, so they are one commit.
- **Coverage is a registry, not a call list.** A derivative kind with no handler is
  a build failure through `unhandled_kinds`, so a new artefact type cannot be added
  without either handling it or breaking the gate. A participant that enumerated
  kinds inline would simply not mention the new one.
- **Idempotence lives in the schema.** The outbox is unique per derivative, per
  operation, per trigger, per tombstone, so a retried erasure enqueues nothing the
  first attempt already enqueued. That is what makes retrying the normal recovery
  path rather than an amplifier.

**What this participant reports is what it scheduled, not what it removed.** The
count is propagation items, and calling them deletions would overstate: the
artefacts go when the drain runs. `pending_overdue` is where a reader asks whether
that has happened, and the fail-closed read path keys off it rather than off this
return value.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.retention import derivatives, policies, tombstones
from contextplane.types import TenantContext

_log = logging.getLogger(__name__)

#: The record classes an actor can author that carry their content into a
#: derivative. Closed, and ordered the way the erasure reads: the records whose
#: derivatives hold verbatim text come first, so a failure part-way through has
#: already scheduled the artefacts that matter most.
ACTOR_RECORD_CLASSES: tuple[str, ...] = (
    policies.RECORD_TASK_CHECKPOINT,
    policies.RECORD_EXTERNAL_SIGNAL,
    policies.RECORD_CONTEXT_FEEDBACK,
    policies.RECORD_CONTEXT_RECEIPT,
    policies.RECORD_MEMORY_CLAIM,
)

#: How each class finds the rows one actor authored. Written here rather than
#: pushed into each owning module because the erasure is the only caller that needs
#: "by actor" for all five, and five modules each growing an erasure-shaped query is
#: how the coverage list stops being one list.
_ACTOR_SOURCE_SQL: dict[str, str] = {
    policies.RECORD_TASK_CHECKPOINT: (
        "SELECT checkpoint_id AS id FROM task_checkpoints "
        "WHERE tenant_id = :tenant AND author_actor_id = :actor"
    ),
    policies.RECORD_EXTERNAL_SIGNAL: (
        "SELECT signal_id AS id FROM external_signals "
        "WHERE tenant_id = :tenant AND producer_actor_id = :actor"
    ),
    policies.RECORD_CONTEXT_FEEDBACK: (
        "SELECT feedback_id AS id FROM context_feedback "
        "WHERE tenant_id = :tenant AND actor_id = :actor"
    ),
    policies.RECORD_CONTEXT_RECEIPT: (
        "SELECT receipt_id AS id FROM context_receipts "
        "WHERE tenant_id = :tenant AND requested_by_actor_id = :actor"
    ),
    policies.RECORD_MEMORY_CLAIM: (
        "SELECT claim_id AS id FROM memory_claims "
        "WHERE tenant_id = :tenant AND asserted_by_actor_id = :actor"
    ),
}


class ContextDerivativeErasure:
    """Schedules derivative propagation for everything one actor authored.

    Registered in the `ErasureRegistry` after the participants that delete source
    rows, and that order is load-bearing: this reads the source tables to find what
    the actor wrote, so a participant that had already deleted them would leave
    nothing to enqueue and the artefacts built from them would survive the erasure
    that was supposed to reach them.
    """

    subsystem = "context_derivatives"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        salts: tombstones.TenantSaltResolver,
    ) -> None:
        self._session_factory = session_factory
        self._salts = salts

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Tombstone the actor's records and enqueue every derivative built from them."""
        now = datetime.datetime.now(datetime.UTC)
        scheduled: dict[str, int] = {}

        async with self._session_factory() as session:
            tombstone_id = await self._tombstone(session, ctx, target_actor_id, now)

            for record_class in ACTOR_RECORD_CLASSES:
                source_ids = await self._sources(session, ctx.tenant_id, target_actor_id, record_class)
                if not source_ids:
                    # A real answer, not a skip: this actor authored none of this
                    # class. Recorded as zero so the report distinguishes "nothing
                    # to do" from "never asked".
                    scheduled[record_class] = 0
                    continue

                scheduled[record_class] = await derivatives.enqueue_for_sources(
                    session,
                    tenant_id=ctx.tenant_id,
                    record_class=record_class,
                    source_ids=source_ids,
                    operation=derivatives.OPERATION_DELETE,
                    trigger=derivatives.TRIGGER_ERASURE,
                    now=now,
                    tombstone_id=tombstone_id,
                )

            # One commit: the tombstone that authorises the work and the work it
            # authorises land together or not at all.
            await session.commit()

        _log.info(
            "context_derivatives.erasure_scheduled: actor=%s scheduled=%s",
            target_actor_id,
            scheduled,
        )
        return scheduled

    async def _tombstone(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        target_actor_id: uuid.UUID,
        now: datetime.datetime,
    ) -> uuid.UUID:
        """Record what was erased, in a form that proves it without holding it.

        The proof is a tenant-keyed HMAC over the subject, so it can answer "was
        this erased?" without storing anything that reconstructs who. The salt is
        destroyed at offboarding, which is what stops the proof outliving the
        tenant's right to hold it.
        """
        proof = tombstones.mint_proof(
            self._salts.salt_for(ctx.tenant_id),
            record_class=policies.RECORD_DERIVATIVE,
            subject_id=target_actor_id,
            # The actor id is the subject and its own digest here: an actor is not a
            # record with content, so there is nothing else to commit to. Passing the
            # subject keeps the HMAC bound to who without inventing a content hash
            # that would only ever be this same value.
            content_digest=str(target_actor_id),
            effective_at=now,
        )
        tombstone_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO source_tombstones
                    (tombstone_id, tenant_id, record_class, subject_id, policy_version,
                     request_authority, reason, effective_at, proof_hmac, propagation_state)
                VALUES (:id, :tenant, :cls, :subject, :policy, :authority, :reason,
                        :now, :proof, :state)
                ON CONFLICT (tenant_id, record_class, subject_id) DO NOTHING
                """
            ),
            {
                "id": tombstone_id,
                "tenant": ctx.tenant_id,
                "cls": policies.RECORD_DERIVATIVE,
                "subject": target_actor_id,
                "policy": policies.POLICY_VERSION,
                "authority": ctx.actor_id,
                "reason": derivatives.TRIGGER_ERASURE,
                "now": now,
                "proof": proof,
                "state": "pending",
            },
        )

        # A retry finds the tombstone the first attempt wrote. Re-reading it rather
        # than reusing the generated id is what makes the outbox's per-tombstone
        # uniqueness hold across attempts instead of enqueuing the same work again
        # under a second tombstone.
        existing = await session.execute(
            text(
                "SELECT tombstone_id FROM source_tombstones "
                "WHERE tenant_id = :tenant AND record_class = :cls AND subject_id = :subject"
            ),
            {"tenant": ctx.tenant_id, "cls": policies.RECORD_DERIVATIVE, "subject": target_actor_id},
        )
        return uuid.UUID(str(existing.scalar_one()))

    async def _sources(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        record_class: str,
    ) -> list[uuid.UUID]:
        rows = await session.execute(
            text(_ACTOR_SOURCE_SQL[record_class]),
            {"tenant": tenant_id, "actor": actor_id},
        )
        return [uuid.UUID(str(row[0])) for row in rows.all()]
