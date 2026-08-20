"""Fitting how fast a predicate's claims actually get superseded.

Confidence decay needs a half-life. The shipped model authors one per category —
six numbers, argued for in prose — and ADR 0003 replaced that with a measured one
per predicate. This module is the measurement.

**The fit itself is one line of arithmetic, and the discipline around it is the
rest of the file.** Given a sample of claims and how many were superseded within
an observation window, the exponential half-life that reproduces that rate is
`window · ln 2 / -ln(1 - rate)`. Everything else here exists because that number
is easy to compute and easy to believe for the wrong reason.

**A fit is stored and never selected until somebody has inspected it.** The
assumption ADR 0003 names as most likely to be wrong is that supersession tracks
*churn* — a claim becoming untrue — rather than *correction* — a claim having
been wrong when it was written. Those produce identical bitemporal history and
opposite conclusions. A predicate whose extractions are frequently wrong would be
measured as fast-moving and decayed aggressively, which buries an extraction
defect under a confidence curve where nobody will find it. So `store` writes a
`fitted` row and takes no argument that could do otherwise; only
`record_inspection` can make one selectable, and it requires a finding in
writing.

**Nothing consumes the rates yet.** `confidence_decay` reads them in the task
that follows this one. Fitting ships first so the inspection discipline exists
before there is a number anybody wants to use, which is the only order in which
"stored and never selected" is a rule rather than a preference.

**Below the observation floor there is no rate.** A predicate with three
supersessions has not been measured, and a fitted rate over three observations is
noise wearing a number's clothes. Those predicates carry nothing and fall back to
the category figure, which is what the shipped model already does when it has
nothing better.

**A rate is refused at the extremes rather than clamped.** A predicate where
every claim was superseded within the window has a rate of 1.0, whose half-life
is zero — not "very fast", undefined, because an exponential that reaches zero
never had a half-life. A predicate where none was superseded has a rate of 0.0
and an infinite half-life. Both are answers about the window being wrong for that
predicate rather than about the predicate, and returning a number for either
would state the opposite.
"""

from __future__ import annotations

import dataclasses
import datetime
import math
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

STATUS_FITTED: Final = "fitted"
STATUS_ACTIVE: Final = "active"
STATUS_REJECTED: Final = "rejected"

#: Below this many observed supersessions, no rate is fitted. Deliberately the
#: same shape of rule as `confidence_decay.MIN_CHANGE_OBSERVATIONS`, and larger
#: because this fits a curve rather than picking a multiplier: three points
#: cannot distinguish a slow predicate from a quiet quarter.
MIN_OBSERVED_SUPERSESSIONS: Final = 20

#: The window a fit is measured over. One year, because the slowest category the
#: authored table expects to *move* is ownership at 270 days, and a window shorter
#: than the thing being measured cannot see it. The three categories set at 730
#: days or more are out of reach on purpose -- a decision that was taken and an
#: incident that happened do not stop being true, so a predicate in one of them
#: falls below the floor and keeps its authored figure.
DEFAULT_WINDOW_DAYS: Final = 365


class ChurnFitRefused(ValueError):
    """A fit was asked for over a sample that cannot produce one."""


@dataclasses.dataclass(frozen=True)
class ChurnFit:
    """One predicate's measured supersession rate, before anybody has believed it."""

    predicate: str
    half_life_days: float
    observed_supersessions: int
    sampled_claims: int
    observation_window_days: int

    @property
    def supersession_rate(self) -> float:
        """The share of the sampled claims that were superseded in the window."""
        return self.observed_supersessions / self.sampled_claims


def half_life_from_rate(*, rate: float, window_days: int) -> float:
    """The exponential half-life implied by *rate* of a population turning over.

    Refuses 0.0 and 1.0 rather than returning infinity or zero. Both are
    statements about the window rather than about the predicate: nothing
    superseded means the window is shorter than the predicate's lifetime, and
    everything superseded means it is longer than the whole distribution.
    """
    if window_days <= 0:
        msg = f"an observation window of {window_days} days measures nothing"
        raise ChurnFitRefused(msg)
    if not 0.0 < rate < 1.0:
        msg = (
            f"a supersession rate of {rate} has no half-life; 0 and 1 are facts about the "
            f"{window_days}-day window rather than about the predicate, and a number here would say otherwise"
        )
        raise ChurnFitRefused(msg)
    return window_days * math.log(2) / -math.log(1.0 - rate)


def fit(
    *, predicate: str, sampled_claims: int, observed_supersessions: int, window_days: int = DEFAULT_WINDOW_DAYS
) -> ChurnFit:
    """Fit one predicate, or refuse and say which condition was not met."""
    if sampled_claims <= 0:
        msg = f"{predicate}: no claims in the window, so nothing was measured"
        raise ChurnFitRefused(msg)
    if observed_supersessions > sampled_claims:
        msg = f"{predicate}: {observed_supersessions} supersessions among {sampled_claims} claims is impossible"
        raise ChurnFitRefused(msg)
    if observed_supersessions < MIN_OBSERVED_SUPERSESSIONS:
        msg = (
            f"{predicate}: {observed_supersessions} supersessions is below the floor of "
            f"{MIN_OBSERVED_SUPERSESSIONS}; this predicate carries no rate and falls back to its category"
        )
        raise ChurnFitRefused(msg)

    rate = observed_supersessions / sampled_claims
    return ChurnFit(
        predicate=predicate,
        half_life_days=round(half_life_from_rate(rate=rate, window_days=window_days), 2),
        observed_supersessions=observed_supersessions,
        sampled_claims=sampled_claims,
        observation_window_days=window_days,
    )


async def inspected_half_lives(session: AsyncSession) -> dict[str, float]:
    """Every predicate with an inspected, believed rate, on a caller's session.

    A free function taking the caller's session rather than a service method,
    because the two write paths that need it are already inside a transaction and
    the alternative is a second connection reading a table they may have just
    written. Returns `{}` on every deployment until somebody inspects a fit, and
    `{}` produces exactly the authored-category behaviour that shipped before
    this existed.
    """
    rows = (
        await session.execute(
            text("SELECT predicate, half_life_days FROM memory_predicate_churn WHERE status = :active"),
            {"active": STATUS_ACTIVE},
        )
    ).all()
    return {row.predicate: float(row.half_life_days) for row in rows}


class PredicateChurnService:
    """Measures supersession, stores fits, and refuses to select an uninspected one."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def measure(self, *, now: datetime.datetime, window_days: int = DEFAULT_WINDOW_DAYS) -> list[ChurnFit]:
        """Fit every predicate that clears the floor, from the bitemporal history.

        Counts a claim as superseded when `superseded_by` is set, over claims
        written inside the window. Deliberately not over claims *superseded*
        inside the window: that denominator would exclude the claims that
        survived, which are the entire signal.
        """
        cutoff = now - datetime.timedelta(days=window_days)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT predicate, COUNT(*) AS sampled, "
                        "       COUNT(*) FILTER (WHERE superseded_by IS NOT NULL) AS superseded "
                        "FROM memory_claims WHERE created_at >= :cutoff GROUP BY predicate"
                    ),
                    {"cutoff": cutoff},
                )
            ).all()

        fits: list[ChurnFit] = []
        for row in rows:
            try:
                fits.append(
                    fit(
                        predicate=row.predicate,
                        sampled_claims=int(row.sampled),
                        observed_supersessions=int(row.superseded),
                        window_days=window_days,
                    )
                )
            except ChurnFitRefused:
                # A predicate that cannot be fitted is not an error. It carries no
                # rate and falls back, which is the designed outcome for most
                # predicates on most deployments.
                continue
        return fits

    async def store(self, candidate: ChurnFit, *, now: datetime.datetime) -> uuid.UUID:
        """Write a fit as `fitted`. There is no argument that makes it active."""
        fit_id = uuid.uuid4()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_predicate_churn "
                    "  (fit_id, predicate, half_life_days, observed_supersessions, "
                    "   observation_window_days, status, fitted_at) "
                    "VALUES (:fid, :pred, CAST(:hl AS NUMERIC), :obs, :win, :status, CAST(:now AS TIMESTAMPTZ))"
                ),
                {
                    "fid": fit_id,
                    "pred": candidate.predicate,
                    "hl": candidate.half_life_days,
                    "obs": candidate.observed_supersessions,
                    "win": candidate.observation_window_days,
                    "status": STATUS_FITTED,
                    "now": now,
                },
            )
        return fit_id

    async def record_inspection(
        self,
        *,
        fit_id: uuid.UUID,
        inspected_by: uuid.UUID,
        finding: str,
        reflects_change: bool,
        now: datetime.datetime,
    ) -> str:
        """Make a fit selectable, or record why it never will be.

        `reflects_change` is the question ADR 0003 names: did these supersessions
        happen because the world moved, or because the claims were wrong when
        written? An inspection that answers the second rejects the fit — and the
        rejected row stays, because it is the record of why this predicate has no
        rate, which is otherwise indistinguishable from nobody having looked.
        """
        if not finding.strip():
            msg = "an inspection needs a written finding; a signature on an empty conclusion records nothing"
            raise ChurnFitRefused(msg)

        status = STATUS_ACTIVE if reflects_change else STATUS_REJECTED
        async with self._session_factory() as session, session.begin():
            if status == STATUS_ACTIVE:
                # One live rate per predicate. The previous one steps aside rather
                # than being deleted: a decay computed under it named it.
                await session.execute(
                    text(
                        "UPDATE memory_predicate_churn SET status = :rejected "
                        "WHERE status = :active AND predicate = ("
                        "  SELECT predicate FROM memory_predicate_churn WHERE fit_id = :fid)"
                    ),
                    {"rejected": STATUS_REJECTED, "active": STATUS_ACTIVE, "fid": fit_id},
                )
            await session.execute(
                text(
                    "UPDATE memory_predicate_churn SET status = :status, inspected_by = :who, "
                    "  inspected_at = CAST(:now AS TIMESTAMPTZ), inspection_finding = :finding "
                    "WHERE fit_id = :fid"
                ),
                {"status": status, "who": inspected_by, "now": now, "finding": finding, "fid": fit_id},
            )
        return status

    async def active_half_lives(self) -> dict[str, float]:
        """Every predicate with an inspected, believed rate. Everything else falls back."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text("SELECT predicate, half_life_days FROM memory_predicate_churn WHERE status = :active"),
                    {"active": STATUS_ACTIVE},
                )
            ).all()
        return {row.predicate: float(row.half_life_days) for row in rows}


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MIN_OBSERVED_SUPERSESSIONS",
    "STATUS_ACTIVE",
    "STATUS_FITTED",
    "STATUS_REJECTED",
    "ChurnFit",
    "ChurnFitRefused",
    "PredicateChurnService",
    "inspected_half_lives",
    "fit",
    "half_life_from_rate",
]
