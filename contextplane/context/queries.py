"""The reads behind the workspace arm, and the shape every other arm returns.

The workspace block comes from the checkpoint tables directly rather than
through the task-memory service. That is deliberate: this module composes the
authorization predicate into its own SELECT, and going through a service that
returns already-loaded objects would mean filtering in Python afterwards. A
Python-side filter returns the right rows today and leaks the moment one caller
forgets it -- the predicate belongs in the SQL, where a query that skips it
returns nothing rather than everything.

Only the workspace arm is implemented here. The canonical, ARC and
observed-claims arms are supplied by their own slices; the assembler takes any
callable of the right shape, so those arrive without this module changing.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from contextplane.context.assembler import ArmOutcome, Exclusion, contextual_item, ordered_items
from contextplane.context.schemas.envelope import BLOCK_WORKSPACE
from contextplane.context.schemas.trust import Classification, TrustMetadataV1
from contextplane.workspaces.models import IntentCheckpoint
from contextplane.workspaces.queries_audience import _authorized_task_ids

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

#: What the workspace block's trust metadata says about where an item came from.
#: A checkpoint is one agent's own record of its work: observed rather than
#: asserted by the registry, mutable because a later checkpoint supersedes it,
#: and attributed to whoever wrote it.
_WORKSPACE_SOURCE = "intent_checkpoint"


def _checkpoint_trust(
    *,
    author: str,
    recorded_at: datetime.datetime,
    classification: Classification,
) -> TrustMetadataV1:
    """Trust for one checkpoint.

    `attribution` carries the author because a workspace item's weight depends
    almost entirely on who recorded it -- an item that cannot say is one a
    reader has to treat as anonymous, and anonymous work is not evidence.
    """
    return TrustMetadataV1(
        trust="observed",
        source=_WORKSPACE_SOURCE,
        # An annotation, not a fact: a checkpoint is one agent's record of its own
        # working state, and reading it as a fact the registry asserts is exactly
        # the flattening the five blocks exist to prevent.
        assertion_kind="annotation",
        authority=author,
        freshness=recorded_at,
        mutability="mutable",
        attribution=author,
        classification=classification,
    )


def _checkpoint_payload(row: IntentCheckpoint) -> dict[str, object]:
    """The content a reader gets, without the bookkeeping they cannot act on."""
    payload: dict[str, Any] = {
        "checkpoint_id": str(row.checkpoint_id),
        "intent_id": str(row.intent_id),
        "sequence": row.sequence,
        "goal": row.goal,
        "decisions": row.decisions,
        "assumptions": row.assumptions,
        "completed_checks": row.completed_checks,
        "open_questions": row.open_questions,
        "recorded_at": row.recorded_at.isoformat(),
    }
    if row.next_action is not None:
        payload["next_action"] = row.next_action
    return payload


async def workspace_arm(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: str,
    moment: datetime.datetime,
    intent_ids: tuple[uuid.UUID, ...] = (),
    limit: int = 50,
    classification: Classification = "internal",
) -> ArmOutcome:
    """Checkpoints this actor is authorized to see, newest task activity first.

    Authorization runs in the SELECT, before any content, count or ordering is
    computed. A version that read the rows and then filtered would have already
    loaded content the caller may not see, and a count taken before the filter
    is itself a disclosure -- "there are four checkpoints you cannot read" tells
    a reader something they were not granted.

    Requesting a task the actor cannot see is not an error and does not fail the
    arm. It is recorded as an exclusion, so the receipt can say a task was asked
    for and withheld -- which is what tells a reader to go and ask for access
    rather than concluding the task is empty.
    """
    authorized = _authorized_task_ids(tenant_id=tenant_id, actor_id=actor_id, moment=moment)

    stmt = (
        select(IntentCheckpoint)
        .where(
            IntentCheckpoint.tenant_id == tenant_id,
            IntentCheckpoint.intent_id.in_(authorized),
        )
        .order_by(IntentCheckpoint.recorded_at.desc(), IntentCheckpoint.sequence.desc())
        # One more than the limit, so "there is more" is answered by the query
        # rather than guessed from a full page.
        .limit(limit + 1)
    )
    if intent_ids:
        stmt = stmt.where(IntentCheckpoint.intent_id.in_(intent_ids))

    rows = list((await session.execute(stmt)).scalars().all())
    truncated = len(rows) > limit
    rows = rows[:limit]

    exclusions: tuple[Exclusion, ...] = ()
    if intent_ids:
        visible = {row.intent_id for row in rows}
        withheld = tuple(
            Exclusion(item_key=str(intent_id), reason="no active participant grant for this actor")
            for intent_id in intent_ids
            if intent_id not in visible
        )
        exclusions = withheld

    items = ordered_items(
        [
            contextual_item(
                block=BLOCK_WORKSPACE,
                source=_WORKSPACE_SOURCE,
                item_key=str(row.checkpoint_id),
                payload=_checkpoint_payload(row),
                trust=_checkpoint_trust(
                    author=row.author,
                    recorded_at=row.recorded_at,
                    classification=classification,
                ),
            )
            for row in rows
        ]
    )

    freshest = max((row.recorded_at for row in rows), default=None)
    return ArmOutcome(
        items=items,
        exclusions=exclusions,
        truncated=truncated,
        fresh_as_of=freshest,
    )


__all__ = ["workspace_arm"]
