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

import datetime
import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select

from contextplane.context.derivative_handlers import register_receipt_links, source_refs_for
from contextplane.context.models_receipt import (
    ContextReceipt,
    ContextReceiptArm,
    ContextReceiptExclusion,
    ContextReceiptItem,
)
from contextplane.context.schemas.envelope import BLOCK_CANONICAL
from contextplane.exceptions import ValidationError
from contextplane.workers.derivative_propagation import pending_overdue
from contextplane.workspaces import recall as workspace_recall

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


class ReceiptNotServable(Exception):
    """A receipt exists but may not be shown as evidence right now.

    Carries which of the two reasons applies, because they call for opposite
    actions: an unhydrated receipt is worth retrying, and a withheld one is not
    until the incident that withheld it is resolved.

    A dedicated type rather than a status code, so the refusal is decided once
    in the service and each transport renders it -- see `refuse_if_unservable`
    for why that had to move.
    """

    def __init__(self, receipt_id: uuid.UUID, *, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.receipt_id = receipt_id
        self.reason = reason
        self.detail = detail


#: Why a receipt is not servable. A closed pair: both are refusals, and a caller
#: branches on which one rather than on the message.
NOT_SERVABLE_UNHYDRATED = "receipt_not_hydrated"
NOT_SERVABLE_WITHHELD = "receipt_withheld"

#: The most receipts one listing returns. A bound rather than a target: this is
#: the only read here that is not keyed by an id, so it is the only one a caller
#: can ask to walk a tenant's whole history with.
MAX_RECEIPT_PAGE: Final[int] = 100


def refuse_if_unservable(receipt: ContextReceipt) -> None:
    """Refuse a receipt that must not be shown, whichever surface is asking.

    **Here rather than in the routers, because it was in one router and one
    transport went without it.** `api/routers/receipts.py` checked
    `hydration_state` before serving; the four MCP tools over the same three
    service reads checked nothing. `get_receipt_exclusions` even told its caller
    that "an empty list means nothing was withheld" -- the exact belief the REST
    409 exists to prevent, since an unhydrated receipt has recorded no
    exclusions yet. One surface refused and the other asserted the opposite
    about the same row.

    That is the third time in this codebase a guard written at a transport was
    missing from a second transport over the same service, so this one is a
    chokepoint the reads call and the transports only render.
    """
    if receipt.withheld_at is not None:
        raise ReceiptNotServable(
            receipt.receipt_id,
            reason=NOT_SERVABLE_WITHHELD,
            detail=(
                f"receipt {receipt.receipt_id} is withheld while an incident affecting its inputs is "
                "worked; it will serve again if the quarantine is reverted"
            ),
        )
    if receipt.hydration_state not in HYDRATION_SERVABLE:
        raise ReceiptNotServable(
            receipt.receipt_id,
            reason=NOT_SERVABLE_UNHYDRATED,
            detail=(
                f"receipt {receipt.receipt_id} is {receipt.hydration_state}, so what it served has not "
                "been recorded yet and an empty answer here would not mean nothing was withheld"
            ),
        )


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
        # **Deliberately does not refuse an unservable receipt.** The header is
        # the observability surface: `hydration_state` is how a caller polling
        # for a resolution it triggered learns to wait, and `withheld_at` is how
        # an operator learns *why* the evidence reads below are refusing.
        # Refusing here too would leave no way to observe the states these
        # columns exist to publish. The reads that return the receipt's content
        # do refuse -- see `_refuse_if_unservable`.
        async with self._session_factory() as session:
            return await self._header(session, ctx, receipt_id)

    async def recent(
        self, ctx: TenantContext, *, limit: int = 50, before: datetime.datetime | None = None
    ) -> tuple[ContextReceipt, ...]:
        """Recent receipts this caller may open, newest first.

        E23-T1. Every receipt read was keyed by an id the caller must already
        hold, so a reader with a question about "what did we serve this morning"
        had nowhere to start.

        **A row appears here only if the detail reads would serve it**, and that
        is the whole authorization argument. A list that showed a withheld
        receipt would be a way around the refusal `refuse_if_unservable` exists
        to make -- an operator would learn a resolution happened, when it
        happened and against what query, from a surface built because a
        different surface refuses to say. The filter is derived from the same
        two conditions that function raises on, and
        `test_the_listing_hides_exactly_what_the_detail_read_refuses` holds them
        equal rather than trusting this comment.

        **Absent rather than present-and-empty.** A withheld receipt rendered as
        a row with nothing in it still discloses that it exists, which is most of
        what withholding is protecting. It is not in the list at all, and the
        count a surface renders is a count of what it may show.

        **Keyset, not offset.** `before` is a timestamp the caller got from the
        last row it read, so a receipt written between two pages cannot shift the
        window and hide a row. An offset would silently skip exactly the rows a
        busy tenant most wants to see.
        """
        if not 1 <= limit <= MAX_RECEIPT_PAGE:
            raise ValidationError(f"limit is 1 to {MAX_RECEIPT_PAGE}, got {limit}")

        conditions = [
            ContextReceipt.tenant_id == ctx.tenant_id,
            # The two halves of `refuse_if_unservable`, as a predicate. Written
            # from the same constants it raises on, so a third hydration state
            # would change both or neither.
            ContextReceipt.withheld_at.is_(None),
            ContextReceipt.hydration_state.in_(sorted(HYDRATION_SERVABLE)),
        ]
        if before is not None:
            conditions.append(ContextReceipt.resolved_at < before)

        async with self._session_factory() as session:
            found = (
                await session.execute(
                    select(ContextReceipt)
                    .where(*conditions)
                    .order_by(ContextReceipt.resolved_at.desc(), ContextReceipt.receipt_id)
                    .limit(limit)
                )
            ).scalars()
        return tuple(found)

    @staticmethod
    async def _header(session: AsyncSession, ctx: TenantContext, receipt_id: uuid.UUID) -> ContextReceipt | None:
        """The receipt row, tenant-scoped, or `None`.

        In the caller's own session, for the reason `_refuse_if_overdue` is: a
        servability check that passed on another connection cannot vouch for a
        state this read never sees.
        """
        return (
            await session.execute(
                select(ContextReceipt).where(
                    ContextReceipt.receipt_id == receipt_id,
                    ContextReceipt.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def _refuse_if_unservable(self, session: AsyncSession, ctx: TenantContext, receipt_id: uuid.UUID) -> None:
        """Refuse before returning any part of a receipt that must not be shown.

        A missing receipt is left alone here: the row-level reads below return
        an empty tuple for one, which is what they returned before, and the
        transports already answer 404 from `get`.

        Without this the withheld state would be a filter rather than a refusal,
        and an empty exclusions list would mean "nothing was withheld" when it
        means "you may not see what was".
        """
        found = await self._header(session, ctx, receipt_id)
        if found is not None:
            refuse_if_unservable(found)

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

        **Guarded, and it is the only read on this surface that needs to be.**
        The `receipt_link` derivative handler is registered `blocking`, and what
        it does on propagation is `UPDATE context_receipt_exclusions SET
        item_key = :marker`. So an exclusion row read before that propagation
        lands carries an `item_key` the propagation is in the middle of
        withdrawing, which is exactly what `pending_overdue(blocking_only=True)`
        exists to refuse.

        Its two siblings do **not** need it, and the reason is the same rule
        read the other way: `get` returns the receipt header from
        `context_receipts`, and `arms_for` returns rows from
        `context_receipt_arms`. Neither table is touched by any blocking
        handler, so a guard on either could not fire -- which is the no-op
        `canonical_arm`'s entry in `arms.py` already refuses to add.
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
            await self._refuse_if_unservable(session, ctx, receipt_id)
            await self._refuse_if_overdue(session, tenant_id=ctx.tenant_id)
            return tuple((await session.execute(stmt)).scalars().all())

    async def _refuse_if_overdue(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        """Refuse to serve while this tenant's blocking propagation is past due.

        Raises the same `OverdueDerivativeRefusal` the arms raise rather than a
        type of its own. A caller that caught one refusal and not the other
        would be protected on whichever surface it happened to name, and two
        spellings of "this tenant's withdrawals are late" is the shape that let
        this gap go unnoticed in the first place.

        In the read's own session, so a check that passed on another connection
        cannot vouch for a state this read never sees.
        """
        overdue = await pending_overdue(session, now=self._clock.now(), blocking_only=True, tenant_id=tenant_id)
        if overdue:
            raise workspace_recall.OverdueDerivativeRefusal(
                f"{overdue} blocking derivative propagation item(s) are past due for this tenant; "
                "an exclusion's item_key is what that propagation withdraws, so it is not served until it lands"
            )

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
            await self._refuse_if_unservable(session, ctx, receipt_id)
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
