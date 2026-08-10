"""The claims-table half of an actor erasure: scrub, repair, delete.

Not a `ClaimService` method, and not composed into it -- this function has no
dependency on `claim_writer.py`, `claim_curator_actions.py`, or
`claim_authority.py`, and none of them depend on it. It landed in the same
file as `ClaimService` for years only because of the privileged-writes rule
below, and the split that moved `ClaimService` out to `claim_writer.py` is
what makes that coincidence visible enough to give this its own module.

Lives in this package because this package is the single writer for
`memory_claims` and `memory_claim_provenance` -- an erasure that wrote them
from elsewhere would be a second vocabulary for the same rows. The
*selection* of what to erase belongs to the erasure participant
(`claim_erasure.py`); every write it implies lands here, in the caller's
transaction.

Three write families, ordered so no delete can trip a constraint:

1. *Excerpt scrub.* The target's session-event provenance rows are removed
   from claims that survive on independent evidence -- the excerpt column
   carries the person's verbatim sentences. Survivors keep at least one
   row by construction: independent evidence is what made them survivors.
2. *Chain repair.* Confirmation triples pointing at selected claims are
   nulled together (their CHECK requires all-or-none). Losers superseded
   by a selected claim are re-pointed at the erased chain's first
   unselected successor when one exists, and otherwise reopened -- status
   back to `staged` with supersession *and* consolidation markers cleared,
   so the next sweep re-decides them instead of skipping them as settled.
3. *Deletion.* Provenance first (belt to the FK cascade's braces), then
   the claims.

A second erasure shape shares this module for the same reason, and only that
reason: when a claim's *source* is withdrawn rather than its author erased,
the claim is minimized instead of deleted. Its two writes live here because
the rule above is about the tables, not about the caller -- the decision of
which claim and why belongs to the propagation handler
(`derivative_handlers.py`), and the writes it implies land here, in the
caller's transaction, exactly as the participant's do.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

#: The status a minimized claim settles into: closed, retained, and served
#: nowhere. The read path filters on `status IN ('staged', 'superseded')`, so
#: this is what takes a claim out of every serving surface while leaving the
#: row an auditor needs. The claim vocabulary has no `invalidated` member and
#: adding one would mean a fourth meaning for a column three paths already read.
CLAIM_STATUS_CLOSED = "rejected"


def _rows_affected(result: object) -> int:
    """How many rows a DML statement touched, as an int rather than an Optional."""
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def erase_claims_for_actor(
    session: AsyncSession,
    *,
    selected: list[uuid.UUID],
    target_actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict[str, int]:
    """The claims-table half of an actor erasure: scrub, repair, delete.

    See the module docstring for the write families and their ordering.
    """
    counts = {
        "claims": 0,
        "provenance_rows": 0,
        "provenance_rows_scrubbed": 0,
        "confirmation_refs_cleared": 0,
        "chains_spliced": 0,
        "losers_reopened": 0,
    }

    scrubbed = await session.execute(
        text(
            "DELETE FROM memory_claim_provenance p "
            " USING memory_session_events e "
            " WHERE p.evidence_kind = 'session_event' "
            "   AND e.event_id::text = p.evidence_ref "
            "   AND e.actor_id = :actor AND e.tenant_id = :tid "
            "   AND p.claim_id <> ALL(:selected)"
        ),
        {
            "actor": target_actor_id,
            "tid": tenant_id,
            # A harmless never-matching id keeps the exclusion well-formed
            # when nothing was selected but excerpts still need scrubbing.
            "selected": selected or [uuid.UUID(int=0)],
        },
    )
    counts["provenance_rows_scrubbed"] = scrubbed.rowcount or 0  # type: ignore[attr-defined]

    if not selected:
        return counts

    cleared = await session.execute(
        text(
            "UPDATE memory_claims "
            "   SET confirms_claim_id = NULL, confirmed_by = NULL, confirmed_at = NULL "
            " WHERE confirms_claim_id = ANY(:selected) "
            "   AND claim_id <> ALL(:selected)"
        ),
        {"selected": selected},
    )
    counts["confirmation_refs_cleared"] = cleared.rowcount or 0  # type: ignore[attr-defined]

    # For every selected claim, its first unselected successor -- the splice
    # target. A chain that never leaves the selected set yields no row, and
    # its losers are reopened instead.
    splice_targets = {
        row.selected_id: row.splice_to
        for row in await session.execute(
            text(
                "WITH RECURSIVE chain AS ( "
                "  SELECT c.claim_id AS selected_id, c.superseded_by AS cursor_id "
                "    FROM memory_claims c WHERE c.claim_id = ANY(:selected) "
                "  UNION ALL "
                "  SELECT chain.selected_id, n.superseded_by "
                "    FROM chain JOIN memory_claims n ON n.claim_id = chain.cursor_id "
                "   WHERE chain.cursor_id = ANY(:selected) "
                ") "
                "SELECT selected_id, cursor_id AS splice_to FROM chain "
                " WHERE cursor_id IS NOT NULL AND cursor_id <> ALL(:selected)"
            ),
            {"selected": selected},
        )
    }

    losers = (
        await session.execute(
            text(
                "SELECT claim_id, superseded_by FROM memory_claims "
                " WHERE superseded_by = ANY(:selected) "
                "   AND claim_id <> ALL(:selected) "
                "   FOR UPDATE"
            ),
            {"selected": selected},
        )
    ).all()
    for loser in losers:
        target = splice_targets.get(loser.superseded_by)
        if target is not None:
            await session.execute(
                text("UPDATE memory_claims SET superseded_by = :to WHERE claim_id = :cid"),
                {"to": target, "cid": loser.claim_id},
            )
            counts["chains_spliced"] += 1
        else:
            # The whole chain is being erased: the belief this loser was
            # displaced by no longer exists, so it is the best remaining
            # assertion. Clearing consolidated_at is what lets the next sweep
            # re-decide it instead of skipping it as already settled.
            await session.execute(
                text(
                    "UPDATE memory_claims "
                    "   SET status = 'staged', superseded_by = NULL, "
                    "       superseded_reason = NULL, t_invalidated_at = NULL, "
                    "       consolidated_at = NULL "
                    " WHERE claim_id = :cid"
                ),
                {"cid": loser.claim_id},
            )
            counts["losers_reopened"] += 1

    provenance = await session.execute(
        text("DELETE FROM memory_claim_provenance WHERE claim_id = ANY(:selected)"),
        {"selected": selected},
    )
    counts["provenance_rows"] = provenance.rowcount or 0  # type: ignore[attr-defined]

    claims = await session.execute(
        text("DELETE FROM memory_claims WHERE claim_id = ANY(:selected)"),
        {"selected": selected},
    )
    counts["claims"] = claims.rowcount or 0  # type: ignore[attr-defined]
    return counts


async def minimize_claim_evidence(session: AsyncSession, *, claim_id: uuid.UUID) -> int:
    """Clear the quotations from one claim's citations, keeping the citations.

    The excerpt is the part that holds somebody's verbatim sentence; the kind and
    the ref are what make the claim's evidence answerable afterwards. Dropping the
    rows instead would leave a claim that cites nothing, which is a claim nobody
    can audit rather than one whose sources were withdrawn.

    Only the rows that still quote something, so a second pass touches none and
    reports zero -- which is what makes a retried propagation free rather than a
    second reduction.
    """
    return _rows_affected(
        await session.execute(
            text(
                "UPDATE memory_claim_provenance SET evidence_excerpt = NULL "
                " WHERE claim_id = :claim AND evidence_excerpt IS NOT NULL"
            ),
            {"claim": claim_id},
        )
    )


async def close_claim_for_erasure(session: AsyncSession, *, claim_id: uuid.UUID) -> int:
    """Take one claim out of every serving path, leaving the row for audit.

    `t_invalidated_at` is cleared because the schema ties it to the `superseded`
    status as a biconditional, so a claim that was already superseded cannot carry
    it into this status. What that costs is the instant it was overtaken; what
    survives is *that* it was, in `superseded_by` and `superseded_reason`, both
    deliberately untouched so the chain stays walkable. When it was erased is on
    the tombstone and the work item that ordered the reduction.

    Nothing here has to be repaired, and that is the difference from an actor
    erasure: no row is removed, so no confirmation triple and no supersession link
    is left pointing at something that stopped existing.
    """
    return _rows_affected(
        await session.execute(
            text(
                "UPDATE memory_claims SET status = :closed, t_invalidated_at = NULL "
                " WHERE claim_id = :claim AND status <> :closed"
            ),
            {"claim": claim_id, "closed": CLAIM_STATUS_CLOSED},
        )
    )


__all__ = [
    "CLAIM_STATUS_CLOSED",
    "close_claim_for_erasure",
    "erase_claims_for_actor",
    "minimize_claim_evidence",
]
