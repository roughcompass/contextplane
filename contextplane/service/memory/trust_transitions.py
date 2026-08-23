"""Noticing that a claim fell out of a trust class, because nothing else will.

Decay is computed on read. `effective_confidence` takes the stored score, the
time it was scored, and a half-life, and returns what the claim is worth now —
so a claim is `strong` on one read and `moderate` on the next with no code
having run in between. **There is no event.** A record of the crossing therefore
needs something that goes looking, and this is it.

## What it compares against

The bucket the claim was last *seen* in, which is the `to_bucket` of its most
recent transition. A claim with no transitions yet is compared against the
bucket its **stored** score falls in — where it started, before any decay.

That seeding matters: without it the first sweep after a claim is written would
have nothing to compare against and would either record a spurious transition or
skip a real one. With it, the first crossing is caught on the first pass that
sees it.

## Why it is idempotent, and why that is not an accident

Running the sweep twice in a row records nothing the second time, because the
first pass moved the last-seen bucket to where the claim already is. That is the
property that lets the schedule be aggressive without inflating the record — and
it is enforced rather than hoped for: `ck_trust_transition_moved` refuses a row
whose buckets are equal, so a bug that lost the comparison would fail loudly
instead of writing a decay history that never happened.

## Downward only

A claim that regains trust does so because something happened — a confirmation,
a corroborating source, a rescore — and each of those already leaves a record on
the path that owns it. Decay is the only direction with no event behind it, so
it is the only one that needs noticing.

## What it does not cover

`NON_DECAYING_VALUE_TYPES` is `{"prose"}`, so prose claims never decay and never
appear here. The record is not a complete history of trust across the store; it
is a history of trust *lost to time*.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Final

from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.memory.confidence import BUCKET_LOWER_BOUNDS, bucket_for
from contextplane.service.memory.confidence_read import serve
from contextplane.types import Clock

#: Strongest first, so a lower index is a stronger bucket and "fell" is a rank
#: that increased. Derived from the published bounds rather than restated: a
#: bucket added there orders itself here.
_RANK: Final[dict[str, int]] = {name: index for index, (name, _) in enumerate(BUCKET_LOWER_BOUNDS)}

#: How many claims one *transaction* looks at, not how many a pass does. A sweep
#: that scanned the whole store in one transaction would hold a snapshot open for
#: as long as that takes; a sweep that stopped after one batch would examine the
#: same first 500 claim ids forever and never reach the rest, because nothing in
#: the predicate excludes a claim it already looked at. So the batch bounds the
#: transaction and the keyset walks past it.
DEFAULT_BATCH: Final[int] = 500

TRANSITIONS_RECORDED = Counter(
    "contextplane_claim_trust_transitions_total",
    "Claims observed to have fallen out of a trust class, by the class they fell to.",
    ["to_bucket"],
)


@dataclasses.dataclass(frozen=True)
class SweepReport:
    """What one pass looked at and what it found.

    `examined` and `recorded` are both here because their ratio is the useful
    signal: a pass that examines a lot and records nothing is a healthy store,
    and a pass that records most of what it sees means a half-life is wrong.
    """

    examined: int
    recorded: int


def fell(previous: str, current: str) -> bool:
    """Whether `current` is a weaker bucket than `previous`.

    A rank comparison rather than a confidence comparison: two scores inside one
    bucket differ numerically and mean the same thing, and recording that as a
    transition would fill the table with movement no consumer can act on.
    """
    return _RANK[current] > _RANK[previous]


class TrustTransitionSweep:
    """Finds claims that have fallen out of a trust class, and records it once."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        batch: int = DEFAULT_BATCH,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._batch = batch

    async def run_once(self) -> SweepReport:
        """Every eligible claim, in keyset batches. Reports the totals.

        The keyset is what makes this a sweep rather than a sample. Nothing in
        the predicate excludes a claim that has already been examined -- a claim
        with a transition recorded is still eligible for the next one -- so a
        single `LIMIT` with no cursor would re-examine the same first page on
        every pass and never reach anything beyond it.
        """
        now = self._clock.now()
        examined = recorded = 0
        after: uuid.UUID | None = None

        while True:
            page_examined, page_recorded, after = await self._page(now=now, after=after)
            examined += page_examined
            recorded += page_recorded
            if after is None:
                break

        return SweepReport(examined=examined, recorded=recorded)

    async def _page(
        self,
        *,
        now: datetime.datetime,
        after: uuid.UUID | None,
    ) -> tuple[int, int, uuid.UUID | None]:
        """One transaction's worth. Returns the cursor, or None when exhausted."""
        async with self._session_factory() as session, session.begin():
            rows = list(
                (
                    await session.execute(
                        text(
                            "SELECT c.claim_id, c.owning_tenant_id, c.author_tenant_id, c.confidence, "
                            "       c.confidence_scored_at, c.decay_half_life_days, c.value_type, "
                            "       c.confidence_inputs, "
                            "       (SELECT t.to_bucket FROM claim_trust_transitions t "
                            "         WHERE t.claim_id = c.claim_id "
                            "         ORDER BY t.observed_at DESC LIMIT 1) AS last_seen "
                            "  FROM memory_claims c "
                            " WHERE c.confidence IS NOT NULL "
                            "   AND c.confidence_scored_at IS NOT NULL "
                            "   AND c.decay_half_life_days IS NOT NULL "
                            "   AND c.t_invalidated_at IS NULL "
                            "   AND (CAST(:after AS UUID) IS NULL OR c.claim_id > CAST(:after AS UUID)) "
                            " ORDER BY c.claim_id "
                            " LIMIT :batch"
                        ),
                        {"after": after, "batch": self._batch},
                    )
                ).mappings()
            )

            recorded = 0
            for row in rows:
                stored = float(row["confidence"])
                # Seeded from the stored score when the claim has never been
                # seen: that is the bucket it started in, before any decay.
                previous = row["last_seen"] or bucket_for(stored)
                served = serve(
                    stored=stored,
                    scored_at=row["confidence_scored_at"],
                    half_life_days=float(row["decay_half_life_days"]),
                    now=now,
                    value_type=row["value_type"],
                )
                if not fell(previous, served.bucket):
                    continue

                await session.execute(
                    text(
                        "INSERT INTO claim_trust_transitions "
                        "  (transition_id, tenant_id, claim_id, from_bucket, to_bucket, "
                        "   effective_confidence, confidence_inputs, observed_at) "
                        "VALUES (:tid_row, :tenant, :cid, :prev, :now_bucket, "
                        "        CAST(:conf AS NUMERIC), CAST(:inputs AS JSONB), :observed)"
                    ),
                    {
                        "tid_row": uuid.uuid4(),
                        "tenant": row["owning_tenant_id"] or row["author_tenant_id"],
                        "cid": row["claim_id"],
                        "prev": previous,
                        "now_bucket": served.bucket,
                        "conf": served.effective,
                        # Frozen, because the stored inputs are corrected when a
                        # source is re-scored and the question this row answers is
                        # about the moment, not about today's view of it.
                        "inputs": (
                            json.dumps(row["confidence_inputs"], sort_keys=True)
                            if row["confidence_inputs"] is not None
                            else None
                        ),
                        "observed": now,
                    },
                )
                TRANSITIONS_RECORDED.labels(to_bucket=served.bucket).inc()
                recorded += 1

        # A short page is the last one. Exactly-full pages ask again, which costs
        # one empty query at the end and never stops early.
        cursor = rows[-1]["claim_id"] if len(rows) == self._batch else None
        return len(rows), recorded, cursor


async def transitions_for(
    session: AsyncSession,
    *,
    claim_id: uuid.UUID,
) -> list[dict[str, object]]:
    """One claim's decay history, oldest first.

    Oldest first because the question is "how did this lose trust", and a
    sequence read backwards is one a reader has to reverse in their head.
    """
    rows = (
        await session.execute(
            text(
                "SELECT from_bucket, to_bucket, effective_confidence, observed_at "
                "  FROM claim_trust_transitions WHERE claim_id = :cid "
                " ORDER BY observed_at"
            ),
            {"cid": claim_id},
        )
    ).mappings()
    return [
        {
            "from_bucket": row["from_bucket"],
            "to_bucket": row["to_bucket"],
            "effective_confidence": float(row["effective_confidence"]),
            "observed_at": row["observed_at"],
        }
        for row in rows
    ]


def latest_observation(now: datetime.datetime, interval_hours: float) -> datetime.datetime:
    """The earliest instant a crossing recorded at `now` could actually have happened.

    Exposed so a caller reporting a decay history can state the window rather
    than implying an instant: the sweep saw it at `observed_at`, and it happened
    somewhere in the interval before that.
    """
    return now - datetime.timedelta(hours=interval_hours)
