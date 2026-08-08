"""ObservationWindowEvaluatorWorker -- closes every observation cohort
whose window has reached its correct boundary, one bounded pass per call.

**Why this worker exists even though `qualify` closes inline too.**
`QualificationService.compute` runs the identical boundary check
(`qualification.close_due_cohort`) every time a human calls `POST {PV}/
observation/qualify`, so correctness never depends on this worker running
at all. What it adds is promptness: a candidate nobody happens to poll
right at its boundary would otherwise sit with a stale `closed_at IS NULL`
until the next `qualify` call, and -- more importantly -- `GET {PV}/
observation` would keep reporting a window as "still open" past the
instant it should have closed. `close_cohort`'s own compare-and-swap
(`WHERE closed_at IS NULL`) is what makes running this worker *and* an
inline `qualify` call at the same instant safe: whichever gets there
first wins, and the other's write is a no-op, never a double-close.

**Why this worker needs only a session factory and a clock.** `close_due_
cohort`/`sweep_open_cohorts` are module-level functions in `qualification.
py`, not `QualificationService` methods, precisely so this worker never
has to construct the full service's authorization/review-package/shadow/
replay-corpus collaborator graph just to run a boundary check that touches
none of them -- see either function's own docstring.

A window that closes late is a window that admitted something it should
not have -- see `queries/observation.py::record_observation`'s own `NOT
EXISTS` guard for the write-side half of that same property. This worker
is the read-independent half: it does not wait for a request to arrive
before enforcing the boundary.
"""

from __future__ import annotations

import dataclasses
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service.qualification import sweep_open_cohorts
from contextplane.types import Clock

_log = logging.getLogger(__name__)

#: Cohorts checked per pass. Same reasoning as every other bounded worker
#: in this package: bounds one call's work regardless of backlog size.
DEFAULT_LIMIT = 200


@dataclasses.dataclass(frozen=True)
class ObservationWindowEvaluatorResult:
    checked: int
    closed: int


class ObservationWindowEvaluatorWorker:
    """Closes due observation cohorts, one bounded pass per call.

    Parameters
    ----------
    session_factory:
        Async session factory wired to the Postgres database.
    clock:
        Injectable clock -- the boundary this worker enforces is tested at
        the instant the window's deadline (or the seven-day cap) elapses,
        not against wall time.
    limit:
        Maximum open cohorts checked per `run_once()` call.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock, limit: int = DEFAULT_LIMIT
    ) -> None:
        if limit < 1:
            msg = f"limit must be at least 1, got {limit}"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._clock = clock
        self._limit = limit

    async def run_once(self) -> ObservationWindowEvaluatorResult:
        checked, closed = await sweep_open_cohorts(self._session_factory, now=self._clock.now(), limit=self._limit)
        result = ObservationWindowEvaluatorResult(checked=checked, closed=closed)
        if result.checked:
            _log.info("arc_observation_window_evaluator: checked=%d closed=%d", result.checked, result.closed)
        return result


__all__ = [
    "DEFAULT_LIMIT",
    "ObservationWindowEvaluatorResult",
    "ObservationWindowEvaluatorWorker",
]
