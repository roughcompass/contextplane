"""Wire shapes for the task-memory surfaces.

These mirror the frozen contract objects rather than re-deciding anything. The
contract already refuses a self-grant, a naive timestamp, a checkpoint that
tries to carry its own ordering; repeating those rules here would create a
second place they can drift, and the shape that drifts is always the one nobody
is looking at.

What these types add is the transport's own concerns: what a client may send
(never `sequence`, `author`, `recorded_at` or `digest` -- all server-decided),
and what a reader gets back in a form that survives JSON.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import BaseModel, Field

from contextplane.workspaces.schemas.task_memory import (
    PARTICIPANT_ROLES,
    TaskCheckpointV1,
    TaskParticipantGrantV1,
)


class GrantCreate(BaseModel):
    """Add one participant to a task.

    `granted_by` is deliberately absent: it is the calling actor, taken from the
    request context. Accepting it would let a caller attribute a grant to
    somebody else, and the stored record would look identical to a real one
    afterwards.
    """

    actor_id: str = Field(min_length=1, description="The actor being granted participation.")
    role: str = Field(description=f"One of {sorted(PARTICIPANT_ROLES)}.")
    expires_at: datetime.datetime | None = Field(
        default=None,
        description="When the grant stops applying. Absent means it lasts as long as the task.",
    )


class GrantResponse(BaseModel):
    """One grant, active or not."""

    task_id: uuid.UUID
    actor_id: str
    role: str
    granted_by: str
    granted_at: datetime.datetime
    expires_at: datetime.datetime | None
    resolver_version: str

    @classmethod
    def of(cls, grant: TaskParticipantGrantV1) -> GrantResponse:
        """The wire shape of one stored grant."""
        return cls(
            task_id=grant.task_id,
            actor_id=grant.actor_id,
            role=grant.role,
            granted_by=grant.granted_by,
            granted_at=grant.granted_at,
            expires_at=grant.expires_at,
            resolver_version=grant.resolver_version,
        )


class GrantListResponse(BaseModel):
    """Every grant on one task, expired ones included.

    Expired grants are not filtered out. An audit of a past read needs the
    grants that applied then, and hiding them makes a revoked participant
    indistinguishable from one who was never there.
    """

    grants: list[GrantResponse]


class CheckpointAppend(BaseModel):
    """One step to record on a task.

    Carries content only. Ordering, identity, attribution, retention and the
    digest are computed server-side from its own view -- a client that could set
    them could write a checkpoint into the middle of somebody else's chain.
    """

    goal: str = Field(min_length=1)
    decisions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    completed_checks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_action: str | None = None
    evidence: list[dict[str, Any]] = Field(
        default_factory=list,
        description="External references supporting this step, normalized server-side.",
    )


class CheckpointResponse(BaseModel):
    """One recorded step, in the shape resume reads back."""

    checkpoint_id: uuid.UUID
    task_id: uuid.UUID
    sequence: int
    predecessor_id: uuid.UUID | None
    goal: str
    decisions: list[str]
    assumptions: list[str]
    completed_checks: list[str]
    open_questions: list[str]
    next_action: str | None
    author: str
    recorded_at: datetime.datetime
    retention_policy: str
    digest: str

    @classmethod
    def of(cls, checkpoint: TaskCheckpointV1) -> CheckpointResponse:
        """The wire shape of one recorded checkpoint."""
        return cls(
            checkpoint_id=checkpoint.checkpoint_id,
            task_id=checkpoint.task_id,
            sequence=checkpoint.sequence,
            predecessor_id=checkpoint.predecessor_id,
            goal=checkpoint.goal,
            decisions=list(checkpoint.decisions),
            assumptions=list(checkpoint.assumptions),
            completed_checks=list(checkpoint.completed_checks),
            open_questions=list(checkpoint.open_questions),
            next_action=checkpoint.next_action,
            author=checkpoint.author,
            recorded_at=checkpoint.recorded_at,
            retention_policy=checkpoint.retention_policy,
            digest=checkpoint.digest,
        )


__all__ = [
    "CheckpointAppend",
    "CheckpointResponse",
    "GrantCreate",
    "GrantListResponse",
    "GrantResponse",
]
