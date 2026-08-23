"""What orders the curation queue, and what stops the order starving anything.

E5-T3. Extracted from `curation_queue.py` rather than left in it, because that
file crossed its 800-line ceiling the moment this arrived — and the gate's
instruction is to split along a real seam. This is one: the queue's *reading*
and the queue's *ordering* are separate concerns, and the ordering is the half
with a governed number and a reachability property to defend.

**The starvation this file exists to prevent is created by ranking, not
inherited.** FIFO cannot starve anything. A ranked queue can, and the obvious
ranking has a feedback loop in it: confidence decays with age, a decayed claim
ranks lower, a claim nobody reviews never has its confidence refreshed, so it
decays further and sinks because it sank.

`DECAY_FLOOR` does not fix that, and it is worth seeing why because it looks as
though it should. The floor bounds the decayed *value* — it asymptotes to 0.10
rather than to zero — so a claim never decays out of existence. But **rank is
relative**, and an item pinned at the floor sits below every item that has not
decayed. The value is bounded and the position is not.

So confidence is not in the ordering at all. That removes the loop rather than
damping it, and it buys a second property: every ranking input below is fixed
for the lifetime of a pagination pass, so a keyset cursor over them cannot have
rows reordered underneath it. A rank built on a decaying value would silently
skip rows mid-pagination, which for a review queue is the same defect as
starvation reached by a different route.

**The bound is a number, because "we also consider age" is not checkable.**
Past `ESCALATION_AGE_DAYS` an item is ordered ahead of everything younger than
it whatever its leverage, and within that group by arrival — so the oldest
backlogged claim reaches the head of the queue. The number is governed.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Final

from contextplane import ranking

#: Days a backlogged claim may wait before it outranks everything younger than
#: it. Read from the governed registry at import: the entry's own reason records
#: why this number exists, which is that "we also consider age" is not something
#: a test can assert.
ESCALATION_AGE_DAYS: Final[float] = ranking.threshold("review-queue-escalation-age-days@1")

#: The three sort columns, all ascending, and the reason they are all ascending.
#:
#: Postgres compares row constructors component-wise in one direction, so a
#: keyset cursor cannot express `escalated DESC, dependants DESC, created_at
#: ASC`. Negating the two descending components turns the whole ordering into a
#: single ascending tuple, which is what lets the cursor below be a plain
#: comparison rather than a chain of ORs that is wrong in one branch.
#:
#: **Confidence is deliberately absent, and that is the anti-feedback design.**
#: Confidence decays with age; a decayed claim would rank lower; a claim nobody
#: reviews never has its confidence refreshed, so it decays further and sinks
#: because it sank. `DECAY_FLOOR` does not fix that -- it bounds the decayed
#: *value*, and rank is *relative*, so an item pinned at the floor sits below
#: everything that has not decayed. Leaving it out removes the loop rather than
#: damping it.
#:
#: It also keeps the cursor honest: a dependant count and a policy's sample size
#: do not move during a pagination pass, and escalation is monotone in time, so
#: it can only carry an item *forward* past a cursor -- never backward, where a
#: reader would skip it.
_RANK_COLUMNS = """,
       CASE WHEN c.created_at <= :escalation_cutoff THEN 0 ELSE 1 END AS escalation_rank,
       -(SELECT count(DISTINCT e.src_entity_id)
           FROM edges e
          WHERE e.dst_entity_id = c.subject_entity_id
            AND e.rel IN ('depends_on', 'composes', 'provides_to')
            AND e.t_invalidated_at IS NULL
            AND (e.t_valid_to IS NULL OR e.t_valid_to > now())) AS neg_dependants,
       -COALESCE(sp.min_sample, 0) AS neg_sampling"""

#: E5-T2's budget, joined rather than read per row. A category whose policy
#: demands a heavier sample surfaces ahead of one that does not, which is what
#: makes the sampling policy affect what a reviewer actually sees.
_RANK_JOIN = """
  LEFT JOIN claim_sampling_policies sp
         ON sp.tenant_id = COALESCE(c.owning_tenant_id, c.author_tenant_id)
        AND sp.claim_category = c.claim_category"""

_RANK_ORDER = " ORDER BY escalation_rank, neg_dependants, neg_sampling, created_at, claim_id"


@dataclasses.dataclass(frozen=True)
class QueueCursor:
    """A position in the ranked queue: the whole sort tuple, in sort order.

    Every component either cannot change (`created_at`, `claim_id`), or changes
    only in the direction that carries a row *toward* the front
    (`escalation_rank`, once). So resuming from one cannot silently skip a row —
    which for a review queue is the same defect as starvation, reached by a
    different route.
    """

    escalation_rank: int
    neg_dependants: int
    neg_sampling: int
    created_at: datetime.datetime
    claim_id: uuid.UUID


class SystemClock:
    """The default when no clock is injected. Named rather than a lambda so the
    one place this service reads wall time is greppable."""

    @staticmethod
    def now() -> datetime.datetime:
        """The current instant, for the escalation cutoff."""
        return datetime.datetime.now(tz=datetime.UTC)
