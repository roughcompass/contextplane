"""Persist what one resolution returned, and everything it dropped on the way.

A receipt that cannot be found by the work it describes is not evidence, and a
receipt that records only what survived is not evidence either. Both failures
look identical from the outside: a stored row, a green write, an answer nobody
can check afterwards.

**Exclusions are written or the receipt is not written.** The assembler produces
selection evidence because it is the only place that still knows what was
dropped and why -- the envelope carries what survived, so nothing downstream can
reconstruct it. A writer that persisted the items and discarded the exclusions
would look exactly like a working receipt writer, and the first person to notice
would be someone asking why an answer looked thin and finding no record that
anything was withheld.

**Trust is written per item, in columns.** Not because a blob would be harder to
write -- it would be easier -- but because the release gate has to assert
label coverage over these rows, and a gate that has to parse a blob carries its
own copy of the schema, which is the copy that stops matching.

**One transaction.** A receipt whose arms committed and whose exclusions did not
is worse than no receipt: it reads as complete.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from contextplane.context.derivative_handlers import register_receipt_links, source_refs_for
from contextplane.context.models_receipt import (
    ContextReceipt,
    ContextReceiptArm,
    ContextReceiptExclusion,
    ContextReceiptItem,
)
from contextplane.context.schemas.envelope import BLOCK_CANONICAL

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.context.assembler import AssemblyResult, SelectionEvidence
    from contextplane.context.schemas.envelope import ContextEnvelopeV1
    from contextplane.types import Clock, TenantContext


def request_digest(request: Mapping[str, Any]) -> str:
    """A stable digest of the request a resolution answered.

    Sorted keys and a compact separator so the same request digests the same
    however the mapping was built. Two resolutions are only comparable if the
    thing they answered is comparable, and a digest that changed with key order
    would make every comparison a false negative.
    """
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


#: What a receipt says about its own completeness.
#:
#: `failed` is a resting state rather than a transient. Hydration that gave up
#: has to leave evidence it gave up: a row stuck at `pending` forever is
#: indistinguishable from one still in flight, and a reader owes those two
#: different answers -- "wait" against "this will never be evidence".
HYDRATION_PENDING = "pending"
HYDRATION_COMPLETE = "complete"
HYDRATION_FAILED = "failed"

#: The states in which a receipt may be shown as evidence of what was served.
#: Exactly one, and named rather than written as `== "complete"` at each read,
#: so a fourth state cannot be added without every reader being revisited.
HYDRATION_SERVABLE: frozenset[str] = frozenset({HYDRATION_COMPLETE})


class ContextReceiptService:
    """Writes receipts, and reads them back by their own id."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def record(
        self,
        ctx: TenantContext,
        *,
        result: AssemblyResult,
        intent_id: uuid.UUID | None = None,
        request: Mapping[str, Any] | None = None,
    ) -> uuid.UUID:
        """Store one resolution whole, and return its receipt id.

        Everything goes in one transaction. A receipt whose arms committed and
        whose exclusions did not is worse than no receipt at all, because it
        reads as a complete record of a resolution that withheld nothing.

        The derivative registration is in that same transaction for the same
        reason and one more. A receipt holds what somebody read, verbatim, so it
        is a derivative of every record it quoted -- and an unregistered
        derivative is one no erasure reaches and no expiry sweeps. Written
        afterwards it would be a second commit that can fail on its own, and the
        receipt left behind would be invisible to the machinery that has to reach
        it, while looking exactly like a receipt that had nothing to register.

        **The registration is per receipt, not per item.** An item is not
        separately addressable -- minimizing a receipt reduces all of its items in
        one pass, and there is no operation that reduces one and leaves the rest --
        so a registration per item would multiply rows without ever being acted on
        individually. The link table is what keeps the finer information: one
        registration carries a link to every record the receipt quoted, and expires
        with the earliest of them.
        """
        receipt_id = uuid.uuid4()
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            session.add(
                ContextReceipt(
                    receipt_id=receipt_id,
                    tenant_id=ctx.tenant_id,
                    intent_id=intent_id,
                    state=result.envelope.state,
                    cacheable=result.envelope.quality.cacheable,
                    # Written whole in this transaction, so it is complete when
                    # it is committed. Stated rather than defaulted: the value
                    # is a claim about this write path, and when hydration
                    # becomes asynchronous the claim changes here rather than
                    # somewhere a reader has to go looking for.
                    hydration_state=HYDRATION_COMPLETE,
                    item_count=sum(len(block.items) for block in result.envelope.blocks),
                    exclusion_count=sum(len(arm.exclusions) for arm in result.evidence),
                    resolved_at=now,
                    requested_by=str(ctx.actor_id),
                    request_digest=request_digest(request) if request is not None else None,
                )
            )
            self._add_arms(session, receipt_id=receipt_id, result=result)
            self._add_items(session, receipt_id=receipt_id, envelope=result.envelope)
            self._add_exclusions(session, receipt_id=receipt_id, evidence=result.evidence)
            await register_receipt_links(
                session,
                tenant_id=ctx.tenant_id,
                receipt_id=receipt_id,
                sources=source_refs_for(result.envelope, now=now),
                now=now,
            )

        return receipt_id

    def _add_arms(
        self,
        session: AsyncSession,
        *,
        receipt_id: uuid.UUID,
        result: AssemblyResult,
    ) -> None:
        """One row per arm, carrying both what it said and what it cost."""
        by_block = {item.block: item for item in result.evidence}
        for block in result.envelope.blocks:
            evidence = by_block.get(block.name)
            session.add(
                ContextReceiptArm(
                    arm_id=uuid.uuid4(),
                    receipt_id=receipt_id,
                    block=block.name,
                    state=block.state,
                    reason=block.reason,
                    considered=evidence.considered if evidence else None,
                    returned=evidence.returned if evidence else None,
                    truncated_by_arm=evidence.truncated_by_arm if evidence else None,
                    truncated_by_cap=evidence.truncated_by_cap if evidence else None,
                    fresh_as_of=evidence.fresh_as_of if evidence else None,
                    stale=evidence.stale if evidence else None,
                    duration_ms=evidence.duration_ms if evidence else None,
                )
            )

    def _add_items(
        self,
        session: AsyncSession,
        *,
        receipt_id: uuid.UUID,
        envelope: ContextEnvelopeV1,
    ) -> None:
        """One row per item, with its trust spread across columns.

        Canonical items are written with no trust, which the database enforces
        as well: attaching one would invite the question of whether another
        authority could have supplied the registry's own answer.
        """
        for block in envelope.blocks:
            for item in block.items:
                trust = item.trust
                payload = item.payload
                session.add(
                    ContextReceiptItem(
                        item_row_id=uuid.uuid4(),
                        receipt_id=receipt_id,
                        receipt_item_id=item.receipt_item_id.value(),
                        block=block.name,
                        source=item.receipt_item_id.source,
                        item_key=item.receipt_item_id.item_key,
                        trust=trust.trust if trust else None,
                        trust_source=trust.source if trust else None,
                        assertion_kind=trust.assertion_kind if trust else None,
                        authority=trust.authority if trust else None,
                        freshness=trust.freshness if trust else None,
                        mutability=trust.mutability if trust else None,
                        attribution=trust.attribution if trust else None,
                        classification=trust.classification if trust else None,
                        # Carried on the payload rather than on the trust
                        # contract: which revision of a source an item came from
                        # is a property of that fetch, and the trust contract is
                        # frozen.
                        source_revision=_optional_str(payload.get("source_revision")),
                        source_digest=_optional_str(payload.get("source_digest")),
                    )
                )

    def _add_exclusions(
        self,
        session: AsyncSession,
        *,
        receipt_id: uuid.UUID,
        evidence: Sequence[SelectionEvidence],
    ) -> None:
        """Everything an arm found and did not return.

        Written unconditionally. This is the part of a receipt that answers "was
        there more than this", and an answer that omits it is indistinguishable
        from one where there genuinely was nothing.
        """
        for arm in evidence:
            for exclusion in arm.exclusions:
                session.add(
                    ContextReceiptExclusion(
                        exclusion_id=uuid.uuid4(),
                        receipt_id=receipt_id,
                        block=arm.block,
                        item_key=exclusion.item_key,
                        reason=exclusion.reason,
                    )
                )

    # -- reads -----------------------------------------------------------

    async def get(self, ctx: TenantContext, *, receipt_id: uuid.UUID) -> ContextReceipt | None:
        """One receipt by id, scoped to the tenant that owns it.

        The tenant predicate is in the SELECT rather than checked afterwards. A
        read that loaded the row and then compared has already loaded a row it
        may not return, and the comparison is one refactor from disappearing.
        """
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(ContextReceipt).where(
                        ContextReceipt.receipt_id == receipt_id,
                        ContextReceipt.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()

    async def exclusions_for(
        self,
        ctx: TenantContext,
        *,
        receipt_id: uuid.UUID,
        block: str | None = None,
    ) -> tuple[ContextReceiptExclusion, ...]:
        """What one resolution withheld, optionally for one block.

        Joined back through the receipt so the tenant predicate applies: the
        exclusions table carries no tenant of its own, and reading it by
        `receipt_id` alone would return another tenant's withholding to anyone
        who guessed an id.
        """
        stmt = (
            select(ContextReceiptExclusion)
            .join(ContextReceipt, ContextReceipt.receipt_id == ContextReceiptExclusion.receipt_id)
            .where(
                ContextReceiptExclusion.receipt_id == receipt_id,
                ContextReceipt.tenant_id == ctx.tenant_id,
            )
            .order_by(ContextReceiptExclusion.block, ContextReceiptExclusion.item_key)
        )
        if block is not None:
            stmt = stmt.where(ContextReceiptExclusion.block == block)

        async with self._session_factory() as session:
            return tuple((await session.execute(stmt)).scalars().all())

    async def arms_for(self, ctx: TenantContext, *, receipt_id: uuid.UUID) -> tuple[ContextReceiptArm, ...]:
        """Which blocks answered one resolution, and how each of them did.

        Joined back through the receipt for the tenant predicate, exactly as
        `exclusions_for` is and for the same reason: the arms table carries no
        tenant of its own, so reading it by `receipt_id` alone would hand
        another tenant's resolution shape to anyone who guessed an id.

        Ordered by block so two reads of one receipt produce the same sequence.
        A caller digesting this -- and a handoff does -- would otherwise get a
        different digest depending on how the rows came back.
        """
        stmt = (
            select(ContextReceiptArm)
            .join(ContextReceipt, ContextReceipt.receipt_id == ContextReceiptArm.receipt_id)
            .where(
                ContextReceiptArm.receipt_id == receipt_id,
                ContextReceipt.tenant_id == ctx.tenant_id,
            )
            .order_by(ContextReceiptArm.block)
        )
        async with self._session_factory() as session:
            return tuple((await session.execute(stmt)).scalars().all())


def _optional_str(value: object) -> str | None:
    """A string, or nothing. Never the word "None"."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def canonical_items_carry_no_trust(envelope: ContextEnvelopeV1) -> bool:
    """The invariant the item writer relies on, checkable by a caller.

    The envelope contract already enforces it in memory and the database
    enforces it again on the way in. This exists so a test can state the rule
    without reaching into either.
    """
    return all(item.trust is None for block in envelope.blocks if block.name == BLOCK_CANONICAL for item in block.items)


__all__ = [
    "ContextReceiptService",
    "canonical_items_carry_no_trust",
    "request_digest",
]
