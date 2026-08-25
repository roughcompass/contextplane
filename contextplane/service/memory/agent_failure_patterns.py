"""What one agent keeps getting wrong, grouped so an instruction change can act on it.

E20-T6. Accuracy says an agent is 72% right. That is not something anybody can
fix. This groups its failures by `(claim_category, predicate)` and carries
examples, which is the grain at which somebody can write a better instruction.

**Two counts per group, and the second is the one that is usually missing.**
`incorrect_count` says how often this group appears among the failures.
`total_count` says how often the group was judged at all. Reporting only the
first conflates a predicate the agent uses constantly and mostly gets right --
which will dominate any failure list by volume alone -- with one it touches
rarely and always gets wrong. The second is the one worth an instruction change,
and it is invisible without the denominator.

**Examples are claim values and the adjudicator's own note.** Following
`calibration.py`'s standard that a fitted number should come with something a
person can check: a group that says "owned_by_team, 9 of 11 wrong" is a lead,
and the eleven values plus what the reviewer wrote about them is the evidence.

**It writes, despite the name.** `build_report` persists to
`agent_failure_pattern_report` and returns the id. That is not incidental
bookkeeping: `agent_instruction`'s CHECK refuses to activate a version that does
not cite a stored `report_id`, so a report nobody stored is a report no
instruction can be justified by.

**The window comes from `AgentAccuracyService`, not from a second definition.**
Both reads take the same parameters and this module passes them straight
through. Two modules that each decided what "the last 30 days" meant would
eventually disagree, and the disagreement would show up as a report whose
headline accuracy did not match its own groups.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ValidationError
from contextplane.service.memory.agent_accuracy import AgentAccuracyService
from contextplane.service.memory.agent_autonomy import AgentAutonomyService
from contextplane.types import Clock, TenantContext

#: How many examples a group carries. Enough to see a pattern, few enough that a
#: report stays readable and the stored JSONB stays small.
DEFAULT_EXAMPLES_PER_GROUP = 5

#: Groups by `(claim_category, predicate)` with both counts, and attaches
#: examples through a lateral join so the limit applies per group rather than to
#: the whole result.
#:
#: **A quarantined claim still counts and still appears; only its value is
#: withheld.** Dropping it from the counts would let an operator improve an
#: agent's measured accuracy by quarantining its worst claims, which is a worse
#: failure than the disclosure: a quarantine is about the claim's provenance
#: turning out to be wrong, not about whether the agent's judgement was.
#:
#: `total_count` counts every decided verdict in the group, not every claim: an
#: unreviewed claim says nothing about whether the agent got it right, and
#: including it would make a well-reviewed predicate look worse than an ignored
#: one. `undecidable` is excluded from both counts here for the same reason the
#: accuracy read excludes it from its denominator.
_PATTERN_SQL = """
WITH judged AS (
    SELECT c.claim_id,
           c.claim_category,
           c.predicate,
           c.value_jsonb,
           c.quarantined_at,
           a.verdict,
           a.note,
           a.adjudicated_at
      FROM memory_claim_adjudication a
      JOIN memory_claims c ON c.claim_id = a.claim_id
     WHERE c.author_actor_id = :actor
       AND c.author_tenant_id = :tenant
       AND a.verdict IN ('correct', 'incorrect')
       AND a.adjudicated_at >= :window_start
       AND a.adjudicated_at < :window_end
),
grouped AS (
    SELECT claim_category,
           predicate,
           count(*) FILTER (WHERE verdict = 'incorrect') AS incorrect_count,
           count(*)                                      AS total_count
      FROM judged
     GROUP BY claim_category, predicate
    HAVING count(*) FILTER (WHERE verdict = 'incorrect') > 0
)
SELECT g.claim_category,
       g.predicate,
       g.incorrect_count,
       g.total_count,
       e.examples
  FROM grouped g
  CROSS JOIN LATERAL (
      SELECT coalesce(json_agg(x ORDER BY x.adjudicated_at DESC), '[]'::json) AS examples
        FROM (
            SELECT j.claim_id,
                   -- Withheld content stays withheld here too. A quarantine says
                   -- the claim's provenance turned out to be wrong, and this
                   -- surface hands the value to the authoring agent itself via
                   -- `get_my_failure_patterns` -- so serving it would disclose
                   -- through a performance read what the serving path refuses.
                   -- The id and the reviewer's note still come back, because
                   -- "one of yours was wrong and has since been withheld" is
                   -- exactly what this report is for.
                   CASE WHEN j.quarantined_at IS NULL THEN j.value_jsonb END AS value_jsonb,
                   j.note,
                   j.adjudicated_at
              FROM judged j
             WHERE j.claim_category = g.claim_category
               AND j.predicate = g.predicate
               AND j.verdict = 'incorrect'
             ORDER BY j.adjudicated_at DESC
             LIMIT :examples
        ) x
  ) e
 ORDER BY g.incorrect_count DESC, g.claim_category, g.predicate
"""

_INSERT_SQL = """
INSERT INTO agent_failure_pattern_report (
    tenant_id, author_actor_id, window_start, window_end,
    n_adjudicated, n_incorrect, n_intervention_sessions, n_sessions,
    groups, generated_at, generated_by
) VALUES (
    :tenant, :actor, :window_start, :window_end,
    :n_adjudicated, :n_incorrect, :n_intervention_sessions, :n_sessions,
    CAST(:groups AS JSONB), :now, :generated_by
)
RETURNING report_id
"""


@dataclasses.dataclass(frozen=True)
class FailureExample:
    """One wrong claim, with what the reviewer said about it."""

    claim_id: uuid.UUID
    #: `None` when the claim has since been quarantined. The example is still
    #: listed -- the agent should know one of its claims was judged wrong -- but
    #: withheld content is not disclosed through a performance read.
    value: object
    note: str | None


@dataclasses.dataclass(frozen=True)
class FailureGroup:
    """One `(category, predicate)` the agent got wrong, and how often."""

    claim_category: str
    predicate: str
    incorrect_count: int
    total_count: int
    examples: tuple[FailureExample, ...]

    @property
    def rate(self) -> float:
        """How often this group fails when it is judged at all.

        No `None` case: `_PATTERN_SQL` only emits a group with at least one
        incorrect verdict, so `total_count` is never zero here. That is a
        property of the query rather than of this class, which is why it is
        stated rather than defended with a guard that could never fire.
        """
        return self.incorrect_count / self.total_count


@dataclasses.dataclass(frozen=True)
class FailurePatternReport:
    """What one agent got wrong over a window, and how much steering it needed."""

    report_id: uuid.UUID
    author_actor_id: uuid.UUID
    window_start: datetime.datetime
    window_end: datetime.datetime
    n_adjudicated: int
    n_incorrect: int
    n_sessions: int
    n_intervention_sessions: int
    groups: tuple[FailureGroup, ...]


def _serialize(groups: tuple[FailureGroup, ...]) -> str:
    """The stored snapshot.

    A blob rather than a child table, following `memory_calibration_mapping.bins`:
    a report is a fitted aggregate over a window, read whole and never joined
    into or updated in place.
    """
    return json.dumps(
        [
            {
                "claim_category": group.claim_category,
                "predicate": group.predicate,
                "incorrect_count": group.incorrect_count,
                "total_count": group.total_count,
                "rate": group.rate,
                "example_claim_ids": [str(example.claim_id) for example in group.examples],
            }
            for group in groups
        ],
        sort_keys=True,
    )


class AgentFailurePatternService:
    """Builds and stores failure-pattern reports for one tenant's agent."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._accuracy = AgentAccuracyService(session_factory)
        self._autonomy = AgentAutonomyService(session_factory)

    async def build_report(
        self,
        ctx: TenantContext,
        *,
        author_actor_id: uuid.UUID,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
        examples_per_group: int = DEFAULT_EXAMPLES_PER_GROUP,
    ) -> FailurePatternReport:
        """Group this agent's failures, attach evidence, and store the result.

        Stores unconditionally, including when there are no failure groups. A
        report saying "nothing went wrong in this window" is evidence an
        instruction did not need changing, and it is the baseline a later report
        is compared against -- E20's whole premise is measuring whether accuracy
        moved after a change, which needs a before.
        """
        if examples_per_group < 1:
            raise ValidationError("examples_per_group must be at least 1; a group with no evidence is a bare number")

        # Both reads take the same window, passed through rather than
        # re-derived. They validate it themselves, so a bad window is refused
        # once, by them, in their own words.
        accuracy = await self._accuracy.accuracy_for(
            ctx, author_actor_id=author_actor_id, window_start=window_start, window_end=window_end
        )
        autonomy = await self._autonomy.autonomy_for(
            ctx, author_actor_id=author_actor_id, window_start=window_start, window_end=window_end
        )
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            rows = (
                await session.execute(
                    text(_PATTERN_SQL),
                    {
                        "actor": author_actor_id,
                        "tenant": ctx.tenant_id,
                        "window_start": window_start,
                        "window_end": window_end,
                        "examples": examples_per_group,
                    },
                )
            ).all()

            groups = tuple(
                FailureGroup(
                    claim_category=str(row.claim_category),
                    predicate=str(row.predicate),
                    incorrect_count=int(row.incorrect_count),
                    total_count=int(row.total_count),
                    examples=tuple(
                        FailureExample(
                            claim_id=uuid.UUID(str(example["claim_id"])),
                            value=example["value_jsonb"],
                            note=example["note"],
                        )
                        for example in row.examples
                    ),
                )
                for row in rows
            )

            report_id = (
                await session.execute(
                    text(_INSERT_SQL),
                    {
                        "tenant": ctx.tenant_id,
                        "actor": author_actor_id,
                        "window_start": window_start,
                        "window_end": window_end,
                        "n_adjudicated": accuracy.overall.n_adjudicated,
                        "n_incorrect": accuracy.overall.n_incorrect,
                        "n_intervention_sessions": autonomy.n_intervened,
                        "n_sessions": autonomy.n_sessions,
                        "groups": _serialize(groups),
                        "now": now,
                        "generated_by": ctx.actor_id,
                    },
                )
            ).scalar_one()

        return FailurePatternReport(
            report_id=report_id,
            author_actor_id=author_actor_id,
            window_start=window_start,
            window_end=window_end,
            n_adjudicated=accuracy.overall.n_adjudicated,
            n_incorrect=accuracy.overall.n_incorrect,
            n_sessions=autonomy.n_sessions,
            n_intervention_sessions=autonomy.n_intervened,
            groups=groups,
        )


__all__ = [
    "DEFAULT_EXAMPLES_PER_GROUP",
    "AgentFailurePatternService",
    "FailureExample",
    "FailureGroup",
    "FailurePatternReport",
]
