"""CheckpointExporterWorker -- drains `arc_operational_chain_checkpoints`
`WHERE exported_at IS NULL`, one bounded pass per call.

Matches `SourceStatusRefreshWorker`'s own reasoning for reading its due set
unlocked (`contextplane/arc/workers/source_status_refresh.py`): the actual
mutation (`CheckpointExportService.export_checkpoint`'s own compare-and-
swap) does not need a batch-wide lock held across it, and this batch's
mutation may include a slow external sink round trip per row -- holding
`FOR UPDATE` across that would serialize every other exporter pass, and
every other writer of these rows, behind however long the sink takes to
answer. Two exporter passes racing the same checkpoint is safe: the sink's
own identity-keyed idempotency and `export_checkpoint`'s compare-and-swap
both absorb it into "already exported," never a duplicate.

Startup and this worker's own periodic pass are meant to be the same
operation -- there is no separate startup-only code path here; a fresh
pass right after boot drains whatever is pending exactly the way any other
scheduled pass would.
"""

from __future__ import annotations

import dataclasses
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service.checkpoint_export import (
    CheckpointExportOutcome,
    CheckpointExportService,
    CheckpointIntegrityError,
)
from contextplane.arc.service.queries import operational_chain as queries

_log = logging.getLogger(__name__)

#: Checkpoints claimed per pass. Same reasoning as every other bounded
#: worker in this package: bounds one call's work regardless of backlog
#: size, and the scheduler's interval decides how often the rest gets
#: picked up.
DEFAULT_LIMIT = 200


@dataclasses.dataclass(frozen=True)
class CheckpointExportResult:
    """Outcome of one bounded pass.

    `integrity_failed` counts checkpoints whose export attempt raised
    `CheckpointIntegrityError` -- a local/sink disagreement this pass
    detected but, per the module and service docstrings, never repairs.
    `sink_unavailable` counts checkpoints left pending because no sink is
    configured; that is the safe, expected outcome on every deployment
    today (see `CheckpointExportService`'s own module docstring), not a
    failure.
    """

    due: int
    exported: int
    sink_unavailable: int
    integrity_failed: int


class CheckpointExporterWorker:
    """Drains pending checkpoints, one bounded pass per call.

    Parameters
    ----------
    session_factory:
        Async session factory wired to the Postgres database.
    export_service:
        Where the actual export/verification happens; this worker only
        claims the due set and dispatches to it.
    limit:
        Maximum checkpoints claimed per `run_once()` call.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        export_service: CheckpointExportService,
        *,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        if limit < 1:
            msg = f"limit must be at least 1, got {limit}"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._export_service = export_service
        self._limit = limit

    async def run_once(self) -> CheckpointExportResult:
        """Claim and attempt up to `limit` pending checkpoints, each
        independently. One checkpoint's integrity failure or unavailable
        sink never costs the checkpoints behind it their own attempt."""
        async with self._session_factory() as session:
            due_ids = await queries.select_pending_checkpoints(session, limit=self._limit)

        exported = 0
        sink_unavailable = 0
        integrity_failed = 0
        for checkpoint_id in due_ids:
            try:
                outcome = await self._export_service.export_checkpoint(checkpoint_id)
            except CheckpointIntegrityError as exc:
                _log.warning(
                    "arc_checkpoint_exporter: integrity failure checkpoint_id=%s reason_code=%s: %s",
                    checkpoint_id,
                    exc.reason_code,
                    exc,
                )
                integrity_failed += 1
                continue
            if outcome is CheckpointExportOutcome.EXPORTED:
                exported += 1
            elif outcome is CheckpointExportOutcome.SINK_UNAVAILABLE:
                sink_unavailable += 1
            # ALREADY_EXPORTED: another pass or a retried caller beat this
            # one to it -- not counted as this pass's own work, and not a
            # failure either.

        result = CheckpointExportResult(
            due=len(due_ids), exported=exported, sink_unavailable=sink_unavailable, integrity_failed=integrity_failed
        )
        if result.due:
            _log.info(
                "arc_checkpoint_exporter: due=%d exported=%d sink_unavailable=%d integrity_failed=%d",
                result.due,
                result.exported,
                result.sink_unavailable,
                result.integrity_failed,
            )
        return result


__all__ = [
    "DEFAULT_LIMIT",
    "CheckpointExportResult",
    "CheckpointExporterWorker",
]
