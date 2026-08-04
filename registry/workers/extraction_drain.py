"""The extraction drain: pull a queued window, call the provider, stage what conforms.

Runs on the existing scheduler alongside the embedding drain, with the same
`SELECT ... FOR UPDATE SKIP LOCKED` claim so concurrent instances and overlapping
ticks cannot double-process a row.

**One row's failure never touches another's.** Each queued window is its own
transaction and its own try. A provider that times out on one session must not
stall the twenty behind it, and a strategy with a defective prompt must not stop
the strategies that work.

**Retriable and terminal failures are handled differently, because they are
different.** A rate limit means "later"; a rejected API key means "never, until
somebody changes something". Retrying the second is three more calls with the same
wrong credential, and the backoff would hide the real problem behind an apparently
busy queue. So a terminal failure dead-letters immediately rather than serving
out its retries.

**Backoff is written to the row, not slept in the worker.** A worker that slept
would hold a database connection and a scheduler slot doing nothing, and would
lose its place entirely on restart. `next_attempt_at` survives both.

**Nothing here writes claims directly.** Staging goes through the extraction
service and from there through the single claim write path, so every invariant a
claim carries applies to extracted ones exactly as it does to any other.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid

from prometheus_client import Counter, Gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.extraction.config import StrategyConfigService
from registry.extraction.containment import new_boundary
from registry.extraction.provider import (
    ExtractionProvider,
    ExtractionRequest,
    ProviderError,
)
from registry.extraction.service import ExtractionService
from registry.extraction.strategies import STRATEGIES, Strategy
from registry.service.memory.session_events import SessionEvent
from registry.types import Clock, TenantContext

_log = logging.getLogger(__name__)

# 30s, 60s, 120s then dead-letter, matching the embedding drain's shape. Three
# attempts is enough to ride out a restart or a rate-limit window and few enough
# that a genuinely broken prompt surfaces the same day.
BACKOFF_SCHEDULE_S: tuple[int, ...] = (30, 60, 120)
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE_S)

# Per tick. Bounded so one tenant with a large backlog cannot monopolize a tick,
# and low enough that a tick finishes well inside the scheduler interval.
DEFAULT_BATCH_SIZE = 10

# Events handed to the provider in one call. A window larger than this is
# extracted in successive runs rather than in one enormous prompt: cost is
# superlinear in context and a model's attention is not.
DEFAULT_WINDOW_EVENTS = 40

_PENDING = Gauge(
    "registry_extraction_outbox_pending",
    "Rows currently queued for extraction.",
)

_DRAINED = Counter(
    "registry_extraction_outbox_drained_total",
    "Outbox rows processed, by outcome.",
    ["outcome"],
)

_DEAD_LETTERED = Counter(
    "registry_extraction_dead_lettered_total",
    "Outbox rows that exhausted retries or failed terminally, by strategy.",
    ["strategy"],
)

_OUTCOME_OK = "staged"
_OUTCOME_RETRY = "retry_scheduled"
_OUTCOME_DEAD = "dead_lettered"
_OUTCOME_EMPTY = "no_events"
_OUTCOME_UNKNOWN_STRATEGY = "unknown_strategy"
_OUTCOME_DISABLED = "strategy_disabled"


@dataclasses.dataclass(frozen=True)
class DrainReport:
    """What one tick did. Returned rather than only logged so tests can assert."""

    claimed: int
    staged_claims: int
    retried: int
    dead_lettered: int
    refusals: int

    @property
    def had_work(self) -> bool:
        return self.claimed > 0


class ExtractionDrainWorker:
    """Drains the extraction outbox. One tick, one bounded batch."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: ExtractionProvider,
        extraction: ExtractionService,
        *,
        clock: Clock,
        batch_size: int = DEFAULT_BATCH_SIZE,
        window_events: int = DEFAULT_WINDOW_EVENTS,
        config: StrategyConfigService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._extraction = extraction
        self._clock = clock
        self._batch_size = batch_size
        self._window_events = window_events
        # Constructed here when not supplied, so the worker resolves per-tenant
        # configuration by default rather than only when somebody remembers to
        # wire it. A default of "shipped settings for everyone" would mean a
        # tenant's disable flag silently did nothing.
        self._config = config or StrategyConfigService(session_factory, clock=clock)

    async def run_once(self) -> DrainReport:
        """Claim and process up to one batch of queued windows."""
        now = self._clock.now()
        rows = await self._claim(now)
        await self._refresh_pending_gauge()

        if not rows:
            return DrainReport(claimed=0, staged_claims=0, retried=0, dead_lettered=0, refusals=0)

        staged = retried = dead = refusals = 0
        for row in rows:
            # Each row gets its own attempt. A provider that fails on one session
            # must not stall the rest of the batch.
            outcome = await self._process(row, now)
            staged += outcome.staged
            refusals += outcome.refusals
            if outcome.result == _OUTCOME_RETRY:
                retried += 1
            elif outcome.result == _OUTCOME_DEAD:
                dead += 1

        await self._refresh_pending_gauge()
        return DrainReport(
            claimed=len(rows),
            staged_claims=staged,
            retried=retried,
            dead_lettered=dead,
            refusals=refusals,
        )

    # -- claiming ---------------------------------------------------------------

    async def _claim(self, now: datetime.datetime) -> list[_Row]:
        """Take up to `batch_size` eligible rows, locking them for this worker.

        SKIP LOCKED rather than a status column: a crashed worker's rows become
        claimable again when its transaction dies, with nothing to reconcile. A
        status flag would need a reaper for exactly that case.
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "SELECT outbox_id, tenant_id, actor_id, session_id, strategy_id, "
                    "       from_seq, through_seq, attempts, enqueued_at "
                    "FROM memory_extraction_outbox "
                    "WHERE next_attempt_at IS NULL OR next_attempt_at <= CAST(:now AS TIMESTAMPTZ) "
                    "ORDER BY enqueued_at "
                    "LIMIT :lim "
                    "FOR UPDATE SKIP LOCKED"
                ),
                {"now": now, "lim": self._batch_size},
            )
            return [
                _Row(
                    outbox_id=r.outbox_id,
                    tenant_id=r.tenant_id,
                    actor_id=r.actor_id,
                    session_id=r.session_id,
                    strategy_id=r.strategy_id,
                    from_seq=r.from_seq,
                    through_seq=r.through_seq,
                    attempts=r.attempts,
                    enqueued_at=r.enqueued_at,
                )
                for r in result.all()
            ]

    # -- processing -------------------------------------------------------------

    async def _process(self, row: _Row, now: datetime.datetime) -> _Outcome:
        base = STRATEGIES.get(row.strategy_id)
        if base is None:
            # A queued strategy this build does not have. Dead-lettered rather
            # than retried forever: no number of attempts will make an unknown
            # strategy known, and leaving it queued would make the backlog grow
            # without bound after a rollback.
            await self._dead_letter(row, f"unknown strategy {row.strategy_id!r}", now)
            _DRAINED.labels(outcome=_OUTCOME_UNKNOWN_STRATEGY).inc()
            _DEAD_LETTERED.labels(strategy=row.strategy_id).inc()
            return _Outcome(_OUTCOME_DEAD, 0, 0)

        resolved = await self._config.resolve_one(row.tenant_id, row.strategy_id)
        strategy = resolved.strategy
        if not resolved.is_enabled:
            # Disabled after the row was queued. Completed rather than left
            # pending: a disabled strategy's backlog would otherwise grow
            # silently and then flood when somebody re-enabled it, extracting
            # from transcripts that are weeks old.
            await self._complete(row, through_seq=row.through_seq)
            _DRAINED.labels(outcome=_OUTCOME_DISABLED).inc()
            return _Outcome(_OUTCOME_DISABLED, 0, 0)

        events = await self._load_window(row)
        if not events:
            # The window is gone -- erased, expired, or already consumed. Not a
            # failure and not worth a retry: there is nothing to extract, and the
            # queue row would otherwise be immortal.
            await self._complete(row, through_seq=row.through_seq)
            _DRAINED.labels(outcome=_OUTCOME_EMPTY).inc()
            return _Outcome(_OUTCOME_EMPTY, 0, 0)

        boundary = new_boundary()
        request = ExtractionRequest(
            events=events,
            strategy_id=strategy.strategy_id,
            system_prompt=strategy.system_prompt,
            output_schema=strategy.output_schema,
            model_id=strategy.default_model_id,
            max_output_tokens=strategy.max_output_tokens,
            permitted_predicates=strategy.permitted_predicates,
            requested_at=now,
        )

        try:
            result = await self._provider.extract(request)
        except ProviderError as exc:
            # The provider itself decides retriable versus terminal, because only
            # it knows which status meant what.
            if exc.is_retriable:
                return await self._schedule_retry(row, str(exc), now)
            await self._dead_letter(row, str(exc), now)
            _DRAINED.labels(outcome=_OUTCOME_DEAD).inc()
            _DEAD_LETTERED.labels(strategy=row.strategy_id).inc()
            return _Outcome(_OUTCOME_DEAD, 0, 0)

        ctx = TenantContext(
            tenant_id=row.tenant_id,
            actor_id=row.actor_id,
            # The extraction worker acts for the actor whose session it is, with
            # no request to authorize. Roles are empty because there is no role
            # to claim: staging authorizes on tenant and subject ownership, not
            # on a role the worker would have to invent.
            roles=[],
            oidc_subject=f"extraction-worker:{row.strategy_id}",
        )

        lag = (now - events[0].created_at).total_seconds()
        outcome = await self._extraction.stage_result(
            ctx,
            strategy=strategy,
            result=result,
            known_event_ids=frozenset(str(e.event_id) for e in events),
            boundary=boundary,
            lag_seconds=max(0.0, lag),
            confidence_floor=resolved.confidence_floor,
            namespace=resolved.namespace_for(tenant_id=row.tenant_id, actor_id=row.actor_id, session_id=row.session_id),
        )

        await self._complete(row, through_seq=events[-1].seq)
        _DRAINED.labels(outcome=_OUTCOME_OK).inc()
        return _Outcome(_OUTCOME_OK, len(outcome.staged), len(outcome.refusals))

    async def _load_window(self, row: _Row) -> tuple[SessionEvent, ...]:
        """The queued window's events, bounded and in sequence order.

        Ordered by `seq`, never by timestamp: two events written in the same
        millisecond have no stable order under a clock, and a transcript handed
        to a model out of order produces claims about a conversation that did not
        happen.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT event_id, session_id, seq, kind, body, tool_name, metadata, "
                    "       created_at "
                    "FROM memory_session_events "
                    "WHERE tenant_id = :tid AND actor_id = :aid AND session_id = :sid "
                    "  AND seq >= CAST(:from_seq AS BIGINT) "
                    "  AND seq <= CAST(:through_seq AS BIGINT) "
                    "  AND invalidated_at IS NULL "
                    "ORDER BY seq "
                    "LIMIT :lim"
                ),
                {
                    "tid": row.tenant_id,
                    "aid": row.actor_id,
                    "sid": row.session_id,
                    "from_seq": row.from_seq,
                    "through_seq": row.through_seq,
                    "lim": self._window_events,
                },
            )
            return tuple(
                SessionEvent(
                    event_id=r.event_id,
                    session_id=r.session_id,
                    seq=r.seq,
                    kind=r.kind,
                    body=r.body,
                    tool_name=r.tool_name,
                    metadata=r.metadata or {},
                    created_at=r.created_at,
                )
                for r in result.all()
            )

    # -- completion and failure -------------------------------------------------

    async def _complete(self, row: _Row, *, through_seq: int) -> None:
        """Delete the row, or advance it if events arrived while we worked.

        Advancing rather than deleting matters: a turn written during the
        provider call would otherwise be dropped, because the enqueue that
        covered it upserted onto the row being deleted.
        """
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM memory_extraction_outbox "
                    "WHERE outbox_id = :oid "
                    "  AND through_seq <= CAST(:through AS BIGINT)"
                ),
                {"oid": row.outbox_id, "through": through_seq},
            )
            await session.execute(
                text(
                    "UPDATE memory_extraction_outbox "
                    "SET from_seq = CAST(:next AS BIGINT), attempts = 0, "
                    "    next_attempt_at = NULL, last_error = NULL "
                    "WHERE outbox_id = :oid"
                ),
                {"oid": row.outbox_id, "next": through_seq + 1},
            )

    async def _schedule_retry(self, row: _Row, error: str, now: datetime.datetime) -> _Outcome:
        """Back off, or dead-letter if the retries are spent."""
        attempts = row.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            await self._dead_letter(row, error, now, attempts=attempts)
            _DRAINED.labels(outcome=_OUTCOME_DEAD).inc()
            _DEAD_LETTERED.labels(strategy=row.strategy_id).inc()
            return _Outcome(_OUTCOME_DEAD, 0, 0)

        delay = BACKOFF_SCHEDULE_S[attempts - 1]
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE memory_extraction_outbox "
                    "SET attempts = CAST(:attempts AS INTEGER), "
                    "    last_error = :err, "
                    "    last_attempt_at = CAST(:now AS TIMESTAMPTZ), "
                    "    next_attempt_at = CAST(:now AS TIMESTAMPTZ) "
                    "                      + make_interval(secs => CAST(:delay AS INTEGER)) "
                    "WHERE outbox_id = :oid"
                ),
                {
                    "oid": row.outbox_id,
                    "attempts": attempts,
                    "err": error[:2000],
                    "now": now,
                    "delay": delay,
                },
            )
        _DRAINED.labels(outcome=_OUTCOME_RETRY).inc()
        _log.info(
            "extraction.retry strategy=%s session=%s attempt=%d/%d delay=%ds",
            row.strategy_id,
            row.session_id,
            attempts,
            MAX_ATTEMPTS,
            delay,
        )
        return _Outcome(_OUTCOME_RETRY, 0, 0)

    async def _dead_letter(self, row: _Row, error: str, now: datetime.datetime, *, attempts: int | None = None) -> None:
        """Move the row to the dead-letter table, keeping why and how hard.

        One transaction, so a crash between the insert and the delete cannot both
        lose the row and leave it queued.
        """
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_extraction_outbox_failed "
                    "  (tenant_id, actor_id, session_id, strategy_id, from_seq, through_seq, "
                    "   attempts, last_error, enqueued_at, failed_at) "
                    "VALUES (:tid, :aid, :sid, :strat, CAST(:from_seq AS BIGINT), "
                    "        CAST(:through AS BIGINT), CAST(:attempts AS INTEGER), :err, "
                    "        CAST(:enq AS TIMESTAMPTZ), CAST(:now AS TIMESTAMPTZ))"
                ),
                {
                    "tid": row.tenant_id,
                    "aid": row.actor_id,
                    "sid": row.session_id,
                    "strat": row.strategy_id,
                    "from_seq": row.from_seq,
                    "through": row.through_seq,
                    "attempts": attempts if attempts is not None else row.attempts,
                    "err": error[:2000],
                    "enq": row.enqueued_at,
                    "now": now,
                },
            )
            await session.execute(
                text("DELETE FROM memory_extraction_outbox WHERE outbox_id = :oid"),
                {"oid": row.outbox_id},
            )
        _log.warning(
            "extraction.dead_lettered strategy=%s session=%s attempts=%s error=%s",
            row.strategy_id,
            row.session_id,
            attempts if attempts is not None else row.attempts,
            error[:200],
        )

    async def _refresh_pending_gauge(self) -> None:
        async with self._session_factory() as session:
            pending = (await session.execute(text("SELECT count(*) FROM memory_extraction_outbox"))).scalar_one()
        _PENDING.set(pending)


async def enqueue_extraction(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    session_id: str,
    seq: int,
    strategies: tuple[Strategy, ...],
) -> None:
    """Queue this event's session for extraction, in the caller's transaction.

    Takes the caller's session on purpose: the enqueue must commit with the event
    or not at all. A separate transaction could store an event nobody extracts,
    or queue extraction for an event that was rolled back.

    Upserts, so a burst of ten events in one session widens one window rather
    than queueing ten jobs. `from_seq` is left at whatever a pending row already
    holds -- the earliest unextracted turn -- because narrowing it would skip the
    turns between.
    """
    for strategy in strategies:
        await session.execute(
            text(
                "INSERT INTO memory_extraction_outbox "
                "  (tenant_id, actor_id, session_id, strategy_id, from_seq, through_seq) "
                "VALUES (:tid, :aid, :sid, :strat, CAST(:seq AS BIGINT), CAST(:seq AS BIGINT)) "
                "ON CONFLICT (tenant_id, actor_id, session_id, strategy_id) DO UPDATE "
                "SET through_seq = GREATEST("
                "      memory_extraction_outbox.through_seq, CAST(:seq AS BIGINT)), "
                # A row that was backing off becomes eligible again when new
                # material arrives: the earlier failure may have been about the
                # window, and there is now more of it.
                "    next_attempt_at = NULL"
            ),
            {
                "tid": tenant_id,
                "aid": actor_id,
                "sid": session_id,
                "strat": strategy.strategy_id,
                "seq": seq,
            },
        )


@dataclasses.dataclass(frozen=True)
class _Row:
    outbox_id: uuid.UUID
    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    session_id: str
    strategy_id: str
    from_seq: int
    through_seq: int
    attempts: int
    enqueued_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class _Outcome:
    result: str
    staged: int
    refusals: int


__all__ = [
    "BACKOFF_SCHEDULE_S",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_WINDOW_EVENTS",
    "MAX_ATTEMPTS",
    "DrainReport",
    "ExtractionDrainWorker",
    "enqueue_extraction",
]
