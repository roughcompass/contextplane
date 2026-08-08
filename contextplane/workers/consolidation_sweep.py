"""The sweep that reconciles staged claims on a schedule.

Consolidation runs off the schedule rather than on the write, for one reason: a claim's
correct decision can depend on a claim that has not arrived yet. Reconciling only at
write time would settle each claim against whatever happened to exist at that instant
and never revisit it, so a conflict whose other side showed up second would go
undetected forever.

**Bounded per tick, and oldest first.** A backlog is drained across ticks rather than in
one pass: the alternative is a tick that runs for minutes holding row locks, during
which nothing else can consolidate. Oldest first because a claim waiting longest is the
one whose absence from the store's answers has cost most.

**Every row's failure is its own.** One claim whose neighbourhood is pathological must
not stop the ones behind it, and a sweep that aborted on the first problem would stall
permanently on a single bad row.

**Idempotent, so the cadence is a free variable.** Running every minute and running
every hour reach the same state; the only difference is how stale an answer can be. That
is what makes the interval a tuning decision rather than a correctness one.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid

from prometheus_client import Counter, Gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.memory.consolidation import ConsolidationService
from contextplane.types import Clock

_log = logging.getLogger(__name__)

# Claims reconciled per tick. Bounded so a backlog is drained across ticks rather than
# in one long pass holding locks.
DEFAULT_BATCH_SIZE = 100

_SWEPT = Counter(
    "registry_claim_consolidation_swept_total",
    "Claims reconciled by the sweep, by outcome.",
    ["outcome"],
)

_PENDING = Gauge(
    "registry_claim_consolidation_pending",
    "Live claims never reconciled, or reconciled before something newer arrived.",
)


@dataclasses.dataclass(frozen=True)
class SweepReport:
    """What one tick did. Returned rather than only logged so tests can assert."""

    considered: int
    decided: int
    already_settled: int
    failed: int

    @property
    def had_work(self) -> bool:
        return self.considered > 0


class ConsolidationSweepWorker:
    """Reconciles claims that have not been reconciled since their neighbourhood moved."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        consolidation: ConsolidationService,
        *,
        clock: Clock,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._consolidation = consolidation
        self._clock = clock
        self._batch_size = batch_size

    async def run_once(self) -> SweepReport:
        candidates = await self._candidates()
        await self._refresh_pending_gauge()

        if not candidates:
            return SweepReport(considered=0, decided=0, already_settled=0, failed=0)

        decided = settled = failed = 0
        for claim_id in candidates:
            try:
                outcome = await self._consolidation.consolidate(claim_id)
            except Exception:  # noqa: BLE001 - see comment above
                # Logged with the claim, because a sweep that reported only a count
                # would leave nobody able to find the row that is stuck.
                _log.exception("consolidation.sweep_failed claim_id=%s", claim_id)
                _SWEPT.labels(outcome="failed").inc()
                failed += 1
                continue

            if outcome.already_settled:
                settled += 1
                _SWEPT.labels(outcome="already_settled").inc()
            else:
                decided += 1
                _SWEPT.labels(outcome=outcome.decision).inc()

        await self._refresh_pending_gauge()
        report = SweepReport(
            considered=len(candidates),
            decided=decided,
            already_settled=settled,
            failed=failed,
        )
        if failed:
            _log.warning(
                "consolidation.sweep considered=%d decided=%d failed=%d",
                report.considered,
                report.decided,
                report.failed,
            )
        return report

    async def _candidates(self) -> list[uuid.UUID]:
        """Live claims that need reconciling, oldest first.

        A claim qualifies when it has never been reconciled, or when something newer
        arrived in its neighbourhood since it was. The second half is what keeps the
        sweep from being a one-shot pass: a conflict whose other side showed up second
        is only found by looking again.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT c.claim_id FROM memory_claims c "
                        "WHERE c.status = 'staged' AND c.superseded_by IS NULL "
                        "  AND c.subject_entity_id IS NOT NULL "
                        "  AND ("
                        "    c.consolidated_at IS NULL "
                        "    OR EXISTS ("
                        "      SELECT 1 FROM memory_claims n "
                        "      WHERE n.subject_entity_id = c.subject_entity_id "
                        "        AND n.predicate = c.predicate "
                        "        AND n.status = 'staged' "
                        "        AND n.claim_id <> c.claim_id "
                        "        AND n.created_at > c.consolidated_at"
                        "    )"
                        "  ) "
                        "ORDER BY c.created_at "
                        "LIMIT :lim"
                    ),
                    {"lim": self._batch_size},
                )
            ).all()
        return [r.claim_id for r in rows]

    async def _refresh_pending_gauge(self) -> None:
        async with self._session_factory() as session:
            pending = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM memory_claims "
                        "WHERE status = 'staged' AND superseded_by IS NULL "
                        "  AND subject_entity_id IS NOT NULL AND consolidated_at IS NULL"
                    )
                )
            ).scalar_one()
        _PENDING.set(pending)


__all__ = ["DEFAULT_BATCH_SIZE", "ConsolidationSweepWorker", "SweepReport"]
