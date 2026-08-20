"""Does a high salience actually predict that a claim gets used?

Salience decides what a system remembers, so the weights behind it are a
governed magnitude with a written reason per term. What they do not have is
evidence that the ordering they impose matches reality, and a weighting nobody
has checked is a reasoned guess wearing a number's clothes.

**The label this uses is weaker than the one salience is for, and says so.** The
question worth answering is whether a claim was later *cited on a succeeding
turn* — retrieved, read, and acted on. That needs a citation-to-outcome join
this service does not have. What it does have is receipts: every resolution
records the claims it served, so "was this claim ever served" is joinable today.
Retrieval is necessary for citation and not sufficient for it, so a claim that
scores well here has cleared a lower bar than the one salience is about. Every
report this module produces carries the label it used, because a reliability
figure whose label is unstated will be read as the stronger one.

**A reliability diagram before a threshold.** The output is retrieval rate per
salience bucket. If the weighting predicts anything, the rate rises with the
bucket; if the curve is flat, salience is ordering claims by something retrieval
does not care about, and no threshold over it would mean anything. Reported
before any consumer reads it, which is the only order in which the number can
still say "do not use this yet".

**Brier score beside the curve, not instead of it.** The score is one number and
one number cannot distinguish a model that is uniformly mediocre from one that is
excellent at the top of the range and useless at the bottom — and for a retention
decision, only the top of the range matters. The curve shows which; the score
makes two curves comparable.

**An empty population is reported as empty.** A fresh deployment has no claims
and no receipts, and a reliability curve over zero observations is not a flat
curve, it is no curve. Returning zeros would put a shape on a table nobody
measured.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Final

#: Ten buckets across `[0, 1]`, matching the confidence calibrator's bin count so
#: two reliability reports in one system are read on the same axis.
BUCKET_COUNT: Final = 10

#: Below this many observations a bucket reports its count and no rate. Same
#: reasoning as everywhere else here: three observations is not a rate, and a
#: bucket showing 1.000 from one retrieval is the most misleading cell a
#: reliability table can contain.
MIN_BUCKET_OBSERVATIONS: Final = 20


@dataclasses.dataclass(frozen=True)
class Observation:
    """One scored claim and whether any resolution ever served it."""

    salience: float
    was_retrieved: bool


@dataclasses.dataclass(frozen=True)
class Bucket:
    """One row of the reliability diagram."""

    lower: float
    upper: float
    observations: int
    retrieved: int

    @property
    def retrieval_rate(self) -> float | None:
        """`None` below the floor: a rate from four observations is noise."""
        if self.observations < MIN_BUCKET_OBSERVATIONS:
            return None
        return self.retrieved / self.observations

    @property
    def midpoint(self) -> float:
        """The salience this bucket's rate is plotted against."""
        return (self.lower + self.upper) / 2


@dataclasses.dataclass(frozen=True)
class Reliability:
    """The whole report: the curve, the score, and what the label was."""

    buckets: tuple[Bucket, ...]
    total_observations: int
    #: What "success" meant. Carried in the value rather than left to the caller
    #: to remember, because a reliability number read against the wrong label is
    #: a stronger claim than the one that was measured.
    label: str

    @property
    def measurable_buckets(self) -> tuple[Bucket, ...]:
        """The buckets that cleared the observation floor, in salience order."""
        return tuple(b for b in self.buckets if b.retrieval_rate is not None)

    @property
    def is_monotone(self) -> bool | None:
        """Whether retrieval rate rises with salience across measurable buckets.

        `None` with fewer than two measurable buckets: one point has no slope,
        and reporting `True` for it would let a deployment with almost no data
        read as a validated weighting.
        """
        rates = [b.retrieval_rate for b in self.measurable_buckets]
        if len(rates) < 2:
            return None
        return all(a <= b for a, b in zip(rates, rates[1:], strict=False))  # type: ignore[operator]

    @property
    def brier_score(self) -> float | None:
        """Mean squared error of salience against the outcome. Lower is better.

        `None` on an empty population rather than 0.0, which is the score of a
        perfect predictor and exactly the wrong answer for having measured
        nothing.
        """
        if not self.total_observations:
            return None
        return self._brier

    _brier: float = 0.0


def measure(observations: Sequence[Observation], *, label: str) -> Reliability:
    """Bucket the observations and score them, or report that there were none."""
    if not label.strip():
        msg = "a reliability report states the label it was measured against; an unlabelled one reads as the strongest"
        raise ValueError(msg)

    edges = [i / BUCKET_COUNT for i in range(BUCKET_COUNT + 1)]
    counts = [0] * BUCKET_COUNT
    hits = [0] * BUCKET_COUNT
    squared_error = 0.0

    for item in observations:
        clamped = min(1.0, max(0.0, item.salience))
        index = min(BUCKET_COUNT - 1, int(clamped * BUCKET_COUNT))
        counts[index] += 1
        hits[index] += int(item.was_retrieved)
        squared_error += (clamped - float(item.was_retrieved)) ** 2

    buckets = tuple(
        Bucket(lower=edges[i], upper=edges[i + 1], observations=counts[i], retrieved=hits[i])
        for i in range(BUCKET_COUNT)
    )
    total = len(observations)
    return Reliability(
        buckets=buckets,
        total_observations=total,
        label=label,
        _brier=(squared_error / total) if total else 0.0,
    )


def render(report: Reliability) -> str:
    """The report as a reader sees it, empty population included."""
    lines = [f"salience reliability — label: {report.label}", f"  observations: {report.total_observations}"]
    if not report.total_observations:
        lines.append("  no scored claims and no receipts to join them against; there is no curve to draw")
        return "\n".join(lines)

    lines.append(f"  {'bucket':<14}{'n':>8}{'retrieved':>11}{'rate':>9}")
    for bucket in report.buckets:
        rate = "n/a" if bucket.retrieval_rate is None else f"{bucket.retrieval_rate:.3f}"
        lines.append(
            f"  [{bucket.lower:.1f},{bucket.upper:.1f}){'':<4}{bucket.observations:>8}{bucket.retrieved:>11}{rate:>9}"
        )
    brier = "n/a" if report.brier_score is None else f"{report.brier_score:.4f}"
    monotone = {True: "yes", False: "no", None: "too few measurable buckets to say"}[report.is_monotone]
    lines.append(f"  brier score: {brier}   rises with salience: {monotone}")
    lines.append("  A rate of n/a is a bucket below the observation floor, not a bucket where nothing was retrieved.")
    return "\n".join(lines)


__all__ = [
    "BUCKET_COUNT",
    "MIN_BUCKET_OBSERVATIONS",
    "Bucket",
    "Observation",
    "Reliability",
    "measure",
    "render",
]
