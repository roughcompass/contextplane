"""Fusing several ranked retrieval arms into one ranking.

Not entity search, and not claim search — the arithmetic both of them rank by.
``search`` runs three arms through this and ``ClaimServingService`` runs two, so
this module is the answer to a question that would otherwise be answered twice:
what does a score mean when one arm found a row and another did not, and what
happens to a weight when an arm fails outright.

It moved out of ``search.py`` when that file crossed the repository's size
ceiling, and the seam was already there to be cut along: ``search.py``'s own
docstring described these functions as public *because* a second consumer exists,
which is a description of a shared primitive sitting inside an implementation
module. The one behavioural rule worth restating here, because both callers
depend on it and neither states it: **an arm that raises is absent and its weight
is redistributed; an arm that returns nothing keeps its weight and contributes
nothing.** Empty is an answer. A retriever that treated "nothing matched" as a
failure would quietly reweight every other arm and report a ranking nobody chose.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

_log = logging.getLogger(__name__)

_T = TypeVar("_T")


def rank_decay_weights(n: int) -> list[float]:
    """Rank-based decay: weight for rank r (0-based) = 1/(r+1).

    Public because claim retrieval fuses through it too. A second
    implementation would drift from this one, and then a caller comparing a
    capability result with a claim result would be comparing numbers produced
    by different arithmetic.

    Takes the arm's row *count*, not the rows or their scores — the decay
    curve depends only on how many ranked positions there are, never on what
    occupies them, so a caller cannot pass the wrong data by passing the
    right length. Returns ``[]`` for ``n <= 0``.
    """
    return [1.0 / (rank + 1) for rank in range(n)]


def redistribute_weights(
    weights: dict[str, float],
    failed_arms: set[str],
) -> dict[str, float]:
    """Return new weights with failed arms removed and remaining scaled to sum=1.

    Public for the same reason as `rank_decay_weights`: how a missing arm is
    handled is part of what a fused score means, so every fusion in the
    product handles it the same way.
    """
    surviving = {arm: w for arm, w in weights.items() if arm not in failed_arms}
    total = sum(surviving.values())
    if total == 0.0:
        return {}
    return {arm: w / total for arm, w in surviving.items()}


@dataclass(frozen=True)
class FusedRow(Generic[_T]):
    """One fused result: the winning arm's row plus its accumulated score.

    ``row`` is whatever the first arm to introduce this key returned. Later
    arms that rank the same key only add to ``score`` and ``arm_scores`` —
    they never replace ``row`` — because the identity of a result does not
    depend on which arm found it first, only on being found.
    """

    row: _T
    score: float
    arm_scores: dict[str, float]


async def fuse_hybrid_arms(
    arms: Mapping[str, Awaitable[Sequence[_T]]],
    weights: Mapping[str, float],
    key: Callable[[_T], Hashable],
) -> tuple[dict[Hashable, FusedRow[_T]], set[str]]:
    """Run N ranked retrieval arms concurrently and fuse them into one ranking.

    This is the orchestration ``search`` runs its three arms through, pulled
    out as a public primitive so a second hybrid ranker can reuse the exact
    arithmetic instead of reimplementing it and drifting from it.

    Parameters
    ----------
    arms:
        One already-invoked awaitable per named arm (a coroutine, a task —
        anything ``asyncio.gather`` accepts). Each arm owns its own per-arm
        over-fetch: fusion re-ranks across arms, so a row an individual arm
        placed fourth can finish first once weights and the other arms'
        contributions are added in, and an arm that only returned the
        caller's final desired count would already have discarded it before
        fusion had a chance to promote it. This function does not truncate;
        callers slice the fused, sorted result to whatever size they need.
    weights:
        Base per-arm weight, expected to sum to 1.0 by convention (not
        enforced — weights that don't sum to 1 produce scores that don't
        either).
    key:
        Extracts the dedup identity from one row of one arm's results. Two
        arms returning a row for the same key contribute additively to that
        key's score.

    Arm failure vs. an empty arm
    -----------------------------
    An arm whose awaitable raises is excluded from fusion and its weight is
    redistributed proportionally across the surviving arms (see
    ``redistribute_weights``) — a missing arm should not silently lower every
    score by omission, or the ranking would look like every result got worse
    rather than like one signal went away. An arm that raises nothing but
    returns an empty list is treated differently: it keeps its weight slot
    and simply contributes nothing, because an empty result is a legitimate
    answer ("nothing matched"), not a failure.

    Returns
    -------
    A ``(fused, failed_arms)`` pair. ``fused`` maps each row's dedup key to
    its winning row, accumulated score, and per-arm score breakdown. Rows are
    unordered; callers sort by ``.fused_rank_score`` themselves so they can apply their
    own tie-break.
    """
    names = list(arms.keys())
    raw_results = await asyncio.gather(*arms.values(), return_exceptions=True)

    arm_rows: dict[str, Sequence[_T]] = {}
    failed_arms: set[str] = set()
    for name, result in zip(names, raw_results, strict=True):
        if isinstance(result, BaseException):
            _log.warning(
                "retrieval arm failed — excluding from fusion",
                extra={"arm": name, "error": str(result)},
            )
            failed_arms.add(name)
        else:
            arm_rows[name] = result

    effective_weights = redistribute_weights(dict(weights), failed_arms)

    fused: dict[Hashable, FusedRow[_T]] = {}
    for arm_name, weight in effective_weights.items():
        rows = arm_rows.get(arm_name, [])
        if not rows:
            continue
        rank_scores = rank_decay_weights(len(rows))
        for rank, row in enumerate(rows):
            row_key = key(row)
            contribution = weight * rank_scores[rank]
            existing = fused.get(row_key)
            if existing is None:
                fused[row_key] = FusedRow(row=row, score=contribution, arm_scores={arm_name: contribution})
            else:
                new_arm_scores = dict(existing.arm_scores)
                new_arm_scores[arm_name] = new_arm_scores.get(arm_name, 0.0) + contribution
                fused[row_key] = FusedRow(
                    row=existing.row,
                    score=existing.score + contribution,
                    arm_scores=new_arm_scores,
                )

    return fused, failed_arms


__all__ = [
    "FusedRow",
    "fuse_hybrid_arms",
    "rank_decay_weights",
    "redistribute_weights",
]
