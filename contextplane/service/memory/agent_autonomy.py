"""How often an agent finished a session without being steered.

E20-T5. The second dimension, independent of correctness: an agent can be
accurate and need constant hand-holding, or fast and wrong, and those are
different problems needing different instruction changes. Accuracy alone cannot
tell them apart.

**The signal is already in the data and nothing computed it.**
`memory_session_events.kind` distinguishes `user_message` from `agent_action`,
ordered by `seq` within a session. A session that opens with a human's
`user_message` and then runs on `agent_action` rows completed autonomously. A
session where a `user_message` appears *after* the agent has started is one
where a human stepped in.

**Why that is worth more than a reviewer's verdict.** An adjudication says the
output was wrong, afterwards. A mid-session intervention says where the agent
needed steering, at the moment it needed it -- which is the thing an instruction
change can act on.

**The kickoff is not an intervention, and that is the whole subtlety.** Every
session starts with a human saying what to do. Counting that as steering would
mark every session intervened and the metric would report a constant. So the
boundary is the *first* `agent_action`: a `user_message` before it is the brief,
and one after it is a correction.

**What this deliberately does not distinguish**, stated here rather than assumed
away: a correction from an unrelated follow-up question. "Actually use the other
endpoint" and "while you're there, what's the deploy cadence?" are both
`user_message` rows after the first `agent_action`, and both count as
interventions. That is a coarser signal than "was this specifically a
correction", and it is the honest one available without classifying free text.
Whether it is too noisy is a question real data answers; refining it is a
natural follow-on and not a gap being hidden.

A session with no `agent_action` at all is not counted either way -- nothing ran
autonomously and nothing was corrected, so it is not evidence about autonomy.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ValidationError
from contextplane.types import TenantContext

#: One statement, grouped per session.
#:
#: `first_agent_action` is the session's own boundary between brief and
#: correction, computed as a window function rather than a second query so a
#: session cannot be classified against another session's boundary.
#:
#: Scoped by `actor_id`, which on this table is the actor whose session it is.
#: There is no author/owner split here -- unlike the claim tables, where the
#: tenant that wrote a row and the tenant that owns its subject are different
#: columns -- so the column that looks obvious is also the right one. Said out
#: loud because the neighbouring accuracy read has exactly that trap.
_AUTONOMY_SQL = """
WITH bounded AS (
    SELECT session_id,
           kind,
           seq,
           min(seq) FILTER (WHERE kind = 'agent_action')
               OVER (PARTITION BY session_id) AS first_agent_action
      FROM memory_session_events
     WHERE tenant_id = :tenant
       AND actor_id = :actor
       AND created_at >= :window_start
       AND created_at < :window_end
       AND invalidated_at IS NULL
)
SELECT session_id,
       bool_or(kind = 'user_message' AND seq > first_agent_action) AS intervened
  FROM bounded
 WHERE first_agent_action IS NOT NULL
 GROUP BY session_id
"""


@dataclasses.dataclass(frozen=True)
class AutonomyBreakdown:
    """How many of an agent's sessions ran without a human stepping in."""

    author_actor_id: uuid.UUID
    window_start: datetime.datetime
    window_end: datetime.datetime
    n_sessions: int
    n_intervened: int

    @property
    def n_autonomous(self) -> int:
        """Sessions that ran to completion with no human stepping in."""
        return self.n_sessions - self.n_intervened

    @property
    def intervention_rate(self) -> float | None:
        """Intervened as a fraction of sessions, or None when there were none.

        `None` rather than 0.0, for the reason the accuracy read gives about its
        own rate: an agent that ran no sessions has an *unknown* intervention
        rate, and zero is the specific and flattering claim that it never needed
        help.
        """
        return None if self.n_sessions == 0 else self.n_intervened / self.n_sessions

    @property
    def autonomy_rate(self) -> float | None:
        """The complement, named because it is the figure a reader wants."""
        rate = self.intervention_rate
        return None if rate is None else 1.0 - rate


class AgentAutonomyService:
    """Intervention-rate reads for one tenant's agent."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def autonomy_for(
        self,
        ctx: TenantContext,
        *,
        author_actor_id: uuid.UUID,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> AutonomyBreakdown:
        """How many sessions this agent completed without being steered.

        Computed on read rather than materialised, matching the accuracy read:
        the window is a caller's question, not a fixed reporting period, and a
        stored series would have to pick one.
        """
        if window_end <= window_start:
            raise ValidationError("window_end must be after window_start")

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(_AUTONOMY_SQL),
                    {
                        "tenant": ctx.tenant_id,
                        "actor": author_actor_id,
                        "window_start": window_start,
                        "window_end": window_end,
                    },
                )
            ).all()

        return AutonomyBreakdown(
            author_actor_id=author_actor_id,
            window_start=window_start,
            window_end=window_end,
            n_sessions=len(rows),
            n_intervened=sum(1 for row in rows if row.intervened),
        )


__all__ = ["AgentAutonomyService", "AutonomyBreakdown"]
