"""Erasure: physically deleting everything one actor authored, across subsystems.

A right-to-be-forgotten request is about a person, not about a feature. It has
to reach every place that person's content landed, and the caller asking for it
has no way to know which subsystems those are.

Before this existed there was one erasure path, hardcoded to workspace content,
and any subsystem that later stored personal data had two options: bolt itself
onto a method named for workspaces, or be quietly missed. The second is the
likelier one, and it fails silently — an erasure that reports success while
leaving rows behind is worse than one that errors, because the person is told
they are gone.

So each subsystem registers what it can erase, and the request fans out across
everything registered. Adding a subsystem means adding a participant, and
forgetting to means the gap is visible in one list rather than invisible across
the codebase.

**Physical deletion, not invalidation.** Everywhere else in this system a
removal is a soft-invalidate so the audit trail stays whole. Erasure is the
deliberate exception: the whole point is that the rows stop existing. That is
why it is a separate path from an actor deleting one of their own events, which
soft-invalidates and remains addressable.

**Every participant must be idempotent.** A second request must succeed and
report nothing left to do. Retrying a partially-failed erasure is the normal
case, not the exception, and a participant that errored on already-erased data
would make the retry impossible exactly when it is needed.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from typing import Protocol

from contextplane.types import TenantContext

_log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ErasureCounts:
    """What one participant removed, for the audit record and the response.

    Free-form rather than a fixed shape, because subsystems delete different
    things and flattening them into shared column names would lose which was
    which. The keys are the participant's own vocabulary.
    """

    subsystem: str
    removed: dict[str, int]

    @property
    def total(self) -> int:
        """Sum across the participant's own kinds, for a single audit-record figure."""
        return sum(self.removed.values())


class ErasureParticipant(Protocol):
    """One subsystem's contribution to erasing an actor.

    Takes the requesting context so a participant can enforce its own tenant
    scoping, and the target actor. Must be idempotent.
    """

    @property
    def subsystem(self) -> str:
        """Stable name this participant registers under and reports counts as."""
        ...

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Remove everything this subsystem holds for the actor and return counts by kind.

        Must be safe to call again on data already erased — a retry after a
        partial failure is the expected recovery path, not an edge case.
        """
        ...


class ErasureRegistry:
    """Fans a right-to-be-forgotten request across every registered subsystem."""

    def __init__(self) -> None:
        self._participants: list[ErasureParticipant] = []

    def register(self, participant: ErasureParticipant) -> None:
        """Add a participant, refusing a duplicate subsystem name.

        A second registration under the same name would double-count that
        subsystem's removals and could shadow the first registration's
        behavior without anyone noticing — the failure is loud instead.
        """
        existing = {p.subsystem for p in self._participants}
        if participant.subsystem in existing:
            # Registering twice would double-count and, worse, suggest the
            # second registration replaced the first when it did not.
            msg = f"erasure participant {participant.subsystem!r} is already registered"
            raise ValueError(msg)
        self._participants.append(participant)

    @property
    def subsystems(self) -> tuple[str, ...]:
        """Which subsystems an erasure currently reaches.

        Exposed so a deployment can see its own coverage, and so a test can
        assert a new subsystem was actually wired rather than merely written.
        """
        return tuple(p.subsystem for p in self._participants)

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> list[ErasureCounts]:
        """Erase the actor everywhere, and report what each subsystem removed.

        Participants run in registration order and a failure propagates rather
        than being collected. Continuing past one would report partial success
        as success, and the caller would have no way to tell that some of the
        person's data is still there. A failed erasure must be retried, which
        is why every participant is idempotent.
        """
        results: list[ErasureCounts] = []
        for participant in self._participants:
            removed = await participant.erase_actor(ctx, target_actor_id)
            _log.info(
                "erasure.subsystem_complete: subsystem=%s actor=%s removed=%s",
                participant.subsystem,
                target_actor_id,
                removed,
            )
            results.append(ErasureCounts(subsystem=participant.subsystem, removed=removed))
        return results


class WorkspaceErasure:
    """The workspace subsystem's participation.

    A thin adapter over `WorkspaceService.purge_actor_personal_data`, which
    already did exactly this job and was the only erasure path in the system.
    Wrapped rather than moved: its two-step algorithm and its role check are
    well-tested, and rewriting them to fit a new interface would risk the one
    path that already worked.
    """

    subsystem = "workspace"

    def __init__(self, workspace_service: object) -> None:
        self._workspaces = workspace_service

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Delegate to the workspace service's own purge and reshape its result into counts."""
        result = await self._workspaces.purge_actor_personal_data(  # type: ignore[attr-defined]
            ctx, target_actor_id=target_actor_id
        )
        return {
            "entries": result.purged_entries,
            "workspaces": result.purged_workspaces,
        }


class SessionMemoryErasure:
    """The session-memory subsystem's participation.

    Deletes the actor's events outright, including ones already
    soft-invalidated by their own deletion or by retention. Those rows survive
    an ordinary removal precisely so the audit trail stays whole, and an
    erasure request is the one thing that overrides that.
    """

    subsystem = "session_memory"

    def __init__(self, memory_service: object) -> None:
        self._memory = memory_service

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Delete the actor's events outright, including already-invalidated rows.

        Returns a per-table breakdown rather than one number: an erasure
        receipt that says "12" cannot be checked against anything, and the
        extraction queue is a second place the actor's identifiers lived.
        """
        counts: dict[str, int] = await self._memory.erase_actor_events(  # type: ignore[attr-defined]
            ctx, target_actor_id=target_actor_id
        )
        return counts


class EmbeddingErasure:
    """The embedding index's participation.

    Vectors are derived data, but they are not a summary: `text_chunk` holds the source
    text verbatim, so an erasure that skipped them would leave the erased person's own
    words searchable through the semantic arm. Nothing deleted from `embeddings` at all
    before this participant existed.
    """

    subsystem = "embeddings"

    def __init__(self, embedding_index: object) -> None:
        self._index = embedding_index

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Delegate to the embedding index's own actor-scoped delete, vectors included."""
        counts: dict[str, int] = await self._index.erase_actor(  # type: ignore[attr-defined]
            ctx, target_actor_id
        )
        return counts


__all__ = [
    "EmbeddingErasure",
    "ErasureCounts",
    "ErasureParticipant",
    "ErasureRegistry",
    "SessionMemoryErasure",
    "WorkspaceErasure",
]
