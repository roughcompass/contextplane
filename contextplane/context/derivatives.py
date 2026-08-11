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

import dataclasses
import datetime
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.retention import derivatives, policies, tombstones
from contextplane.types import Clock, SystemClock, TenantContext

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


@dataclasses.dataclass(frozen=True)
class _ActorSource:
    """One class's "which rows did this actor author" query, and how it spells an actor.

    The spelling is a field rather than a detail of the SQL string because the five
    tables genuinely disagree, and the disagreement is not cosmetic: binding the
    wrong shape is either an error asyncpg raises or, worse, a comparison that
    silently matches nothing and reports an erasure that scheduled no work.
    """

    sql: str
    #: Whether the table stores the actor as a real `uuid` (a foreign key into
    #: `actors`) or as the text form of that id. Four of the five store text: they
    #: record an author who need not be an actor row at all, so a `uuid` column
    #: with a foreign key would refuse the rows those tables exist to accept.
    stores_uuid: bool
    #: Whether the table also records what *kind* of author it names, in which case
    #: only the actor origins count. See `policies.ACTOR_ORIGIN_TYPES` for why an
    #: id-only match is wrong here.
    filters_origin_type: bool = False


#: How each class finds the rows one actor authored. Written here rather than
#: pushed into each owning module because the erasure is the only caller that needs
#: "by actor" for all five, and five modules each growing an erasure-shaped query is
#: how the coverage list stops being one list.
#:
#: **Every column below is the column the table actually has.** An earlier version of
#: this map guessed a uniform `<role>_actor_id` naming that four of the five tables
#: never adopted, and because no test drove this path against a real database the
#: participant raised `UndefinedColumn` on the first class it reached — every real
#: erasure died there, after the participants ahead of it had already deleted rows.
#: Nothing about the shape of this dict prevents that recurring, so the integration
#: tier runs all five against real Postgres.
_ACTOR_SOURCES: dict[str, _ActorSource] = {
    # `author` holds the text form of the actor id, and the checkpoint tenant is the
    # workspace's own tenant.
    policies.RECORD_TASK_CHECKPOINT: _ActorSource(
        sql="SELECT checkpoint_id AS id FROM task_checkpoints WHERE tenant_id = :tenant AND author = :actor",
        stores_uuid=False,
    ),
    policies.RECORD_EXTERNAL_SIGNAL: _ActorSource(
        sql=(
            "SELECT signal_id AS id FROM external_signals "
            "WHERE tenant_id = :tenant AND producer_id = :actor AND producer_type = ANY(:origin_types)"
        ),
        stores_uuid=False,
        filters_origin_type=True,
    ),
    policies.RECORD_CONTEXT_FEEDBACK: _ActorSource(
        sql=(
            "SELECT feedback_id AS id FROM context_feedback "
            "WHERE tenant_id = :tenant AND reporter_id = :actor AND reporter_type = ANY(:origin_types)"
        ),
        stores_uuid=False,
        filters_origin_type=True,
    ),
    policies.RECORD_CONTEXT_RECEIPT: _ActorSource(
        sql="SELECT receipt_id AS id FROM context_receipts WHERE tenant_id = :tenant AND requested_by = :actor",
        stores_uuid=False,
    ),
    # The one table that keys the author by a real actor row — and the one that
    # scopes by `author_tenant_id` rather than `tenant_id`. A claim carries two
    # tenants: the one that owns the *subject* and the one whose actor asserted it.
    # Erasing a person reaches what they wrote, so the author tenant is the scope;
    # matching on the owning tenant would miss every claim they asserted about
    # another tenant's subject and would sweep in claims other people wrote about
    # this tenant's.
    policies.RECORD_MEMORY_CLAIM: _ActorSource(
        sql="SELECT claim_id AS id FROM memory_claims WHERE author_tenant_id = :tenant AND author_actor_id = :actor",
        stores_uuid=True,
    ),
}


class ContextDerivativeErasure:
    """Schedules derivative propagation for everything one actor authored.

    Registered in the `ErasureRegistry` before every participant that deletes source
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
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._salts = salts
        # `Clock` says it plainly: all service code takes one and never calls
        # `datetime.now()`. This class did call it, and the cost was not
        # theoretical -- the moment it stamps into `available_at` is the moment
        # the drain compares against, so a caller that runs the drain at a fixed
        # instant could not enqueue at that instant, and its work stayed
        # invisible to the query that was supposed to claim it. Defaulted rather
        # than required so the composition root keeps constructing this the way
        # it already does.
        self._clock: Clock = clock if clock is not None else SystemClock()

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Tombstone the actor's records and enqueue every derivative built from them."""
        now = self._clock.now()
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
                # Text, not the uuid: `request_authority` records who asked as a
                # string — an authority need not be an actor row — and asyncpg
                # refuses a UUID for a text parameter rather than coercing it.
                "authority": str(ctx.actor_id),
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
        """The ids of the rows this actor authored in one class.

        The parameters are built from the class's own spelling rather than passed
        uniformly: asyncpg does not coerce a `UUID` into a text comparison, and a
        text id compared against a `uuid` column is an error rather than a miss —
        which is the good case. The bad case is a shape that compares cleanly and
        matches nothing, and reports an erasure that scheduled no work.
        """
        source = _ACTOR_SOURCES[record_class]
        params: dict[str, object] = {
            "tenant": tenant_id,
            "actor": actor_id if source.stores_uuid else str(actor_id),
        }
        if source.filters_origin_type:
            params["origin_types"] = list(policies.ACTOR_ORIGIN_TYPES)
        rows = await session.execute(text(source.sql), params)
        return [uuid.UUID(str(row[0])) for row in rows.all()]
