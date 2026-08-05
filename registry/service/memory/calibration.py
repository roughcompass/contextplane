"""Turning a provider's self-reported number into something that predicts.

Providers report confidence on internal scales that are not comparable with each
other and are not probabilities. A mapping is what makes one usable, and it is fitted
from claims a person has judged correct or incorrect -- never assumed.

**There is no mapping yet, and the honest form of that is no mapping at all.** Not an
identity mapping: identity asserts that a model reporting 0.9 is right nine times in
ten, which nobody has checked, and storing that assertion under a version string is
how an unexamined number acquires an authoritative look. Until a fit exists the
self-report is recorded and contributes nothing. Recording it is the point -- a
mapping can only ever be fitted from raw scores paired with judged outcomes, so a
deployment that discards them can never stop being uncalibrated.

**Bins rather than a fitted curve.** A curve assumes a shape relating self-reports to
correctness, and nothing here has measured that shape; assuming one at the outset is a
stronger claim than the evidence supports. A bin's value is a sentence anybody can
check -- of the judged claims whose raw score landed here, this fraction were right --
and that sentence is the audit record. It also makes the model and the accuracy target
the same object, since the target is itself stated over buckets.

**A fit that misses the target is stored and never selected.** A mapping worse than the
bound is worse than no mapping, because it carries a version string that reads as
calibrated.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from collections.abc import Sequence

from prometheus_client import Gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Not a version, and shaped so it cannot be mistaken for one -- every real version is
# colon-delimited. A claim carrying this was scored without any evidence about what
# its provider's numbers are worth.
UNCALIBRATED = "uncalibrated"

# Ten bins across the raw range. The coarsest split that can hold the pile-up at the
# top of the range apart from the band below it, which is where a self-reporting model
# puts most of its output.
CALIBRATION_BIN_COUNT = 10

# How many judged outcomes a bin needs before it is believed as much as the pooled
# rate across all bins. The same threshold the extraction conformance rule already
# uses: a handful of observations is not evidence.
PRIOR_STRENGTH = 20.0

# Below this, no mapping is published. Matches the size of the evaluation set the
# accuracy target is defined over -- a fit on less could not be checked against that
# target even in principle.
MIN_ADJUDICATED_FOR_MAPPING = 200

# The largest average gap between a bucket's confidence and how often claims in it
# turned out correct. A fit exceeding this is not fit to serve.
MAX_CALIBRATION_ERROR = 0.15

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_FAILED = "failed"

# A state, not a count, so a gauge rather than a counter.
_STATUS = Gauge(
    "registry_claim_calibration_status",
    "0 = uncalibrated, 1 = a fit is active and within tolerance, 2 = the last fit failed.",
    ["provider", "model", "strategy"],
)

_ERROR = Gauge(
    "registry_claim_calibration_error",
    "Measured mean absolute gap between bucketed confidence and observed correctness.",
    ["provider", "model", "strategy"],
)

STATUS_CODE_UNCALIBRATED = 0
STATUS_CODE_ACTIVE = 1
STATUS_CODE_FAILED = 2


@dataclasses.dataclass(frozen=True)
class Adjudication:
    """One judged outcome, reduced to what a fit needs."""

    provider_confidence: float
    was_correct: bool


@dataclasses.dataclass(frozen=True)
class MappingStatus:
    """One (provider, model, strategy) triple's calibration state, as an
    operator would want to see it: not every fit ever attempted, its most
    recent one -- active if a mapping is currently selected, otherwise
    whatever it last tried and why that did not stick."""

    provider_id: str
    model_id: str
    strategy_id: str
    version: str
    status: str
    n_adjudicated: int
    measured_error: float
    fitted_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class Fit:
    """A candidate mapping and how well it did."""

    bins: tuple[float, ...]
    pooled_rate: float
    n_adjudicated: int
    measured_error: float

    @property
    def meets_target(self) -> bool:
        return self.measured_error <= MAX_CALIBRATION_ERROR

    @property
    def status(self) -> str:
        return STATUS_ACTIVE if self.meets_target else STATUS_FAILED

    def apply(self, raw: float) -> float:
        """The calibrated probability for a raw self-report."""
        return self.bins[_bin_index(raw)]


def _bin_index(raw: float) -> int:
    clamped = min(1.0, max(0.0, raw))
    return min(CALIBRATION_BIN_COUNT - 1, int(clamped * CALIBRATION_BIN_COUNT))


def mapping_version(*, provider_id: str, model_id: str, strategy_id: str, fit_date: str, n: int) -> str:
    """Identifies a fit by everything that would invalidate it.

    Provider and model are in the key because changing either means the numbers
    being mapped come from somewhere else. That is what makes recalibration
    mechanical rather than a procedure somebody has to remember: a swapped model
    matches no row, and scoring reverts to uncalibrated without anyone acting. The
    count is included so a claim's record shows how much evidence stood behind its
    mapping without looking anything up.
    """
    return f"{provider_id}:{model_id}:{strategy_id}:{fit_date}:{n}"


def fit(observations: Sequence[Adjudication]) -> Fit:
    """Observed correctness per bin, pulled toward the pooled rate.

    Thin bins are pulled toward the pooled rate rather than trusted, which is what
    stops four observations from setting a number.
    """
    if not observations:
        return Fit(
            bins=tuple(0.5 for _ in range(CALIBRATION_BIN_COUNT)),
            pooled_rate=0.5,
            n_adjudicated=0,
            # No evidence is not a passing fit. Reported as the worst possible
            # error so an empty set can never be published.
            measured_error=1.0,
        )

    pooled = sum(1 for o in observations if o.was_correct) / len(observations)

    totals = [0] * CALIBRATION_BIN_COUNT
    hits = [0] * CALIBRATION_BIN_COUNT
    for observation in observations:
        index = _bin_index(observation.provider_confidence)
        totals[index] += 1
        hits[index] += int(observation.was_correct)

    bins = tuple(
        (hits[i] + PRIOR_STRENGTH * pooled) / (totals[i] + PRIOR_STRENGTH) for i in range(CALIBRATION_BIN_COUNT)
    )

    return Fit(
        bins=bins,
        pooled_rate=pooled,
        n_adjudicated=len(observations),
        measured_error=calibration_error(bins, observations),
    )


def calibration_error(bins: Sequence[float], observations: Sequence[Adjudication]) -> float:
    """Mean absolute gap between what a bin predicts and what actually happened.

    Weighted by how many observations landed in each bin, so a bin holding two
    outcomes cannot dominate one holding two hundred.
    """
    totals = [0] * CALIBRATION_BIN_COUNT
    hits = [0] * CALIBRATION_BIN_COUNT
    for observation in observations:
        index = _bin_index(observation.provider_confidence)
        totals[index] += 1
        hits[index] += int(observation.was_correct)

    weighted = 0.0
    seen = 0
    for i in range(CALIBRATION_BIN_COUNT):
        if totals[i] == 0:
            continue
        weighted += totals[i] * abs(bins[i] - hits[i] / totals[i])
        seen += totals[i]
    return weighted / seen if seen else 1.0


class CalibrationService:
    """Reads the active mapping, and publishes or refuses a new fit."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: object) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def active_version(self, *, provider_id: str, model_id: str, strategy_id: str) -> str:
        """The version that should score claims from this provider, or the sentinel.

        Keyed on the model, so swapping it matches nothing and this returns the
        uncalibrated token with no human action required. That is the whole
        mechanism behind "a provider change requires recalibration".
        """
        async with self._session_factory() as session:
            version = (
                await session.execute(
                    text(
                        "SELECT version FROM memory_calibration_mapping "
                        "WHERE provider_id = :p AND model_id = :m AND strategy_id = :s "
                        "  AND status = 'active'"
                    ),
                    {"p": provider_id, "m": model_id, "s": strategy_id},
                )
            ).scalar_one_or_none()

        code = STATUS_CODE_ACTIVE if version else STATUS_CODE_UNCALIBRATED
        _STATUS.labels(provider=provider_id, model=model_id, strategy=strategy_id).set(code)
        return str(version) if version else UNCALIBRATED

    async def load_observations(self, *, provider_id: str, model_id: str, strategy_id: str) -> list[Adjudication]:
        """Judged outcomes usable for a fit.

        Only claims that actually carry a provider self-report, and only verdicts
        that decided something -- an "undecidable" verdict is information about the
        reviewer's certainty, not about the claim, and counting it either way would
        bias the fit.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT a.verdict, a.provider_confidence "
                        "FROM memory_claim_adjudication a "
                        "JOIN memory_claims c ON c.claim_id = a.claim_id "
                        "WHERE a.provider_confidence IS NOT NULL "
                        "  AND a.verdict IN ('correct', 'incorrect') "
                        "  AND c.strategy_id = :s"
                    ),
                    {"s": strategy_id},
                )
            ).all()

        return [
            Adjudication(
                provider_confidence=float(r.provider_confidence),
                was_correct=r.verdict == "correct",
            )
            for r in rows
        ]

    async def publish(
        self,
        *,
        provider_id: str,
        model_id: str,
        strategy_id: str,
        candidate: Fit,
        fitted_by: uuid.UUID | None = None,
        now: datetime.datetime,
    ) -> tuple[str, bool]:
        """Store a fit, and activate it only if it meets the accuracy target.

        Returns the version and whether it became active. A failing fit is still
        stored -- discarding it would leave "why are we still uncalibrated" with no
        answer -- but it is never selected for scoring.
        """
        labels = {"provider": provider_id, "model": model_id, "strategy": strategy_id}
        _ERROR.labels(**labels).set(candidate.measured_error)

        if candidate.n_adjudicated < MIN_ADJUDICATED_FOR_MAPPING:
            # Below the evaluation-set size the target is defined over, so the
            # target cannot be checked even in principle. Nothing is stored,
            # because a row that cannot be evaluated is not a fit.
            _STATUS.labels(**labels).set(STATUS_CODE_UNCALIBRATED)
            return UNCALIBRATED, False

        version = mapping_version(
            provider_id=provider_id,
            model_id=model_id,
            strategy_id=strategy_id,
            fit_date=now.date().isoformat(),
            n=candidate.n_adjudicated,
        )
        status = candidate.status

        async with self._session_factory() as session, session.begin():
            if status == STATUS_ACTIVE:
                # Only one active fit per key, so the previous one steps aside
                # rather than being deleted: a claim scored under it names it, and
                # that name has to keep resolving.
                await session.execute(
                    text(
                        "UPDATE memory_calibration_mapping SET status = 'superseded' "
                        "WHERE provider_id = :p AND model_id = :m AND strategy_id = :s "
                        "  AND status = 'active'"
                    ),
                    {"p": provider_id, "m": model_id, "s": strategy_id},
                )

            await session.execute(
                text(
                    "INSERT INTO memory_calibration_mapping "
                    "  (provider_id, model_id, strategy_id, version, bins, n_adjudicated, "
                    "   measured_error, status, fitted_at, fitted_by) "
                    "VALUES (:p, :m, :s, :v, CAST(:bins AS JSONB), :n, "
                    "        CAST(:err AS NUMERIC), :status, CAST(:now AS TIMESTAMPTZ), :by) "
                    "ON CONFLICT (version) DO NOTHING"
                ),
                {
                    "p": provider_id,
                    "m": model_id,
                    "s": strategy_id,
                    "v": version,
                    "bins": json.dumps(list(candidate.bins)),
                    "n": candidate.n_adjudicated,
                    "err": round(candidate.measured_error, 3),
                    "status": status,
                    "now": now,
                    "by": fitted_by,
                },
            )

        _STATUS.labels(**labels).set(STATUS_CODE_ACTIVE if status == STATUS_ACTIVE else STATUS_CODE_FAILED)
        return version, status == STATUS_ACTIVE

    async def active_mappings(self) -> tuple[MappingStatus, ...]:
        """Every triple that has ever been fitted, reduced to its most recent
        attempt.

        Deployment-wide, matching the table's own lack of a tenant column: the
        model being measured is shared, so what an operator needs is not "what
        has this tenant fitted" but "what is fitted at all." One row per
        triple -- the active mapping when there is one, otherwise the most
        recent superseded or failed attempt, via `DISTINCT ON` -- so a triple
        that has been tried and always missed the bound stays visible here
        rather than disappearing the moment nothing about it is active.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT DISTINCT ON (provider_id, model_id, strategy_id) "
                        "       provider_id, model_id, strategy_id, version, status, "
                        "       n_adjudicated, measured_error, fitted_at "
                        "FROM memory_calibration_mapping "
                        "ORDER BY provider_id, model_id, strategy_id, fitted_at DESC"
                    )
                )
            ).all()
        return tuple(
            MappingStatus(
                provider_id=r.provider_id,
                model_id=r.model_id,
                strategy_id=r.strategy_id,
                version=r.version,
                status=r.status,
                n_adjudicated=r.n_adjudicated,
                measured_error=float(r.measured_error),
                fitted_at=r.fitted_at,
            )
            for r in rows
        )

    async def load_active(self, *, provider_id: str, model_id: str, strategy_id: str) -> Fit | None:
        """The active mapping, or None when nothing has been fitted."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT bins, n_adjudicated, measured_error "
                        "FROM memory_calibration_mapping "
                        "WHERE provider_id = :p AND model_id = :m AND strategy_id = :s "
                        "  AND status = 'active'"
                    ),
                    {"p": provider_id, "m": model_id, "s": strategy_id},
                )
            ).one_or_none()

        if row is None:
            return None
        bins = row.bins if isinstance(row.bins, list) else json.loads(row.bins)
        return Fit(
            bins=tuple(float(b) for b in bins),
            pooled_rate=sum(float(b) for b in bins) / len(bins),
            n_adjudicated=row.n_adjudicated,
            measured_error=float(row.measured_error),
        )


__all__ = [
    "CALIBRATION_BIN_COUNT",
    "MAX_CALIBRATION_ERROR",
    "MIN_ADJUDICATED_FOR_MAPPING",
    "PRIOR_STRENGTH",
    "STATUS_ACTIVE",
    "STATUS_FAILED",
    "STATUS_SUPERSEDED",
    "UNCALIBRATED",
    "Adjudication",
    "CalibrationService",
    "Fit",
    "MappingStatus",
    "calibration_error",
    "fit",
    "mapping_version",
]
