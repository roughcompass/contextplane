"""Which tasks this caller is on, so a field naming one can offer it.

E23-T1. Twelve dashboard fields ask for an intent, a tenant or a receipt by
UUID, and none of the three could be listed. This is the first of the three.

## An intent is not a row, and that decides the shape

There is no `intents` table. An intent exists as an `intent_id` referenced from
`intent_participant_grants` and `intent_checkpoints` and nowhere else, which means
there is no such thing as "every task in this tenant" to return. What there is
is **every task this actor has a grant on**, and that is the right answer rather
than a limitation the query works around: participation is already the
authorization rule for reading a task's material, so a directory scoped to it
cannot show a caller a task whose checkpoints they could not open.

**Expired grants are excluded here and included by `list_grants`.** The two are
answering different questions. `list_grants` audits who was on a task, so a
revoked participant must still appear or a past read becomes unexplainable. This
picks what the caller may work on now, and offering a task they can no longer
open would be offering a refusal.

## The label is the latest checkpoint's goal, and it may be absent

A task with no checkpoint yet has nothing to call itself. That is a real state —
a grant is written before the first checkpoint — so the goal is nullable and the
surface says "no checkpoint yet" rather than rendering the UUID it exists to stop
showing people.

Latest rather than first: a task's goal is restated as it moves, and the oldest
statement is the one least likely to describe what it is now.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import TYPE_CHECKING, Final

from sqlalchemy import text

from contextplane.exceptions import ValidationError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import Clock, TenantContext

MAX_PAGE_SIZE: Final[int] = 100
DEFAULT_PAGE_SIZE: Final[int] = 50


@dataclasses.dataclass(frozen=True)
class IntentSummary:
    """One task this caller participates in."""

    intent_id: uuid.UUID
    #: The latest checkpoint's goal, or `None` when the task has none yet.
    goal: str | None
    #: This caller's role on it. What they may do, not what exists.
    role: str
    checkpoint_count: int
    latest_checkpoint_at: datetime.datetime | None
    granted_at: datetime.datetime
    expires_at: datetime.datetime | None


#: Newest activity first, because a picker's first rows are the ones a reader is
#: most likely to want and "what I touched last" is a better guess than "what I
#: was granted first". A task with no checkpoint sorts by its grant instead of
#: sinking below every task that has one.
_LIST = text(
    """
    SELECT g.intent_id,
           g.role,
           g.granted_at,
           g.expires_at,
           c.goal,
           c.recorded_at AS latest_checkpoint_at,
           coalesce(n.checkpoint_count, 0) AS checkpoint_count
      FROM intent_participant_grants g
      LEFT JOIN LATERAL (
          SELECT goal, recorded_at
            FROM intent_checkpoints
           WHERE tenant_id = g.tenant_id AND intent_id = g.intent_id
           ORDER BY sequence DESC
           LIMIT 1
      ) c ON TRUE
      LEFT JOIN LATERAL (
          SELECT count(*) AS checkpoint_count
            FROM intent_checkpoints
           WHERE tenant_id = g.tenant_id AND intent_id = g.intent_id
      ) n ON TRUE
     WHERE g.tenant_id = :tenant_id
       AND g.actor_id = :actor_id
       AND (g.expires_at IS NULL OR g.expires_at > :now)
     ORDER BY coalesce(c.recorded_at, g.granted_at) DESC, g.intent_id
     LIMIT :limit
    """
)


class IntentDirectoryService:
    """The tasks one caller may work on."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def list_intents(
        self, ctx: TenantContext, *, page_size: int = DEFAULT_PAGE_SIZE
    ) -> tuple[IntentSummary, ...]:
        """Tasks this actor has a live grant on, most recently touched first.

        Scoped to the caller inside the query rather than filtered after it. A
        version that read the tenant's tasks and then removed the ones the caller
        cannot see would be one refactor away from returning the wrong set, and
        the failure would be a disclosure rather than an error.
        """
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValidationError(f"page_size is 1 to {MAX_PAGE_SIZE}, got {page_size}")

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    _LIST,
                    {
                        "actor_id": str(ctx.actor_id),
                        "limit": page_size,
                        "now": self._clock.now(),
                        "tenant_id": ctx.tenant_id,
                    },
                )
            ).mappings()

        return tuple(
            IntentSummary(
                checkpoint_count=int(row["checkpoint_count"]),
                expires_at=row["expires_at"],
                goal=row["goal"],
                granted_at=row["granted_at"],
                intent_id=row["intent_id"],
                latest_checkpoint_at=row["latest_checkpoint_at"],
                role=row["role"],
            )
            for row in rows
        )


__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "IntentDirectoryService", "IntentSummary"]
