"""A judge's self-report becomes a number that predicts — or says it does not yet.

E24-T6, on ADR 0026 part 3. The mechanism is not re-derived:
`service/memory/calibration.py` already fits bins from judged outcomes, refuses an
identity mapping, stores a fit that misses its bound without selecting it, and
separates populations whose numbers do not mean the same thing. Its arithmetic is
imported rather than copied — a second implementation of a calibration curve is a
second answer to *"is this number trustworthy"*, which is the question the whole
module exists to give one answer to.

**What changes is the separation key, and only that.** There it is
`(provider_id, model_id, strategy_id, scorer_version, tenant)`. Here it is the
pinned tuple `(judge_model_id, rubric_version, prompt_template_hash)`. The
argument transfers exactly: a fit made under one judge model does not describe
another, and ADR 0026 part 4 makes a rubric edit a new population for the same
reason a scorer change is one there.

**The bootstrapping is the point.** A judge's confidence is recorded from the
very first run and contributes nothing until a fit exists. Recording it is what
makes the fit possible at all — *a mapping can only ever be fitted from raw
scores paired with judged outcomes, so a deployment that discards them can never
stop being uncalibrated*. This is also why ADR 0026 rejected "refuse to judge
until calibrated": a rule withholding judgement until a fit exists can never
produce the observations the fit needs.

**Only decided reviews feed it.** `confirmed` and `overruled` say the reviewer
reached a conclusion about the judge; `unsure` says something about the reviewer.
Counting `unsure` either way would bias the fit, which is the reason
`calibration.py` excludes an undecidable adjudication.

**A fit that misses its bound is stored and never selected.** A mapping worse
than the bound is worse than no mapping, because it carries a version string that
reads as calibrated.

**And until one exists, the surface says unproven.** That is the corollary ADR
0026 calls non-negotiable, and it is the reason `active_fit` returns `None`
rather than an identity mapping: identity asserts that a judge reporting 0.9 is
right nine times in ten, which nobody has checked.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import TYPE_CHECKING, Final

from sqlalchemy import text

from contextplane.service.memory.calibration import (
    MAX_CALIBRATION_ERROR,
    MIN_ADJUDICATED_FOR_MAPPING,
    STATUS_ACTIVE,
    STATUS_FAILED,
    UNCALIBRATED,
    Adjudication,
    Fit,
    fit,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import Clock, TenantContext

#: The review verdicts a fit may learn from. `unsure` is deliberately absent:
#: it is information about the reviewer rather than about the judgement, and
#: counting it either way would bias the fit.
DECIDED_REVIEWS: Final[tuple[str, ...]] = ("confirmed", "overruled")


@dataclasses.dataclass(frozen=True)
class PinnedTuple:
    """What a judged verdict was produced under, and what a fit describes.

    A value object rather than three loose strings, because every read and every
    write takes all three and a call that dropped one would silently pool two
    populations — which is the exact failure the separation exists to prevent.
    """

    judge_model_id: str
    rubric_version: str
    prompt_template_hash: str

    def version(self, *, fit_date: str, n: int) -> str:
        """Identifies a fit by everything that would invalidate it.

        The same shape `calibration.mapping_version` uses, for the same reason:
        a changed judge or a changed rubric matches no row, and scoring reverts
        to uncalibrated without anybody having to act. The count is included so a
        verdict's record shows how much evidence stood behind its mapping without
        looking anything up.
        """
        return f"{self.judge_model_id}:{self.rubric_version}:{self.prompt_template_hash[:12]}:{fit_date}:{n}"


@dataclasses.dataclass(frozen=True)
class CalibrationState:
    """One pinned tuple's calibration state, as an evaluator would want it.

    `is_calibrated` is the field a surface reads, and it is derived from whether
    a fit is active rather than from a count. A tuple with 500 observations and
    no fit that met the bound is *not* calibrated, and reporting the count would
    invite the opposite reading.
    """

    pinned: PinnedTuple
    is_calibrated: bool
    version: str
    n_adjudicated: int
    measured_error: float
    status: str
    fitted_at: datetime.datetime | None = None


class JudgeCalibrationService:
    """Fits a judge's confidence against what people said, or reports that it has not."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def active_fit(self, pinned: PinnedTuple) -> Fit | None:
        """The mapping that should be applied to this judge's numbers, or `None`.

        `None` rather than identity, deliberately. Identity asserts that a judge
        reporting 0.9 is right nine times in ten, which nobody has checked, and
        storing that assertion under a version string is how an unexamined number
        acquires an authoritative look.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT bins, n_adjudicated, measured_error "
                        "  FROM evaluation_judge_calibration "
                        " WHERE judge_model_id = :model AND rubric_version = :rubric "
                        "   AND prompt_template_hash = :template AND status = 'active'"
                    ),
                    {
                        "model": pinned.judge_model_id,
                        "rubric": pinned.rubric_version,
                        "template": pinned.prompt_template_hash,
                    },
                )
            ).one_or_none()
        if row is None:
            return None
        bins = row.bins if isinstance(row.bins, list) else json.loads(row.bins)
        return Fit(
            bins=tuple(float(value) for value in bins),
            measured_error=float(row.measured_error),
            n_adjudicated=int(row.n_adjudicated),
            pooled_rate=sum(float(value) for value in bins) / len(bins),
        )

    async def calibrated_tuples(self) -> frozenset[tuple[str, str, str]]:
        """Every pinned tuple with a fit currently selected.

        One read rather than one per judged row: a score pane renders both
        criteria of a simulation and would otherwise ask the same question twice,
        and a panel of three would ask it six times.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT judge_model_id, rubric_version, prompt_template_hash "
                        "  FROM evaluation_judge_calibration WHERE status = 'active'"
                    )
                )
            ).all()
        return frozenset((row.judge_model_id, row.rubric_version, row.prompt_template_hash) for row in rows)

    async def load_observations(self, pinned: PinnedTuple) -> list[Adjudication]:
        """Judged outcomes usable for a fit, across every tenant.

        **Deployment-wide, matching how `calibration.active_mappings` reads.**
        The thing being calibrated is a model's self-report, which is a property
        of the model rather than of a tenant; pooling is what makes a fit
        reachable at all, since 200 decided reviews is a lot for one tenant and
        ordinary across a deployment.

        Only reviews that decided something. An `unsure` review is information
        about the reviewer, and counting it either way would bias the fit.

        The judge is *correct* when the reviewer confirmed it. That is the only
        available ground truth here, and it is worth stating plainly: this
        calibrates the judge against reviewers, not against the world.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT j.confidence, r.verdict "
                        "  FROM evaluation_judgements j "
                        "  JOIN evaluation_judgement_reviews r ON r.judgement_id = j.judgement_id "
                        " WHERE j.judge_model_id = :model AND j.rubric_version = :rubric "
                        "   AND j.prompt_template_hash = :template "
                        "   AND r.verdict IN ('confirmed', 'overruled')"
                    ),
                    {
                        "model": pinned.judge_model_id,
                        "rubric": pinned.rubric_version,
                        "template": pinned.prompt_template_hash,
                    },
                )
            ).all()
        return [
            Adjudication(provider_confidence=float(row.confidence), was_correct=row.verdict == "confirmed")
            for row in rows
        ]

    async def refit(self, pinned: PinnedTuple, *, fitted_by: uuid.UUID | None = None) -> CalibrationState:
        """Fit this tuple from its decided reviews, and publish only if it holds.

        Returns the state either way. A tuple with too few observations, or with a
        fit that missed the bound, is reported as uncalibrated *with its numbers*
        — because "why are we still uncalibrated" is the question an evaluator
        asks next, and an answer of silence sends them looking in the wrong place.
        """
        observations = await self.load_observations(pinned)
        candidate = fit(observations)

        if candidate.n_adjudicated < MIN_ADJUDICATED_FOR_MAPPING:
            # Below the size the bound is defined over, so the bound cannot be
            # checked even in principle. Nothing is stored: a row that cannot be
            # evaluated is not a fit.
            return CalibrationState(
                is_calibrated=False,
                measured_error=candidate.measured_error,
                n_adjudicated=candidate.n_adjudicated,
                pinned=pinned,
                status=UNCALIBRATED,
                version=UNCALIBRATED,
            )

        now = self._clock.now()
        version = pinned.version(fit_date=now.date().isoformat(), n=candidate.n_adjudicated)
        status = STATUS_ACTIVE if candidate.meets_target else STATUS_FAILED

        async with self._session_factory() as session, session.begin():
            if status == STATUS_ACTIVE:
                # Only one active fit per tuple, so the previous one steps aside
                # rather than being deleted: a verdict scored under it names it,
                # and that name has to keep resolving.
                await session.execute(
                    text(
                        "UPDATE evaluation_judge_calibration SET status = 'superseded' "
                        " WHERE judge_model_id = :model AND rubric_version = :rubric "
                        "   AND prompt_template_hash = :template AND status = 'active'"
                    ),
                    {
                        "model": pinned.judge_model_id,
                        "rubric": pinned.rubric_version,
                        "template": pinned.prompt_template_hash,
                    },
                )
            await session.execute(
                text(
                    "INSERT INTO evaluation_judge_calibration "
                    "(judge_model_id, rubric_version, prompt_template_hash, version, bins, "
                    " n_adjudicated, measured_error, status, fitted_at, fitted_by) "
                    "VALUES (:model, :rubric, :template, :version, CAST(:bins AS JSONB), :n, "
                    "        CAST(:err AS NUMERIC), :status, :now, :by) "
                    "ON CONFLICT (version) DO NOTHING"
                ),
                {
                    "bins": json.dumps(list(candidate.bins)),
                    "by": fitted_by,
                    "err": round(candidate.measured_error, 3),
                    "model": pinned.judge_model_id,
                    "n": candidate.n_adjudicated,
                    "now": now,
                    "rubric": pinned.rubric_version,
                    "status": status,
                    "template": pinned.prompt_template_hash,
                    "version": version,
                },
            )

        return CalibrationState(
            fitted_at=now,
            is_calibrated=status == STATUS_ACTIVE,
            measured_error=candidate.measured_error,
            n_adjudicated=candidate.n_adjudicated,
            pinned=pinned,
            status=status,
            version=version,
        )

    async def states(self, ctx: TenantContext) -> tuple[CalibrationState, ...]:
        """Every tuple that has ever been fitted, reduced to one row each.

        The active fit when there is one; otherwise the most recent attempt. So a
        tuple that has been tried and always missed the bound stays visible
        rather than disappearing the moment nothing about it is active — that row
        is the answer to *"why is this judge still unproven"*.

        **`status = 'active'` sorts before `fitted_at`, and that ordering is not
        decoration.** Two fits written in the same instant tie on the timestamp,
        and `DISTINCT ON` would then pick one arbitrarily — which is how a
        superseded row comes to be reported as a tuple's current state. Ordering
        by activity first makes the answer a property of the rows rather than of
        clock resolution.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT DISTINCT ON (judge_model_id, rubric_version, prompt_template_hash) "
                        "       judge_model_id, rubric_version, prompt_template_hash, version, "
                        "       status, n_adjudicated, measured_error, fitted_at "
                        "  FROM evaluation_judge_calibration "
                        " ORDER BY judge_model_id, rubric_version, prompt_template_hash, "
                        "          (status = 'active') DESC, fitted_at DESC"
                    )
                )
            ).all()
        return tuple(
            CalibrationState(
                fitted_at=row.fitted_at,
                is_calibrated=row.status == STATUS_ACTIVE,
                measured_error=float(row.measured_error),
                n_adjudicated=int(row.n_adjudicated),
                pinned=PinnedTuple(
                    judge_model_id=row.judge_model_id,
                    prompt_template_hash=row.prompt_template_hash,
                    rubric_version=row.rubric_version,
                ),
                status=row.status,
                version=row.version,
            )
            for row in rows
        )


__all__ = [
    "DECIDED_REVIEWS",
    "MAX_CALIBRATION_ERROR",
    "MIN_ADJUDICATED_FOR_MAPPING",
    "UNCALIBRATED",
    "CalibrationState",
    "JudgeCalibrationService",
    "PinnedTuple",
]
