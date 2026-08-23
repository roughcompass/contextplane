"""Withhold the receipts that quoted a quarantined claim, and release them.

E4-T4. Satisfies `service.memory.quarantine.ReceiptWithholding`, and lives here
rather than there because `contextplane.service` may not import
`contextplane.context` -- and both the receipt tables and the `observed_claims`
block name are this layer's.

**A receipt cites a claim by carrying its id as an item key.** That is what
`observed_claims_arm` writes: `item_key=str(claim.claim_id)` on an item whose
block is `BLOCK_OBSERVED_CLAIMS`. So the join below is exact rather than a
heuristic over stored text, and it stays exact because it reads the same
constant the arm writes.

**Nothing here opens a transaction.** Both operations take the caller's session
so they land in the quarantine's own transaction: the claims and the receipts
that quoted them are withheld at one instant, and no reader observes one without
the other. That is stronger than the mark-first-reconcile-after ordering E4-T4
prescribed, which is a remedy for a row-at-a-time sweep this code does not do.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.context.schemas.envelope import BLOCK_OBSERVED_CLAIMS

_WITHHOLD = (
    "UPDATE context_receipts SET withheld_at = CAST(:now AS TIMESTAMPTZ), withheld_by = :qid "
    " WHERE tenant_id = :tid AND withheld_at IS NULL "
    "   AND receipt_id IN ("
    "         SELECT i.receipt_id FROM context_receipt_items i "
    "          WHERE i.block = :block "
    "            AND i.item_key = ANY(CAST(:keys AS TEXT[])))"
)

_RELEASE = "UPDATE context_receipts SET withheld_at = NULL, withheld_by = NULL WHERE withheld_by = :qid"


class ReceiptWithholder:
    """The `ReceiptWithholding` the quarantine service is handed."""

    async def withhold_for_claims(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        claims: tuple[uuid.UUID, ...],
        quarantine_id: uuid.UUID,
        now: datetime.datetime,
    ) -> None:
        """Withhold every servable receipt that served one of these claims.

        `withheld_at IS NULL` in the predicate, so an already-withheld receipt
        is left alone. Overwriting `withheld_by` would relabel it as this
        incident's, and the earlier incident's revert would then release a
        receipt this one still means to withhold -- the same reason
        `apply` refuses to overwrite an existing `quarantined_at`.

        Tenant-scoped on the receipt rather than trusted from the claim ids: an
        item key is a string, and a predicate that could reach another tenant's
        receipts by id collision is a cross-tenant write however unlikely the
        collision.
        """
        if not claims:
            return
        await session.execute(
            text(_WITHHOLD),
            {
                "block": BLOCK_OBSERVED_CLAIMS,
                "keys": [str(claim) for claim in claims],
                "now": now,
                "qid": quarantine_id,
                "tid": tenant_id,
            },
        )

    async def release(self, session: AsyncSession, *, quarantine_id: uuid.UUID) -> None:
        """Serve again every receipt this quarantine withheld, and only those.

        Keyed on `withheld_by`, not re-derived from the membership, for the
        reason revert restores claims from `claim_quarantine_members`: the graph
        moves, and a receipt written after the quarantine that happens to cite
        the same claim was never withheld by it. Releasing it would claim to
        restore something this quarantine never took away.

        A receipt reached by two open incidents stays withheld by the first,
        because `withhold_for_claims` never overwrites an existing `withheld_by`
        -- so the second incident's revert finds nothing of its own to release,
        which is the correct answer rather than a missed one.
        """
        await session.execute(text(_RELEASE), {"qid": quarantine_id})
