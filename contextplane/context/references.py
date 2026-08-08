"""Find receipts by the work they describe.

A receipt is only evidence if somebody can reach it from the thing they are
asking about. Nobody starts from a receipt id: they start from a pull request,
a commit, a build, a deployment -- and ask what context an agent had when it
touched that. This module is the path from one to the other.

**The tenant predicate is in the SELECT, always.** Every read here joins through
`context_external_references`, which carries the tenant. Filtering afterwards
would mean loading rows the caller may not see, and a count taken before the
filter is itself a disclosure: "there are four receipts you cannot read" tells
somebody something they were not granted.

**A reference is matched on its normalized identity, not its URI.** The same
commit reachable through two hostnames is one piece of work, and matching on
whatever string a caller happened to hold would split its history in two.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from contextplane.context.models import ContextExternalReference, ContextReferenceBinding
from contextplane.context.models_receipt import ContextReceipt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import TenantContext

#: The binding subject type receipts are recorded under. Receipts and
#: checkpoints both bind to references through the same junction, so the type is
#: what keeps one query from returning the other's rows.
SUBJECT_RECEIPT = "context_item"


class ReceiptReferenceIndex:
    """Receipts, looked up by the external work they cite."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def receipts_for_reference(
        self,
        ctx: TenantContext,
        *,
        source_system: str,
        source_namespace: str,
        kind: str,
        external_id: str,
        limit: int = 50,
    ) -> tuple[ContextReceipt, ...]:
        """Every receipt citing one piece of external work, newest first.

        Matched on the normalized identity tuple rather than on a URI: the same
        commit reachable through two hostnames is one piece of work, and
        matching the string a caller happened to hold would split its history.

        Authorization runs in the SELECT. The join to
        `context_external_references` carries the tenant, so a reference
        belonging to another tenant contributes no rows rather than being
        filtered out afterwards -- and the count the caller sees is therefore
        already the authorized count.
        """
        stmt = (
            select(ContextReceipt)
            .join(
                ContextReferenceBinding,
                ContextReferenceBinding.subject_id == ContextReceipt.receipt_id,
            )
            .join(
                ContextExternalReference,
                ContextExternalReference.reference_id == ContextReferenceBinding.reference_id,
            )
            .where(
                ContextReceipt.tenant_id == ctx.tenant_id,
                ContextExternalReference.tenant_id == ctx.tenant_id,
                ContextReferenceBinding.tenant_id == ctx.tenant_id,
                ContextReferenceBinding.subject_type == SUBJECT_RECEIPT,
                ContextExternalReference.source_system == source_system,
                ContextExternalReference.source_namespace == source_namespace,
                ContextExternalReference.kind == kind,
                ContextExternalReference.external_id == external_id,
            )
            .order_by(ContextReceipt.resolved_at.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            return tuple((await session.execute(stmt)).scalars().unique().all())

    async def references_for_receipt(
        self,
        ctx: TenantContext,
        *,
        receipt_id: uuid.UUID,
    ) -> tuple[ContextExternalReference, ...]:
        """The external work one receipt cites.

        The reverse direction, and the one an auditor reads: given an answer,
        what did it claim to be about.
        """
        stmt = (
            select(ContextExternalReference)
            .join(
                ContextReferenceBinding,
                ContextReferenceBinding.reference_id == ContextExternalReference.reference_id,
            )
            .join(ContextReceipt, ContextReceipt.receipt_id == ContextReferenceBinding.subject_id)
            .where(
                ContextReceipt.receipt_id == receipt_id,
                ContextReceipt.tenant_id == ctx.tenant_id,
                ContextExternalReference.tenant_id == ctx.tenant_id,
                ContextReferenceBinding.subject_type == SUBJECT_RECEIPT,
            )
            .order_by(
                ContextExternalReference.source_system,
                ContextExternalReference.kind,
                ContextExternalReference.external_id,
            )
        )
        async with self._session_factory() as session:
            return tuple((await session.execute(stmt)).scalars().unique().all())

    async def bind(
        self,
        ctx: TenantContext,
        *,
        receipt_id: uuid.UUID,
        reference_ids: Sequence[uuid.UUID],
        bound_at: object,
    ) -> int:
        """Record that one receipt cites these references. Returns how many were added.

        Idempotent per pair: a reference already bound to this receipt is left
        alone rather than duplicated, because re-recording a resolution must not
        make its citation list grow.
        """
        added = 0
        async with self._session_factory() as session, session.begin():
            existing = set(
                (
                    await session.execute(
                        select(ContextReferenceBinding.reference_id).where(
                            ContextReferenceBinding.tenant_id == ctx.tenant_id,
                            ContextReferenceBinding.subject_type == SUBJECT_RECEIPT,
                            ContextReferenceBinding.subject_id == receipt_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for reference_id in reference_ids:
                if reference_id in existing:
                    continue
                session.add(
                    ContextReferenceBinding(
                        binding_id=uuid.uuid4(),
                        tenant_id=ctx.tenant_id,
                        reference_id=reference_id,
                        subject_type=SUBJECT_RECEIPT,
                        subject_id=receipt_id,
                        bound_at=bound_at,
                    )
                )
                existing.add(reference_id)
                added += 1
        return added


__all__ = ["SUBJECT_RECEIPT", "ReceiptReferenceIndex"]
