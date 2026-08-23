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

**A contradiction case is the fifth thing, and it is the only one this module writes.**
The claim-shaped items above are read-only projections; a case is a row of its own,
recording that a contradiction was routed to a named person and what they eventually
decided. Writing cases here does not make this a second write path into claims -- no
statement in this module touches `memory_claims`, and a disposition is a *proposal*,
never the write it proposes. The surfaces that perform canonical, runbook, and agent-
readiness writes each have their own approval contract, and a curator recording
`propose_canonical` has asked for a promotion rather than performed one. Collapsing
those two events would make "decided" and "written" the same moment, which is exactly
the confusion an accountable owner is there to prevent.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.memory.curation_ranking import (
    _RANK_COLUMNS,
    _RANK_JOIN,
    _RANK_ORDER,
    ESCALATION_AGE_DAYS,
    QueueCursor,
    SystemClock,
)
from contextplane.types import Clock

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
    """One claim awaiting curation, with reason and available actions."""

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

    #: Why this row sits where it does. The consequence preview belongs on the
    #: item rather than behind a second call: a rank a reviewer cannot
    #: interrogate is a rank they learn to ignore, and these three are exactly
    #: the ordering's inputs.
    dependant_count: int = 0
    sampling_priority: int = 0
    #: Past the governed escalation age, so ordered ahead of everything younger
    #: whatever its leverage. This is the flag that makes the queue's
    #: reachability promise visible to the person relying on it.
    escalated: bool = False

    @property
    def available_actions(self) -> tuple[str, ...]:
        """Actions the current reason permits; empty when no action applies."""
        return ACTIONS_BY_REASON.get(self.reason, ())

    def cursor(self) -> QueueCursor:
        """Where a next page should resume from, if this is the last row read."""
        return QueueCursor(
            escalation_rank=0 if self.escalated else 1,
            neg_dependants=-self.dependant_count,
            neg_sampling=-self.sampling_priority,
            created_at=self.created_at,
            claim_id=self.claim_id,
        )


class CurationQueueService:
    """Reads only. Acting on an item goes through the service that owns that
    decision, so the queue cannot become a second write path into claims."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, clock: Clock | None = None) -> None:
        self._factory = factory
        # Optional so the existing construction sites keep working: every method
        # but the ranked read is timeless, and `items_for` accepts an explicit
        # `now` for callers that hold one. A service that demanded a clock for
        # nine methods that never ask the time would be a worse contract than
        # this narrow default.
        self._clock = clock or SystemClock()

    async def items_for(
        self,
        tenant_id: uuid.UUID,
        *,
        cursor: QueueCursor | None = None,
        page_size: int = 100,
        now: datetime.datetime | None = None,
    ) -> tuple[QueueItem, ...]:
        """Everything needing attention in one tenant, most consequential first.

        This was arrival order, and the docstring here argued for it: ranking
        "means the tail is never reached". That was right about ranking and
        wrong only in assuming ranking has to starve. `curation_ranking` holds
        why it does not have to, what the ordering is, and why confidence is
        deliberately absent from it.

        Keyset-paginated on the whole sort tuple. `cursor` is the decoded
        `QueueCursor` from the last row of the previous page; the caller owns
        encoding it, the same split `admin_audit.py`'s query route uses. Fetches
        `page_size + 1` rows so the caller can tell whether another page follows
        without a second query.
        """
        moment = now if now is not None else self._clock.now()
        params: dict[str, Any] = {
            "escalation_cutoff": moment - datetime.timedelta(days=ESCALATION_AGE_DAYS),
            "limit": page_size + 1,
            "tid": tenant_id,
        }
        # A CTE, so the cursor can compare the *computed* sort columns rather
        # than recomputing a correlated subquery inside its own predicate.
        ranked = _QUEUE_SELECT + _RANK_COLUMNS + backlog_predicate(tenant_filter=True, extra_joins=_RANK_JOIN)
        sql = f"WITH ranked AS ({ranked}\n) SELECT * FROM ranked"  # noqa: S608 - fixed module constants; every value is bound
        if cursor is not None:
            sql += (
                " WHERE (escalation_rank, neg_dependants, neg_sampling, created_at, claim_id)"
                " > (:cursor_escalation, :cursor_dependants, :cursor_sampling,"
                "    :cursor_created_at, :cursor_claim_id)"
            )
            params["cursor_claim_id"] = cursor.claim_id
            params["cursor_created_at"] = cursor.created_at
            params["cursor_dependants"] = cursor.neg_dependants
            params["cursor_escalation"] = cursor.escalation_rank
            params["cursor_sampling"] = cursor.neg_sampling
        sql += _RANK_ORDER + " LIMIT :limit"

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
                # The consequence preview, from the same read rather than a
                # traversal per row: a rank a reviewer cannot interrogate is a
                # rank they learn to ignore.
                dependant_count=-int(row["neg_dependants"]),
                sampling_priority=-int(row["neg_sampling"]),
                escalated=int(row["escalation_rank"]) == 0,
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

    # -- contradiction cases -------------------------------------------------


def backlog_predicate(*, tenant_filter: bool, extra_joins: str = "") -> str:
    """The FROM/JOIN/WHERE that decides whether a claim is in the curation backlog.

    A claim is backlogged for the first reason that applies -- unlinked, contested,
    waiting on a high-impact proposal's owner, or below the tenant's confidence
    floor -- which is also the CASE order `_QUEUE_BASE` below uses to label it.

    `operational_health.py`'s cluster-wide backlog gauge calls this the same way
    (`tenant_filter=False`) rather than keeping its own copy of the CASE/JOIN
    logic. One function both callers run through means a change to what counts
    as "backlog" cannot update one and silently leave the other disagreeing --
    the failure mode a hand-copied second query invites by construction.

    `extra_joins` splices in after the standing joins and **before** `WHERE`,
    which is the only place another join can legally go. The ranked queue needs
    one, and appending it to the finished string put a `LEFT JOIN` after the
    predicate -- valid-looking text and a syntax error, which is why the join is
    a parameter here rather than concatenation at the call site.
    """
    tenant_clause = "COALESCE(c.owning_tenant_id, c.author_tenant_id) = :tid\n   AND " if tenant_filter else ""
    return f"""
  FROM memory_claims c
  LEFT JOIN memory_promotion_proposal p
         ON p.claim_id = c.claim_id AND p.state = 'open'
        AND p.high_impact_reasons <> '[]'::JSONB
  LEFT JOIN memory_promotion_policy pol
         ON pol.tenant_id = COALESCE(c.owning_tenant_id, c.author_tenant_id){extra_joins}
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
#: The select list alone. Separate from the predicate so the ranked query can
#: add columns to one and a join to the other -- both of which have exactly one
#: legal position, and neither of which is "the end of the finished string".
_QUEUE_SELECT = """
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
       p.proposal_id"""

_QUEUE_BASE = _QUEUE_SELECT + backlog_predicate(tenant_filter=True)
