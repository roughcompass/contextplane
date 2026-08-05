"""Turning judged adjudications into calibration mappings, on a schedule.

`ConfirmationService.adjudicate` writes judged outcomes into
`memory_claim_adjudication`, and nothing loads them: `CalibrationService`'s own
`load_observations -> fit -> publish` sequence has no caller anywhere in the
production path, so a deployment can accumulate judged claims forever and never
leave the `uncalibrated` state no matter how many a reviewer has judged. This
worker is that caller, run on an interval instead of by hand.

**Provider and model are a deployment choice, not a per-claim fact.** A claim
never records which provider or model produced its self-reported confidence --
`ClaimService.stage_claim` takes a bare `provider_confidence` float, and a
deployment runs exactly one configured extraction provider and model at a time
(`Settings.extraction_provider`, `Settings.extraction_model`). `strategy_id` is
the one axis that does vary per claim, so the walk below fixes provider and
model to the deployment's current configuration and iterates only over
`strategy_id` -- the same two-part identity `CalibrationService.publish` already
uses to name a mapping version. Recalibrating under whatever is configured now,
rather than trying to reconstruct what produced a claim's self-report in the
past, is exactly the posture the rest of calibration already takes: swapping
either setting is supposed to mean starting over, not silently misattributing
old evidence to a new identity.

**Every triple's outcome is its own,** matching the promotion sweep: one
strategy whose adjudications happen to fail a fit (a division that blows up on
a pathological input, a session that drops mid-query) must not stop the next
strategy from being considered.

**`publish`'s own gates are the only gates.** `CalibrationService.publish`
already refuses to activate a fit below the evaluation-set floor and stores a
failing fit without activating it; this worker calls it and reports what it
decided. It never second-guesses that decision or works around it.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.service.memory.calibration import UNCALIBRATED, CalibrationService, fit
from registry.types import Clock

_log = logging.getLogger(__name__)

# Distinct strategies considered per tick. The number of extraction strategies a
# deployment runs is small and does not grow with claim volume the way the
# promotion sweep's candidate claims do, but a bound is cheap insurance against a
# tick that never returns if that assumption stops holding.
DEFAULT_BATCH_SIZE = 100


@dataclasses.dataclass(frozen=True)
class RefitOutcome:
    """What one triple's refit attempt decided. Returned rather than only
    logged so both the worker and the admin `:refit` route can report it."""

    provider_id: str
    model_id: str
    strategy_id: str
    version: str
    activated: bool
    n_adjudicated: int


@dataclasses.dataclass(frozen=True)
class CalibrationRefitReport:
    """What one tick did, across every triple it considered."""

    considered: int
    activated: int
    stored_failed: int
    below_minimum: int
    failed: int

    @property
    def had_work(self) -> bool:
        return self.considered > 0


async def refit_one(
    calibration: CalibrationService,
    *,
    provider_id: str,
    model_id: str,
    strategy_id: str,
    clock: Clock,
    fitted_by: uuid.UUID | None = None,
) -> RefitOutcome:
    """`load_observations -> fit -> publish` for one triple, and nothing else.

    The one place that sequence is written -- the worker's `run_once` and the
    admin `:refit` route both call this rather than each keeping their own
    copy, so an operator's on-demand refit and the scheduled sweep can never
    quietly diverge in what "refit this triple" means.
    """
    observations = await calibration.load_observations(
        provider_id=provider_id, model_id=model_id, strategy_id=strategy_id
    )
    candidate = fit(observations)
    version, activated = await calibration.publish(
        provider_id=provider_id,
        model_id=model_id,
        strategy_id=strategy_id,
        candidate=candidate,
        fitted_by=fitted_by,
        now=clock.now(),
    )
    return RefitOutcome(
        provider_id=provider_id,
        model_id=model_id,
        strategy_id=strategy_id,
        version=version,
        activated=activated,
        n_adjudicated=candidate.n_adjudicated,
    )


class CalibrationRefitWorker:
    """Discovers strategies with judged outcomes, and refits each in isolation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        calibration: CalibrationService,
        *,
        provider_id: str,
        model_id: str,
        clock: Clock,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._calibration = calibration
        self._provider_id = provider_id
        self._model_id = model_id
        self._clock = clock
        self._batch_size = batch_size

    async def run_once(self) -> CalibrationRefitReport:
        strategy_ids = await self._candidate_strategies()

        if not strategy_ids:
            return CalibrationRefitReport(considered=0, activated=0, stored_failed=0, below_minimum=0, failed=0)

        activated = stored_failed = below_minimum = failed = 0
        for strategy_id in strategy_ids:
            try:
                outcome = await refit_one(
                    self._calibration,
                    provider_id=self._provider_id,
                    model_id=self._model_id,
                    strategy_id=strategy_id,
                    clock=self._clock,
                )
            except Exception:  # noqa: BLE001 - see comment above
                # Logged with the strategy, same reasoning as the promotion
                # sweep's per-claim isolation: a report that only carries a
                # count leaves nobody able to find what actually failed.
                _log.exception("calibration_refit.refit_failed strategy_id=%s", strategy_id)
                failed += 1
                continue

            if outcome.version == UNCALIBRATED:
                below_minimum += 1
            elif outcome.activated:
                activated += 1
            else:
                stored_failed += 1

        report = CalibrationRefitReport(
            considered=len(strategy_ids),
            activated=activated,
            stored_failed=stored_failed,
            below_minimum=below_minimum,
            failed=failed,
        )
        if failed:
            _log.warning(
                "calibration_refit.run considered=%d activated=%d stored_failed=%d below_minimum=%d failed=%d",
                report.considered,
                report.activated,
                report.stored_failed,
                report.below_minimum,
                report.failed,
            )
        return report

    async def _candidate_strategies(self) -> list[str]:
        """Strategies with at least one usable judged outcome right now.

        Mirrors `CalibrationService.load_observations`'s own filter (a real
        provider self-report, a verdict that actually decided something) so a
        strategy whose adjudications are all `undecidable`, or that carry no
        self-report at all, is never handed to `publish` as an empty fit --
        `publish` would still refuse to activate it, but it would also reset
        that triple's status gauge to "uncalibrated" on every tick, which is
        wrong the moment a real mapping is already active for it.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT DISTINCT c.strategy_id "
                        "FROM memory_claim_adjudication a "
                        "JOIN memory_claims c ON c.claim_id = a.claim_id "
                        "WHERE a.provider_confidence IS NOT NULL "
                        "  AND a.verdict IN ('correct', 'incorrect') "
                        "  AND c.strategy_id IS NOT NULL "
                        "ORDER BY c.strategy_id "
                        "LIMIT :lim"
                    ),
                    {"lim": self._batch_size},
                )
            ).all()
        return [r.strategy_id for r in rows]


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "CalibrationRefitReport",
    "CalibrationRefitWorker",
    "RefitOutcome",
    "refit_one",
]
