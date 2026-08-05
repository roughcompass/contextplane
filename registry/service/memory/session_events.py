"""An agent's own session memory: append, replay, list, delete.

The substrate the living-memory loop returns through. An agent records what it
did; a later process reads it back and resumes. Nothing here extracts, scores,
or infers -- this layer is deliberately free of any model, and the scenario it
exists to pass runs with no LLM provider configured at all: record a turn, kill
the process, resume the session, replay the last few turns in reverse, and
resolve a follow-up that refers to an earlier subject only by pronoun. That
works because replay returns the actual prior turns.

**Scoping is by actor, and that is the whole security model.** Every read here
carries `(tenant_id, actor_id)`. Tenant scoping alone -- which is what every
other read path in this system uses -- would expose every session to every
colleague, because a session is not shared content. `VisibilityService` cannot
express this: its same-tenant branch returns visible for any actor in the
owning tenant, correct for a catalog entity and exactly wrong for a private
conversation. The pattern followed instead is `workspace`'s, which already
scopes personal content by owning actor.

A session belonging to someone else is reported as absent rather than
forbidden. Distinguishing the two confirms the session exists, which is itself
something the caller is not entitled to know.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import NotFoundError, ValidationError
from registry.types import Clock, TenantContext

if TYPE_CHECKING:  # pragma: no cover - import cycle: extraction reads SessionEvent
    from registry.extraction.strategies import Strategy

# The closed vocabulary. A kind outside it is refused rather than stored: the
# extraction phases that read this table will branch on kind, and an unknown
# value would reach them as a silent gap rather than a rejected write.
EVENT_KINDS = frozenset({"user_message", "agent_action", "tool_invocation"})

# Why an event left the default read path. Bounded, because the column is a
# code an operator triages by, not a message sink.
REASON_ACTOR_DELETED = "actor_deleted"
REASON_RETENTION = "retention_expired"

MAX_BODY_BYTES = 16 * 1024
DEFAULT_PAGE = 100
MAX_PAGE = 1000

# Two appends racing for one sequence number collide on the unique constraint.
# Retried rather than surfaced: the loser's event is perfectly valid, it just
# needs the next position. Bounded because unbounded retry on a genuine
# constraint problem would spin forever.
_MAX_SEQ_ATTEMPTS = 5
_UNIQUE_VIOLATION = "23505"


@dataclasses.dataclass(frozen=True)
class SessionEvent:
    """One recorded moment, as a caller sees it."""

    event_id: uuid.UUID
    session_id: str
    seq: int
    kind: str
    body: str
    tool_name: str | None
    metadata: dict[str, Any]
    created_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class SessionSummary:
    """One session in a listing: enough to decide whether to resume it."""

    session_id: str
    event_count: int
    first_activity_at: datetime.datetime
    last_activity_at: datetime.datetime


class MemoryService:
    """Session events, scoped to the calling actor."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        extraction_strategies: tuple[Strategy, ...] = (),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        # Empty by default, which means no extraction is queued at all. Passing
        # strategies in rather than importing a global set keeps this service
        # usable on a deployment that captures sessions and extracts nothing --
        # and keeps the enqueue out of the way of every existing memory test.
        self._extraction_strategies = extraction_strategies

    # -- write ----------------------------------------------------------------

    async def record_event(
        self,
        ctx: TenantContext,
        *,
        session_id: str,
        kind: str,
        body: str,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionEvent:
        """Append one immutable event to the caller's session.

        The session is not created; it exists because its events do. That is
        why there is no session table to keep in step with this write.
        """
        size_bytes = self._validate(kind=kind, body=body, tool_name=tool_name)
        actor_id = _require_actor(ctx)
        now = self._clock.now()

        for _ in range(_MAX_SEQ_ATTEMPTS):
            try:
                return await self._append(
                    ctx,
                    actor_id=actor_id,
                    session_id=session_id,
                    kind=kind,
                    body=body,
                    tool_name=tool_name,
                    metadata=metadata or {},
                    now=now,
                    size_bytes=size_bytes,
                )
            except IntegrityError as exc:
                if _sqlstate(exc) != _UNIQUE_VIOLATION:
                    raise
                # Lost the race for this position. The next read of MAX(seq)
                # sees the winner's row, so the retry takes the position after
                # it rather than colliding again.
                continue

        msg = f"could not allocate a sequence for session {session_id!r} after {_MAX_SEQ_ATTEMPTS} attempts"
        raise ValidationError(msg)

    async def _append(
        self,
        ctx: TenantContext,
        *,
        actor_id: uuid.UUID,
        session_id: str,
        kind: str,
        body: str,
        tool_name: str | None,
        metadata: dict[str, Any],
        now: datetime.datetime,
        size_bytes: int,
    ) -> SessionEvent:
        async with self._session_factory() as session, session.begin():
            # Retention is read per write rather than cached, so a tenant that
            # shortens its window applies it to everything recorded afterwards
            # without waiting for a restart.
            retention_days = (
                await session.execute(
                    text("SELECT memory_retention_days FROM tenants WHERE tenant_id = :tid"),
                    {"tid": ctx.tenant_id},
                )
            ).scalar_one()

            row = (
                await session.execute(
                    text(
                        "INSERT INTO memory_session_events ("
                        "  tenant_id, actor_id, session_id, seq, kind, body, tool_name, metadata,"
                        "  created_at, expires_at, size_bytes"
                        ") SELECT :tid, :aid, :sid,"
                        "         COALESCE(("
                        "           SELECT MAX(seq) FROM memory_session_events"
                        "            WHERE tenant_id = :tid AND actor_id = :aid AND session_id = :sid"
                        "         ), 0) + 1,"
                        "         :kind, :body, :tool, CAST(:meta AS JSONB), CAST(:now AS TIMESTAMPTZ),"
                        "         CAST(:now AS TIMESTAMPTZ) + make_interval(days => CAST(:days AS INTEGER)),"
                        "         CAST(:size AS INTEGER) "
                        "RETURNING event_id, seq, created_at"
                    ),
                    {
                        "tid": ctx.tenant_id,
                        "aid": actor_id,
                        "sid": session_id,
                        "kind": kind,
                        "body": body,
                        "tool": tool_name,
                        "meta": json.dumps(metadata, sort_keys=True),
                        "now": now,
                        "days": retention_days,
                        # Recorded at ingest because this is the only moment the
                        # body is in hand. Erasure deletes the row outright, so a
                        # size not captured here is gone rather than late.
                        "size": size_bytes,
                    },
                )
            ).one()

            # Queue the session for extraction in the same transaction as the
            # event. A separate transaction could store an event nobody extracts,
            # or queue work for an event that was then rolled back. Extraction
            # itself is never on this path: the enqueue is one upsert per enabled
            # strategy, and the provider is called by a scheduled drain.
            if self._extraction_strategies:
                # Imported here, not at module scope: the extraction package
                # reads `SessionEvent` from this module, so a top-level import
                # would close a cycle. Deferring it keeps the dependency
                # one-directional at import time while still being one call.
                from registry.workers.extraction_drain import (  # noqa: PLC0415 - import cycle, see comment above
                    enqueue_extraction,
                )

                await enqueue_extraction(
                    session,
                    tenant_id=ctx.tenant_id,
                    actor_id=actor_id,
                    session_id=session_id,
                    seq=row.seq,
                    strategies=self._extraction_strategies,
                )

        return SessionEvent(
            event_id=row.event_id,
            session_id=session_id,
            seq=row.seq,
            kind=kind,
            body=body,
            tool_name=tool_name,
            metadata=metadata,
            created_at=row.created_at,
        )

    def _validate(self, *, kind: str, body: str, tool_name: str | None) -> int:
        """Check the event, and return the body size in bytes.

        Returns the size rather than recomputing it at the write: the cap check
        already encodes the body, and a second encode of the same string is both
        wasted work and a chance for the two numbers to disagree.
        """
        if kind not in EVENT_KINDS:
            msg = f"unknown event kind {kind!r}; expected one of {sorted(EVENT_KINDS)}"
            raise ValidationError(msg)
        size_bytes = len(body.encode("utf-8"))
        if size_bytes > MAX_BODY_BYTES:
            # Bytes, matching the schema. A character check would admit roughly
            # four times the cap in multi-byte text.
            msg = f"event body exceeds {MAX_BODY_BYTES} bytes"
            raise ValidationError(msg)
        if (kind == "tool_invocation") != (tool_name is not None):
            msg = "tool_name is required for a tool_invocation event and not permitted on any other kind"
            raise ValidationError(msg)
        return size_bytes

    # -- read -----------------------------------------------------------------

    async def list_sessions(
        self, ctx: TenantContext, *, since: datetime.datetime | None = None, limit: int = 50
    ) -> list[SessionSummary]:
        """The caller's own sessions, most recently active first.

        Aggregated from the events rather than read from a session table:
        counts and activity bounds are facts about the events, and storing them
        separately would be a second write path that could disagree silently.
        """
        actor_id = _require_actor(ctx)
        rows = await self._read(
            "SELECT session_id, count(*) AS event_count,"
            "       min(created_at) AS first_at, max(created_at) AS last_at "
            "FROM memory_session_events "
            "WHERE tenant_id = :tid AND actor_id = :aid AND invalidated_at IS NULL "
            "  AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR created_at >= CAST(:since AS TIMESTAMPTZ)) "
            "GROUP BY session_id ORDER BY max(created_at) DESC LIMIT :limit",
            {"tid": ctx.tenant_id, "aid": actor_id, "since": since, "limit": _page(limit)},
        )
        return [
            SessionSummary(
                session_id=r.session_id,
                event_count=r.event_count,
                first_activity_at=r.first_at,
                last_activity_at=r.last_at,
            )
            for r in rows
        ]

    async def list_events(
        self,
        ctx: TenantContext,
        *,
        session_id: str,
        since_seq: int | None = None,
        until_seq: int | None = None,
        kind: str | None = None,
        metadata_equals: dict[str, str] | None = None,
        cursor: int | None = None,
        limit: int = DEFAULT_PAGE,
        order: str = "asc",
    ) -> list[SessionEvent]:
        """Replay a session in sequence order, forward or reverse.

        Ordered by `seq`, never by timestamp: a burst of events can share a
        `created_at`, and ordering by it would let "the last five in reverse"
        disagree with the forward query's last five -- which is precisely what
        an agent resuming its own conversation depends on.

        `cursor` is the last `seq` seen, not an offset. An offset over an
        append-only table re-reads shifting windows; a `seq` cursor cannot,
        because `seq` is immutable once assigned.
        """
        actor_id = _require_actor(ctx)
        descending = order == "desc"

        sql = (
            "SELECT event_id, session_id, seq, kind, body, tool_name, metadata, created_at "
            "FROM memory_session_events "
            "WHERE tenant_id = :tid AND actor_id = :aid AND session_id = :sid "
            "  AND invalidated_at IS NULL "
            "  AND (CAST(:since AS BIGINT) IS NULL OR seq >= CAST(:since AS BIGINT)) "
            "  AND (CAST(:until AS BIGINT) IS NULL OR seq <= CAST(:until AS BIGINT)) "
            "  AND (CAST(:kind AS TEXT) IS NULL OR kind = CAST(:kind AS TEXT)) "
            "  AND (CAST(:meta AS JSONB) IS NULL OR metadata @> CAST(:meta AS JSONB)) "
        )
        params: dict[str, object] = {
            "tid": ctx.tenant_id,
            "aid": actor_id,
            "sid": session_id,
            "since": since_seq,
            "until": until_seq,
            "kind": kind,
            "meta": json.dumps(metadata_equals, sort_keys=True) if metadata_equals else None,
            "limit": _page(limit),
        }
        if cursor is not None:
            # Strictly past the cursor, in whichever direction we are reading.
            sql += "  AND seq < :cursor " if descending else "  AND seq > :cursor "
            params["cursor"] = cursor
        sql += f"ORDER BY seq {'DESC' if descending else 'ASC'} LIMIT :limit"

        return [_to_event(r) for r in await self._read(sql, params)]

    async def get_event(self, ctx: TenantContext, *, session_id: str, event_id: uuid.UUID) -> SessionEvent:
        """One event from the caller's own session.

        Invalidated events are excluded here as well as from replay: an actor
        who deleted a moment must not be able to read it back through a
        different route.
        """
        actor_id = _require_actor(ctx)
        rows = await self._read(
            "SELECT event_id, session_id, seq, kind, body, tool_name, metadata, created_at "
            "FROM memory_session_events "
            "WHERE tenant_id = :tid AND actor_id = :aid AND session_id = :sid "
            "  AND event_id = :eid AND invalidated_at IS NULL",
            {"tid": ctx.tenant_id, "aid": actor_id, "sid": session_id, "eid": event_id},
        )
        if not rows:
            msg = f"event {event_id} not found"
            raise NotFoundError(msg)
        return _to_event(rows[0])

    # -- delete ---------------------------------------------------------------

    async def delete_event(self, ctx: TenantContext, *, session_id: str, event_id: uuid.UUID) -> None:
        """Soft-invalidate one of the caller's own events.

        The row stays, addressable by id for audit, and leaves every default
        read path. Both halves matter: an event an actor deleted must stop
        appearing in replay or deletion means nothing, and must remain
        answerable to "what was here" or the audit trail has a hole exactly
        where someone chose to remove something.

        Physical erasure is a different operation with a different
        justification -- see the right-to-be-forgotten purge.
        """
        actor_id = _require_actor(ctx)
        async with self._session_factory() as session, session.begin():
            deleted = (
                await session.execute(
                    text(
                        "UPDATE memory_session_events "
                        "SET invalidated_at = :now, invalidated_reason = :reason "
                        "WHERE tenant_id = :tid AND actor_id = :aid AND session_id = :sid "
                        "  AND event_id = :eid AND invalidated_at IS NULL "
                        "RETURNING event_id"
                    ),
                    {
                        "now": self._clock.now(),
                        "reason": REASON_ACTOR_DELETED,
                        "tid": ctx.tenant_id,
                        "aid": actor_id,
                        "sid": session_id,
                        "eid": event_id,
                    },
                )
            ).one_or_none()

        if deleted is None:
            # Absent, already deleted, or somebody else's -- reported
            # identically. Which of the three it is would tell the caller
            # something they are not entitled to know.
            msg = f"event {event_id} not found"
            raise NotFoundError(msg)

    # -- erasure ---------------------------------------------------------------

    async def erase_actor_events(self, ctx: TenantContext, *, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Physically delete the target actor's session events and derived queue rows.

        A hard DELETE, and the only one in this service. Everywhere else a
        removal is a soft-invalidate so the audit trail stays whole; an erasure
        request is the deliberate exception, because the point is that the rows
        stop existing. That includes events already soft-invalidated by their
        author or by retention — those survive an ordinary removal precisely so
        they remain answerable, and this is the one thing that overrides it.

        Scoped to the requesting tenant as well as the target actor. An actor
        id is globally unique, so the tenant predicate is not needed to find
        the right rows; it is there so a request made in the context of one
        tenant cannot reach into another, which is the shape every other query
        in this service holds.

        Idempotent: a second call deletes nothing and returns zero. Retrying a
        partly-failed erasure is the normal case.

        Authorization is the caller's. The route holds the admin gate, and
        re-deriving it here from a different source would be a second place for
        the two to disagree.

        Extraction queue rows go too, and in the same transaction. They are
        derived from the events -- they name the actor, the session, and the
        window -- so leaving them behind would leave the actor's session
        identifiers in place after their conversations were erased. The
        dead-letter table is included for the same reason: it holds the same
        identifiers plus a stored error string.
        """
        async with self._session_factory() as session, session.begin():
            events = await session.execute(
                text("DELETE FROM memory_session_events " "WHERE tenant_id = :tid AND actor_id = :aid"),
                {"tid": ctx.tenant_id, "aid": target_actor_id},
            )
            queued = await session.execute(
                text("DELETE FROM memory_extraction_outbox " "WHERE tenant_id = :tid AND actor_id = :aid"),
                {"tid": ctx.tenant_id, "aid": target_actor_id},
            )
            dead = await session.execute(
                text("DELETE FROM memory_extraction_outbox_failed " "WHERE tenant_id = :tid AND actor_id = :aid"),
                {"tid": ctx.tenant_id, "aid": target_actor_id},
            )

        return {
            "session_events": int(events.rowcount or 0),  # type: ignore[attr-defined]
            "extraction_queued": int(queued.rowcount or 0),  # type: ignore[attr-defined]
            "extraction_dead_lettered": int(dead.rowcount or 0),  # type: ignore[attr-defined]
        }

    # -- shared ---------------------------------------------------------------

    async def _read(self, sql: str, params: dict[str, object]) -> list[Any]:
        async with self._session_factory() as session:
            return list((await session.execute(text(sql), params)).all())


def _require_actor(ctx: TenantContext) -> uuid.UUID:
    """Sessions belong to an actor, so a context without one has no memory.

    Refused rather than defaulted. A credential that resolves to a tenant but
    no actor -- a service token, say -- must not read whatever happens to be
    first, and must not silently write events nobody owns.
    """
    if ctx.actor_id is None:
        msg = "session memory requires an actor identity"
        raise ValidationError(msg)
    return ctx.actor_id


def _page(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE))


def _to_event(row: Row[Any]) -> SessionEvent:
    return SessionEvent(
        event_id=row.event_id,
        session_id=row.session_id,
        seq=row.seq,
        kind=row.kind,
        body=row.body,
        tool_name=row.tool_name,
        metadata=dict(row.metadata or {}),
        created_at=row.created_at,
    )


def _sqlstate(exc: BaseException) -> str | None:
    state = getattr(exc, "sqlstate", None) or getattr(getattr(exc, "orig", None), "sqlstate", None)
    return str(state) if state is not None else None


__all__ = [
    "DEFAULT_PAGE",
    "EVENT_KINDS",
    "MAX_BODY_BYTES",
    "MAX_PAGE",
    "REASON_ACTOR_DELETED",
    "REASON_RETENTION",
    "MemoryService",
    "SessionEvent",
    "SessionSummary",
]
