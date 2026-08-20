"""Whether a candidate beat the incumbent, or merely looked like it did.

A replay suite gives a pass rate: nineteen of twenty is 0.95. The temptation is
to promote when that number exceeds the incumbent's, and on twenty cases that
is close to a coin flip dressed as a measurement. Nineteen of twenty carries a
95% Wilson interval of roughly 0.75 to 0.99 — the same suite re-drawn could
plausibly have produced fourteen, or twenty. Comparing point estimates promotes
on that noise.

So the rule is: promote when the candidate's **lower confidence bound** clears
the incumbent's observed rate. A candidate genuinely better will clear it once
the suite is large enough to show that; a candidate that merely got a good draw
will not. The cost is real and is the point — on twenty cases almost nothing
clears, which means either accepting slower promotion or investing in a larger
replay suite. That tradeoff is a decision somebody should make deliberately,
and a point-estimate comparison is how it gets made accidentally.

Wilson rather than the textbook normal interval: at the small samples and
extreme rates a replay suite actually produces — nineteen of twenty, twenty of
twenty — the normal approximation gives intervals that run past 1.0 or collapse
to zero width, and a bound wider than the possible range is not a bound.

Nothing consumes this yet. It ships before its caller so the promotion path
cannot later reach for a point-estimate comparison on the grounds that the
bound was never built.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Final

__all__ = ["PromotionVerdict", "wilson_lower_bound", "clears_incumbent"]

#: z for a two-sided 95% interval. Named rather than inlined so the confidence
#: level is a thing somebody can find and argue with.
_Z_95: Final = 1.959963984540054


def wilson_lower_bound(successes: int, trials: int, *, z: float = _Z_95) -> float:
    """The lower end of the Wilson score interval for *successes* of *trials*.

    Zero trials returns 0.0: no evidence is not weak evidence of success, and
    returning anything higher would let an empty suite clear a bar.
    """
    if trials <= 0:
        return 0.0
    if successes < 0 or successes > trials:
        msg = f"successes must lie in [0, {trials}], got {successes}"
        raise ValueError(msg)

    phat = successes / trials
    denominator = 1.0 + (z * z) / trials
    centre = phat + (z * z) / (2 * trials)
    margin = z * math.sqrt((phat * (1.0 - phat) + (z * z) / (4 * trials)) / trials)
    return max(0.0, (centre - margin) / denominator)


@dataclasses.dataclass(frozen=True)
class PromotionVerdict:
    """Why a candidate was or was not promoted, in terms a reviewer can check."""

    promote: bool
    candidate_rate: float
    candidate_lower_bound: float
    incumbent_rate: float
    reason: str


def clears_incumbent(
    *,
    candidate_successes: int,
    candidate_trials: int,
    incumbent_successes: int,
    incumbent_trials: int,
    z: float = _Z_95,
) -> PromotionVerdict:
    """Promote only when the candidate's lower bound clears the incumbent's rate.

    The comparison is deliberately asymmetric — a bound against a point
    estimate, not bound against bound. Requiring the candidate's lower bound to
    clear the incumbent's *lower* bound would let a candidate win by having been
    measured on fewer cases, since a smaller suite gives a lower bound that is
    easier to exceed. The incumbent's observed rate is the thing already being
    relied on, so it is the thing to beat.
    """
    candidate_rate = candidate_successes / candidate_trials if candidate_trials else 0.0
    incumbent_rate = incumbent_successes / incumbent_trials if incumbent_trials else 0.0
    lower = wilson_lower_bound(candidate_successes, candidate_trials, z=z)

    if candidate_trials <= 0:
        reason = "no candidate trials; an unmeasured candidate does not promote"
    elif lower > incumbent_rate:
        reason = (
            f"candidate lower bound {lower:.3f} clears incumbent rate {incumbent_rate:.3f} "
            f"over {candidate_trials} cases"
        )
    else:
        reason = (
            f"candidate rate {candidate_rate:.3f} over {candidate_trials} cases has lower bound "
            f"{lower:.3f}, which does not clear incumbent rate {incumbent_rate:.3f}; "
            "a larger replay suite would narrow the interval"
        )

    return PromotionVerdict(
        promote=candidate_trials > 0 and lower > incumbent_rate,
        candidate_rate=candidate_rate,
        candidate_lower_bound=lower,
        incumbent_rate=incumbent_rate,
        reason=reason,
    )
