"""How accurate one author's claims turned out to be, over a window.

E20-T4. The question is operational: a tenant runs an agent, the agent writes
claims, reviewers adjudicate some of them, and the tenant wants to know how
often that agent is right -- overall, and broken down finely enough to act on.

**Shaped after `calibration.py`, not after `learning_reads.py`.** Frozen
dataclasses, a pure aggregation function, and a thin service over a session
factory. The learning-read module is the wrong model here even though it is the
closer neighbour by subject: it existed to enforce floors, and there are none
left to enforce -- the per-actor floor was removed uniformly, and this read is
the surface that removal was for.

**`author_tenant_id`, not `owning_tenant_id`.** The two differ and the
difference is the whole point of this read. `owning_tenant_id` is the tenant
that owns the claim's *subject*; `author_tenant_id` is the tenant that ran the
agent. Scoping by the former would answer "how accurate were claims about our
capabilities, whoever wrote them" -- a different and much less useful question,
and one that would leak another tenant's agent's error rate into this tenant's
figures the moment two tenants write about the same subject.

**`undecidable` counts in the header and not in the rate.** The same split
`calibration.py` already makes, for the same stated reason: an undecidable
verdict is information about the reviewer's certainty, not about the claim, and
folding it into either side of the ratio would bias the result. But dropping it
entirely would hide review effort that happened, so it is reported beside the
rate rather than behind it.

**No confidence interval, deliberately.** Four correct out of five is 80% and so
is eight hundred out of a thousand, and a reader who acts on the first as if it
were the second has been misled by this module. `n_decided` is served with every
rate so the difference is visible -- which is weaker than an interval and is
what can be justified without deciding what a "reliable" rate means, a question
this module has no basis to answer and E20's later tasks may.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ValidationError
from contextplane.types import TenantContext

#: The verdicts that decide something. `undecidable` is the third value the
#: adjudication CHECK allows, and it is deliberately absent here.
DECIDING_VERDICTS: Final[tuple[str, ...]] = ("correct", "incorrect")

BREAKDOWN_OVERALL: Final = "overall"
BREAKDOWN_CATEGORY: Final = "claim_category"
BREAKDOWN_PREDICATE: Final = "predicate"

#: How an accuracy read may be grouped. Closed, because each value is a column
#: this module names in SQL -- an open breakdown would be a group-by over
#: caller-supplied text.
BREAKDOWNS: Final[tuple[str, ...]] = (BREAKDOWN_OVERALL, BREAKDOWN_CATEGORY, BREAKDOWN_PREDICATE)

_GROUP_COLUMN: Final[dict[str, str]] = {
    BREAKDOWN_CATEGORY: "c.claim_category",
    BREAKDOWN_PREDICATE: "c.predicate",
}


@dataclasses.dataclass(frozen=True)
class AccuracyGroup:
    """One row of an accuracy breakdown."""

    label: str
    n_correct: int
    n_incorrect: int
    n_undecidable: int

    @property
    def n_decided(self) -> int:
        """The denominator the rate is actually over."""
        return self.n_correct + self.n_incorrect

    @property
    def n_adjudicated(self) -> int:
        """Every verdict recorded, including the ones that decided nothing."""
        return self.n_decided + self.n_undecidable

    @property
    def rate(self) -> float | None:
        """Correct as a fraction of decided, or None when nothing was decided.

        `None` rather than 0.0. A window in which every verdict was undecidable
        has an unknown accuracy, and zero is a specific and wrong claim about it
        -- the same distinction `Cell.value` used to draw between a withheld
        figure and an empty one.
        """
        return None if self.n_decided == 0 else self.n_correct / self.n_decided


@dataclasses.dataclass(frozen=True)
class Accuracy:
    """One author's accuracy over one window, grouped or not."""

    author_actor_id: uuid.UUID
    window_start: datetime.datetime
    window_end: datetime.datetime
    breakdown: str
    groups: tuple[AccuracyGroup, ...]

    @property
    def overall(self) -> AccuracyGroup:
        """The whole window as one group, summed from the parts.

        Summed rather than queried separately, so a breakdown and its header can
        never disagree -- two statements over the same window would eventually
        differ by a filter somebody added to one of them.
        """
        return AccuracyGroup(
            label=BREAKDOWN_OVERALL,
            n_correct=sum(group.n_correct for group in self.groups),
            n_incorrect=sum(group.n_incorrect for group in self.groups),
            n_undecidable=sum(group.n_undecidable for group in self.groups),
        )


def _sql(breakdown: str) -> str:
    """The one statement, grouped or not.

    **Scoped by `c.author_tenant_id`, which is the tenant that ran the agent --
    not `c.owning_tenant_id`, the tenant that owns the claim's subject.** The two
    differ, and swapping them silently answers a different question: accuracy of
    claims *about us* rather than accuracy of *our agent*. `learning_reads.py`
    makes the opposite choice for its own query and says why there; this comment
    exists so neither is copied into the other by resemblance.

    Every verdict in the window is counted, including `undecidable`, because the
    header reports review effort. The rate's denominator excludes it, and that
    happens in `AccuracyGroup` rather than here -- one statement, one pass, and
    the arithmetic where a reader can see it.
    """
    group = _GROUP_COLUMN.get(breakdown)
    label = group if group is not None else "'overall'"
    group_by = f"GROUP BY {group}" if group is not None else ""
    return f"""
SELECT {label} AS label,
       count(*) FILTER (WHERE a.verdict = 'correct')     AS n_correct,
       count(*) FILTER (WHERE a.verdict = 'incorrect')   AS n_incorrect,
       count(*) FILTER (WHERE a.verdict = 'undecidable') AS n_undecidable
  FROM memory_claim_adjudication a
  JOIN memory_claims c ON c.claim_id = a.claim_id
 WHERE c.author_actor_id = :actor
   AND c.author_tenant_id = :tenant
   AND a.adjudicated_at >= :window_start
   AND a.adjudicated_at < :window_end
 {group_by}
 ORDER BY label
"""


class AgentAccuracyService:
    """Per-author accuracy reads for one tenant."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def accuracy_for(
        self,
        ctx: TenantContext,
        *,
        author_actor_id: uuid.UUID,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
        breakdown: str = BREAKDOWN_OVERALL,
    ) -> Accuracy:
        """How often this author was judged right, over this window.

        A window with no adjudications returns an `Accuracy` with no groups
        rather than raising: "nobody reviewed this agent's work" is an answer,
        and it is the answer a caller most needs to distinguish from "reviewed
        and wrong".
        """
        if breakdown not in BREAKDOWNS:
            raise ValidationError(f"unknown breakdown {breakdown!r}; legal values are {list(BREAKDOWNS)}")
        if window_end <= window_start:
            raise ValidationError("window_end must be after window_start")

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(_sql(breakdown)),
                    {
                        "actor": author_actor_id,
                        "tenant": ctx.tenant_id,
                        "window_start": window_start,
                        "window_end": window_end,
                    },
                )
            ).all()

        groups = tuple(
            AccuracyGroup(
                label=str(row.label),
                n_correct=int(row.n_correct),
                n_incorrect=int(row.n_incorrect),
                n_undecidable=int(row.n_undecidable),
            )
            for row in rows
            # An ungrouped query returns one row even when nothing matched, with
            # every count at zero. That is a row about no adjudications, and
            # serving it as a group would make "never reviewed" look like a
            # measured result.
            if int(row.n_correct) or int(row.n_incorrect) or int(row.n_undecidable)
        )
        return Accuracy(
            author_actor_id=author_actor_id,
            window_start=window_start,
            window_end=window_end,
            breakdown=breakdown,
            groups=groups,
        )


__all__ = [
    "BREAKDOWNS",
    "BREAKDOWN_CATEGORY",
    "BREAKDOWN_OVERALL",
    "BREAKDOWN_PREDICATE",
    "DECIDING_VERDICTS",
    "Accuracy",
    "AccuracyGroup",
    "AgentAccuracyService",
]
