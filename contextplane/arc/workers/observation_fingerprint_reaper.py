"""ObservationFingerprintReaperWorker -- the ADR 041 Sec.7 retention rule:
delete per-manifest fingerprints thirty days after their cohort's window
closed, keeping only the aggregate counters and signed digests. A row
under legal hold is skipped entirely, every pass, until the hold is
released.

**Why fingerprints outlive the cohort by exactly thirty days, not zero.**
"The appeal window is 30 days from the qualification decision for both
acceptance and failure" -- an appeal filed on day 29 needs the disputed
fingerprints still present to be adjudicated, so they cannot be cleared
the instant a cohort closes. `reap_fingerprints` anchors the retention
clock on `arc_observation_cohorts.closed_at` (when the window's own
denominator became final) rather than the qualification's `computed_at`
or `accepted_at`: a cohort can be recomputed against by more than one
qualification attempt over time, but it closes exactly once, which is
what makes it the one deterministic anchor every attempt against that
cohort shares.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service.queries import observation as queries
from contextplane.types import Clock

_log = logging.getLogger(__name__)

#: ADR 041 Sec.7's own retention window.
RETENTION_WINDOW = datetime.timedelta(days=30)


@dataclasses.dataclass(frozen=True)
class ObservationFingerprintReaperResult:
    reaped: int


class ObservationFingerprintReaperWorker:
    """Clears `arc_observation_results.fingerprint_digests` for every row
    whose cohort closed more than thirty days ago and carries no legal
    hold, one bounded statement per call.

    Parameters
    ----------
    session_factory:
        Async session factory wired to the Postgres database.
    clock:
        Injectable clock -- the boundary this worker enforces is tested at
        the instant the retention window elapses, not against wall time.
    retention_window:
        Overridable for tests; defaults to ADR 041 Sec.7's thirty days.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        retention_window: datetime.timedelta = RETENTION_WINDOW,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._retention_window = retention_window

    async def run_once(self) -> ObservationFingerprintReaperResult:
        now = self._clock.now()
        closed_before = now - self._retention_window
        async with self._session_factory() as session, session.begin():
            reaped = await queries.reap_fingerprints(session, closed_before=closed_before, now=now)
        result = ObservationFingerprintReaperResult(reaped=reaped)
        if result.reaped:
            _log.info("arc_observation_fingerprint_reaper: reaped=%d", result.reaped)
        return result


__all__ = [
    "RETENTION_WINDOW",
    "ObservationFingerprintReaperResult",
    "ObservationFingerprintReaperWorker",
]
