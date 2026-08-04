"""Finding claims that disagree, and recording the disagreement.

Two claims about one subject, under one single-valued predicate, whose values
cannot both hold over overlapping effective intervals, are a disagreement. The
alternative to detecting it is not neutrality: it is a store that serves two
answers to one question with nothing indicating that it is doing so.

**Detection is mechanical, and only because values are typed.** No model reads a
claim to decide whether it conflicts with another. That is what makes a
disagreement reproducible, re-derivable, and reviewable -- and it is the reason
prose is excluded rather than adjudicated.

**Contesting neither deletes nor picks a winner.** Both claims usually are
well-formed and sincerely asserted; one is out of date, or one source is wrong,
and deciding which needs authority-aware resolution or a person. So a
disagreement lowers confidence, blocks promotion, and surfaces the pair.

**A set-valued predicate never disagrees with itself.** A capability depends on
many things, so two dependency claims are two facts. The sweep reads only
single-valued predicates, and treating a set-valued one as single would make
every claim under it permanently unpromotable -- both values being true, neither
can supersede the other and no reviewer could resolve it.

**Detection runs on the write path, not on read.** A reader asking why a claim
scored as it did needs the answer that was true when it scored, and the
neighbourhood may have changed since. It also means the promotion gate reads a
column rather than performing a query it could forget to perform.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid

from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.service.claim_compare import (
    INCOMPATIBLE,
    intervals_overlap,
    is_near_duplicate,
    values_compatible,
)
from registry.service.global_vocabulary import CARDINALITY_SINGLE

_CONTESTS = Counter(
    "registry_claim_contest_detected_total",
    "Disagreements detected between claims, by predicate.",
    ["predicate"],
)

# How a disagreement ended. Kept rather than deleted: that two sources disagreed
# is a fact about the store's history, and a resolution that erased its own cause
# could not be reviewed afterwards.
RESOLUTION_SUPERSEDED = "superseded"
RESOLUTION_BOTH_RETAINED = "both_retained"
RESOLUTION_DISMISSED = "dismissed"
RESOLUTION_WITHDRAWN = "claim_withdrawn"

RESOLUTIONS = frozenset(
    {
        RESOLUTION_SUPERSEDED,
        RESOLUTION_BOTH_RETAINED,
        RESOLUTION_DISMISSED,
        RESOLUTION_WITHDRAWN,
    }
)

# A subject and predicate with more claims than this is not compared exhaustively.
# The comparison is pairwise, so cost grows with the square; a subject carrying
# hundreds of single-valued claims for one predicate is already pathological and
# the right response is to say so rather than to spend minutes on it.
MAX_NEIGHBOURHOOD = 50


@dataclasses.dataclass(frozen=True)
class Disagreement:
    """One detected pair, as a reviewer needs to see it."""

    lower_claim_id: uuid.UUID
    upper_claim_id: uuid.UUID
    predicate: str
    lower_value: object
    upper_value: object


@dataclasses.dataclass(frozen=True)
class ContestOutcome:
    """What one detection pass found. Returned so callers can assert on it."""

    detected: tuple[Disagreement, ...]
    neighbourhood_size: int
    truncated: bool

    @property
    def is_contested(self) -> bool:
        return bool(self.detected)

    def counterparties(self, claim_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        """The other claim in every detected pair.

        A disagreement lowers both sides, and only one of them is the claim being
        written -- so a caller rescoring after detection needs to know which
        already-stored claims were also affected.
        """
        others = {
            pair.upper_claim_id if pair.lower_claim_id == claim_id else pair.lower_claim_id for pair in self.detected
        }
        others.discard(claim_id)
        return tuple(sorted(others, key=str))


@dataclasses.dataclass(frozen=True)
class _Neighbour:
    claim_id: uuid.UUID
    value: object
    value_type: str
    value_entity_id: uuid.UUID | None
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None


async def detect_for_claim(
    session: AsyncSession,
    *,
    claim_id: uuid.UUID,
    now: datetime.datetime,
) -> ContestOutcome:
    """Compare one claim against its neighbourhood and record what disagrees.

    Runs in the caller's transaction, so a claim and the disagreements it creates
    commit together. A separate transaction could leave a claim staged and
    uncontested when it conflicts with something already stored, and the promotion
    gate reads that flag.
    """
    subject = (
        await session.execute(
            text(
                "SELECT subject_entity_id, predicate, value_jsonb, value_type, "
                "       value_cardinality, value_entity_id, asserted_valid_from, "
                "       asserted_valid_to "
                "FROM lmm_claims WHERE claim_id = :cid AND status = 'staged'"
            ),
            {"cid": claim_id},
        )
    ).one_or_none()

    if subject is None or subject.subject_entity_id is None:
        # Unlinked or absent. A claim with no subject has no neighbourhood, and
        # such a claim is excluded from scoring anyway.
        return ContestOutcome(detected=(), neighbourhood_size=0, truncated=False)

    if subject.value_cardinality != CARDINALITY_SINGLE:
        # A set-valued predicate's differing values are two facts. Comparing them
        # would mark every second dependency contested, and no reviewer could
        # resolve it because both are true.
        return ContestOutcome(detected=(), neighbourhood_size=0, truncated=False)

    rows = (
        await session.execute(
            text(
                "SELECT claim_id, value_jsonb, value_type, value_entity_id, "
                "       asserted_valid_from, asserted_valid_to "
                "FROM lmm_claims "
                "WHERE subject_entity_id = :eid AND predicate = :pred "
                "  AND status = 'staged' AND value_cardinality = 'single' "
                "  AND claim_id <> :cid "
                "ORDER BY asserted_valid_from DESC "
                "LIMIT :lim"
            ),
            {
                "eid": subject.subject_entity_id,
                "pred": subject.predicate,
                "cid": claim_id,
                "lim": MAX_NEIGHBOURHOOD + 1,
            },
        )
    ).all()

    truncated = len(rows) > MAX_NEIGHBOURHOOD
    neighbours = [
        _Neighbour(
            claim_id=r.claim_id,
            value=r.value_jsonb,
            value_type=r.value_type,
            value_entity_id=r.value_entity_id,
            valid_from=r.asserted_valid_from,
            valid_to=r.asserted_valid_to,
        )
        for r in rows[:MAX_NEIGHBOURHOOD]
    ]

    found: list[Disagreement] = []
    for other in neighbours:
        if other.value_type != subject.value_type:
            # Should not happen while a predicate's type is immutable, and if it
            # ever does, two values of different types are not comparable.
            continue
        if not intervals_overlap(
            subject.asserted_valid_from,
            subject.asserted_valid_to,
            other.valid_from,
            other.valid_to,
        ):
            # Successive assertions, not competing ones. A claim ending exactly
            # when another begins is a handover.
            continue

        verdict = values_compatible(
            subject.value_type,
            subject.value_jsonb,
            other.value,
            left_entity_id=str(subject.value_entity_id) if subject.value_entity_id else None,
            right_entity_id=str(other.value_entity_id) if other.value_entity_id else None,
        )
        if verdict != INCOMPATIBLE:
            # Compatible, or a value neither side can read. An unreadable value
            # is a validation gap, and calling it a disagreement would
            # manufacture a contested claim out of a bug.
            continue

        # Two phrasings of one assertion are not a disagreement, and this check has
        # to agree with the one consolidation uses. When it did not, a claim was
        # marked contested here and collapsed as a duplicate moments later -- leaving
        # a survivor permanently flagged as conflicted with something that had said
        # the same thing. Twenty sessions naming one team slightly differently
        # produced seventeen contested claims that no reviewer could resolve.
        near, _ = is_near_duplicate(subject.value_type, subject.value_jsonb, other.value)
        if near:
            continue

        lower, upper = sorted((claim_id, other.claim_id), key=str)
        lower_value = subject.value_jsonb if lower == claim_id else other.value
        upper_value = other.value if lower == claim_id else subject.value_jsonb
        found.append(
            Disagreement(
                lower_claim_id=lower,
                upper_claim_id=upper,
                predicate=subject.predicate,
                lower_value=lower_value,
                upper_value=upper_value,
            )
        )

    for pair in found:
        await _record(session, pair, subject.subject_entity_id, now)
        _CONTESTS.labels(predicate=pair.predicate).inc()

    if found:
        # Both sides of every pair, in one statement. Marking only the new claim
        # would leave the older one looking uncontested while the disagreement
        # row says otherwise, and the promotion gate reads the flag.
        await session.execute(
            text("UPDATE lmm_claims SET is_contested = TRUE " "WHERE claim_id = ANY(:ids)"),
            {"ids": list({pair.lower_claim_id for pair in found} | {pair.upper_claim_id for pair in found})},
        )

    return ContestOutcome(
        detected=tuple(found),
        neighbourhood_size=len(neighbours),
        truncated=truncated,
    )


async def _record(
    session: AsyncSession,
    pair: Disagreement,
    subject_entity_id: uuid.UUID,
    now: datetime.datetime,
) -> None:
    """Store the pair, or leave an existing row alone.

    Idempotent on the ordered pair, so the sweep revisiting a neighbourhood does
    not create a second row -- which would double the confidence penalty on
    re-detection and make the same disagreement look like two.
    """
    await session.execute(
        text(
            "INSERT INTO lmm_claim_contest "
            "  (lower_claim_id, upper_claim_id, subject_entity_id, predicate, "
            "   lower_value, upper_value, detected_at) "
            "VALUES (:lower, :upper, :eid, :pred, CAST(:lval AS JSONB), "
            "        CAST(:uval AS JSONB), CAST(:now AS TIMESTAMPTZ)) "
            "ON CONFLICT (lower_claim_id, upper_claim_id) DO NOTHING"
        ),
        {
            "lower": pair.lower_claim_id,
            "upper": pair.upper_claim_id,
            "eid": subject_entity_id,
            "pred": pair.predicate,
            "lval": json.dumps(pair.lower_value, sort_keys=True, separators=(",", ":")),
            "uval": json.dumps(pair.upper_value, sort_keys=True, separators=(",", ":")),
            "now": now,
        },
    )


async def resolve(
    session: AsyncSession,
    *,
    contest_id: uuid.UUID,
    resolution: str,
    now: datetime.datetime,
) -> None:
    """Settle a disagreement, and clear the flag on any claim with none left.

    The flag is a cached answer to "does an unresolved row exist for this claim",
    so it has to be recomputed rather than simply cleared: a claim may be in
    several disagreements, and settling one does not settle the others.
    """
    if resolution not in RESOLUTIONS:
        msg = f"unknown resolution {resolution!r}; expected one of {sorted(RESOLUTIONS)}"
        raise ValueError(msg)

    affected = (
        await session.execute(
            text(
                "UPDATE lmm_claim_contest "
                "SET resolved_at = CAST(:now AS TIMESTAMPTZ), resolution = :res "
                "WHERE contest_id = :cid AND resolved_at IS NULL "
                "RETURNING lower_claim_id, upper_claim_id"
            ),
            {"cid": contest_id, "res": resolution, "now": now},
        )
    ).one_or_none()

    if affected is None:
        return

    await session.execute(
        text(
            "UPDATE lmm_claims SET is_contested = FALSE "
            "WHERE claim_id = ANY(:ids) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM lmm_claim_contest c "
            "    WHERE c.resolved_at IS NULL "
            "      AND (c.lower_claim_id = lmm_claims.claim_id "
            "           OR c.upper_claim_id = lmm_claims.claim_id)"
            "  )"
        ),
        {"ids": [affected.lower_claim_id, affected.upper_claim_id]},
    )


async def resolve_contests_for(
    session: AsyncSession,
    *,
    claim_id: uuid.UUID,
    now: datetime.datetime,
    resolution: str = RESOLUTION_SUPERSEDED,
) -> int:
    """Settle every open disagreement involving one claim, and clear the flags.

    Called when a claim is closed: its disagreements are resolved by its closure,
    whatever they were about. The counterparty's flag is recomputed rather than simply
    cleared, because it may be in other disagreements that are still open.

    Returns how many were settled, so a caller can tell a closure that resolved
    something from one that resolved nothing.
    """
    rows = (
        await session.execute(
            text(
                "UPDATE lmm_claim_contest "
                "SET resolved_at = CAST(:now AS TIMESTAMPTZ), resolution = :res "
                "WHERE resolved_at IS NULL "
                "  AND (lower_claim_id = :cid OR upper_claim_id = :cid) "
                "RETURNING lower_claim_id, upper_claim_id"
            ),
            {"cid": claim_id, "res": resolution, "now": now},
        )
    ).all()

    if not rows:
        return 0

    touched = {r.lower_claim_id for r in rows} | {r.upper_claim_id for r in rows}
    await session.execute(
        text(
            "UPDATE lmm_claims SET is_contested = FALSE "
            "WHERE claim_id = ANY(:ids) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM lmm_claim_contest c "
            "    WHERE c.resolved_at IS NULL "
            "      AND (c.lower_claim_id = lmm_claims.claim_id "
            "           OR c.upper_claim_id = lmm_claims.claim_id)"
            "  )"
        ),
        {"ids": list(touched)},
    )
    return len(rows)


__all__ = [
    "MAX_NEIGHBOURHOOD",
    "RESOLUTIONS",
    "RESOLUTION_BOTH_RETAINED",
    "RESOLUTION_DISMISSED",
    "RESOLUTION_SUPERSEDED",
    "RESOLUTION_WITHDRAWN",
    "ContestOutcome",
    "Disagreement",
    "detect_for_claim",
    "resolve",
    "resolve_contests_for",
]
