"""One queue for everything needing a person, and what kind of attention each needs.

Four things arrive here, and they are not interchangeable. An unlinked claim needs
somebody to say what it is about. A contested one needs somebody to decide which of two
assertions holds. A below-floor claim needs somebody to judge whether it is worth
keeping. A high-impact proposal needs its *owner* specifically, and nobody else will do.

Collapsing them into one undifferentiated list would be the easy implementation and the
wrong one: a curator picking up an item has to know what is being asked of them before
they can act, and "there are 40 things" is not a queue anybody works.

**What a machine extracted and what a person stands behind are distinguished on every
row.** That distinction is the audit the curator role exists to perform. A queue that
showed only the current value would make a machine's guess and a person's decision look
the same at a glance, which is the one confusion this surface must never create.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

REASON_UNLINKED: Final[str] = "unlinked"
REASON_CONTESTED: Final[str] = "contested"
REASON_BELOW_FLOOR: Final[str] = "below_floor"
REASON_AWAITING_OWNER: Final[str] = "awaiting_owner"

# A `containment_refused` reason existed here once, for a candidate a containment
# check turned away. Removed rather than given a SQL arm: nothing in this codebase
# writes a queryable record of that refusal -- the direct-assertion route refuses
# synchronously and never stages or queues the attempt, and extraction's own
# refusal path only counts and logs. A vocabulary entry with no row it could ever
# match is a dead branch, not a feature waiting on wiring.

# What a curator can do about each, so the surface can offer the right action rather
# than every action. An item whose only offered action is wrong for it is how a queue
# gets worked incorrectly rather than slowly.
ACTIONS_BY_REASON: Final[dict[str, tuple[str, ...]]] = {
    REASON_UNLINKED: ("link", "discard"),
    REASON_CONTESTED: ("confirm", "discard", "escalate"),
    REASON_BELOW_FLOOR: ("confirm", "discard"),
    # Deliberately not "accept" or "reject": those belong to the owner's review path,
    # which checks tenancy and role. Offering them here would put a second door on
    # a decision that has an owner.
    REASON_AWAITING_OWNER: ("escalate",),
}


@dataclasses.dataclass(frozen=True)
class QueueItem:
    claim_id: uuid.UUID
    reason: str
    subject_reference: str
    subject_entity_id: uuid.UUID | None
    predicate: str
    value: Any
    confidence: float | None
    created_at: datetime.datetime
    # Whether a person stands behind this row, as against a machine having extracted
    # or inferred it. Read from the claim's own authority tier rather than from
    # whether a confirmation points at it: a confirmation closes what it confirms, so
    # a "has been confirmed" flag would be false on every row that is still in the
    # queue -- a field that reads as informative and can never vary.
    human_backed: bool
    proposal_id: uuid.UUID | None = None

    @property
    def available_actions(self) -> tuple[str, ...]:
        return ACTIONS_BY_REASON.get(self.reason, ())


class CurationQueueService:
    """Reads only. Acting on an item goes through the service that owns that
    decision, so the queue cannot become a second write path into claims."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def items_for(
        self,
        tenant_id: uuid.UUID,
        *,
        cursor: tuple[datetime.datetime, uuid.UUID] | None = None,
        page_size: int = 100,
    ) -> tuple[QueueItem, ...]:
        """Everything needing attention in one tenant, oldest first.

        Oldest first because the alternative -- highest confidence, or largest blast
        radius -- means the tail is never reached, and an item nobody will ever see
        is one that should not have been queued.

        Keyset-paginated on `(created_at, claim_id)`. `cursor` is the already-decoded
        pair from the last row of the previous page; the caller (the REST route)
        owns encoding and decoding the opaque token, the same split
        `admin_audit.py`'s query route uses. Fetches `page_size + 1` rows so the
        caller can tell whether another page follows without a second query.
        """
        params: dict[str, Any] = {"tid": tenant_id, "limit": page_size + 1}
        sql = _QUEUE_BASE
        if cursor is not None:
            sql += " AND (c.created_at, c.claim_id) > (:cursor_created_at, :cursor_claim_id)"
            params["cursor_created_at"] = cursor[0]
            params["cursor_claim_id"] = cursor[1]
        sql += " ORDER BY c.created_at, c.claim_id LIMIT :limit"

        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(sql),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            QueueItem(
                claim_id=row["claim_id"],
                reason=row["reason"],
                subject_reference=row["subject_reference"],
                subject_entity_id=row["subject_entity_id"],
                predicate=row["predicate"],
                value=row["value"],
                confidence=float(row["confidence"]) if row["confidence"] is not None else None,
                created_at=row["created_at"],
                human_backed=bool(row["human_backed"]),
                proposal_id=row["proposal_id"],
            )
            for row in rows
        )

    async def counts_for(self, tenant_id: uuid.UUID) -> dict[str, int]:
        """How much of each kind is waiting.

        Separate from the item list because the number a curator needs to see before
        opening the queue is not the same as the page they open.
        """
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"SELECT reason, count(*) AS n FROM ({_QUEUE_BASE}) q GROUP BY reason"  # noqa: S608 - _QUEUE_BASE is a fixed module-level query constant, not caller input; :tid is the only actual value and is bound below
                        ),
                        {"tid": tenant_id},
                    )
                )
                .mappings()
                .all()
            )
        return {row["reason"]: int(row["n"]) for row in rows}


def backlog_predicate(*, tenant_filter: bool) -> str:
    """The FROM/JOIN/WHERE that decides whether a claim is in the curation backlog.

    A claim is backlogged for the first reason that applies -- unlinked, contested,
    waiting on a high-impact proposal's owner, or below the tenant's confidence
    floor -- which is also the CASE order `_QUEUE_BASE` below uses to label it.

    `operational_health.py`'s cluster-wide backlog gauge calls this the same way
    (`tenant_filter=False`) rather than keeping its own copy of the CASE/JOIN
    logic. One function both callers run through means a change to what counts
    as "backlog" cannot update one and silently leave the other disagreeing --
    the failure mode a hand-copied second query invites by construction.
    """
    tenant_clause = "COALESCE(c.owning_tenant_id, c.author_tenant_id) = :tid\n   AND " if tenant_filter else ""
    return f"""
  FROM memory_claims c
  LEFT JOIN memory_promotion_proposal p
         ON p.claim_id = c.claim_id AND p.state = 'open'
        AND p.high_impact_reasons <> '[]'::JSONB
  LEFT JOIN memory_promotion_policy pol
         ON pol.tenant_id = COALESCE(c.owning_tenant_id, c.author_tenant_id)
 WHERE {tenant_clause}c.t_invalidated_at IS NULL
   AND c.status <> 'superseded'
   AND (
       c.status = 'unlinked'
       OR c.is_contested
       OR p.proposal_id IS NOT NULL
       OR c.confidence < COALESCE(pol.confidence_floor, 0)
   )
"""


# A claim reaches the queue for the first reason that applies, so one claim never
# appears twice under different headings. The order runs from "we do not know what this
# is about" outward, because an unlinked claim cannot be usefully judged on any of the
# later grounds anyway.
_QUEUE_BASE = """
SELECT c.claim_id,
       CASE
           WHEN c.status = 'unlinked' THEN 'unlinked'
           WHEN c.is_contested THEN 'contested'
           WHEN p.proposal_id IS NOT NULL THEN 'awaiting_owner'
           WHEN c.confidence < COALESCE(pol.confidence_floor, 0) THEN 'below_floor'
       END AS reason,
       c.subject_reference,
       c.subject_entity_id,
       c.predicate,
       c.value_jsonb AS value,
       c.confidence,
       c.created_at,
       (c.source_authority IN ('owner_human', 'observer_human')
        OR c.confirms_claim_id IS NOT NULL) AS human_backed,
       p.proposal_id""" + backlog_predicate(tenant_filter=True)
