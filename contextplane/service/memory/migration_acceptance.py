"""Accepting a migrated lot, and recording what it decided about each row.

E12-T4. ADR 0022 decides what `migrated_canonical` commits to; this is the
caller that writes it, and the first caller of `DISPOSITION_BY_POLICY` anywhere
in the tree.

## What this is not

**It is not a policy deciding that unreviewed material is canonical.** That
reading is why the task sat blocked, and reading the mechanisms is what changed
it. `require_minimum_sample` raises unless a *person* has inspected at least
`min_sample` claims in the category, and `inspected_dispositions` -- the count it
is handed -- excludes automated disposals. So this service cannot clear its own
floor by disposing more, and the sample that accepts a lot is always a human one.

What it records is the same outcome across the lot's uninspected remainder, which
is what a lot is.

**It does not write canon.** `record_disposition`'s own docstring binds this:
*"Nothing here writes what the disposition proposes -- the surface that owns the
target does that."* `migrated_canonical` carries `target_kind = canonical_fact`
and asks the promotion surface, exactly as the three proposal dispositions do.

## The count is tenant-wide and the floor is per-category

`inspected_dispositions` counts a tenant's human dispositions since an instant;
`acceptance_for` compares that against one category's floor. Those do not have
the same shape, and this module does not pretend otherwise -- the window is a
caller's argument, because deciding it here would be setting a review policy
nobody asked for. Whether the count should itself be per-category is a real
question about `curation_cases`, which stores an axis rather than a category,
and it is not one an importer gets to answer on the way past.

## The order of operations, and why it is this way

The floor is checked **once, before any case is touched**. Checking per row would
mean a lot that halts halfway leaves some rows disposed and some not -- a
partially accepted lot, which is the one state acceptance sampling has no
vocabulary for. A halt here leaves the queue exactly as it found it.

Each row is then opened, routed to the run's automation principal, and disposed.
Routing is not ceremony: `record_disposition` refuses an unrouted case because a
disposition with no accountable owner is a decision nobody is behind, and the
principal named here is what makes "which policy disposed this" answerable from
the row without a registry of service accounts.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from contextplane.service.memory.curation_cases import (
    DISPOSITION_BY_POLICY,
    DISPOSITION_MIGRATED_CANONICAL,
    CurationCaseService,
)
from contextplane.service.memory.sampling_policy import (
    SamplingPolicyService,
    require_minimum_sample,
)
from contextplane.types import TenantContext


@dataclasses.dataclass(frozen=True)
class MigratedClaim:
    """One row of a lot, named by the axis a curation case is opened on."""

    subject_reference: str
    predicate: str


@dataclasses.dataclass(frozen=True)
class LotAcceptance:
    """What accepting one lot did, for the run record and the audit."""

    claim_category: str
    #: Rows disposed. Not "rows imported" -- a row whose axis already had an open
    #: case is decided on that case, and the count says so.
    disposed: int
    inspected: int
    min_sample: int


class MigrationAcceptanceService:
    """Accept a migrated lot under the tenant's own sampling policy.

    One lot is one claim category within one connector run (ADR 0022, assumption
    2). Two categories in one import are two lots with two floors, because their
    floors were set separately and for different reasons -- a single lot spanning
    both would be accepted on the weaker.
    """

    def __init__(
        self,
        *,
        cases: CurationCaseService,
        sampling: SamplingPolicyService,
    ) -> None:
        self._cases = cases
        self._sampling = sampling

    async def accept_lot(
        self,
        ctx: TenantContext,
        *,
        claim_category: str,
        claims: tuple[MigratedClaim, ...],
        inspection_since: datetime.datetime,
        principal: uuid.UUID,
        now: datetime.datetime,
    ) -> LotAcceptance:
        """Accept `claims` as one lot, or refuse the whole lot.

        Raises `SampleTooSmall` when the category's floor has not been met by
        human inspection, naming the shortfall. That is the intended behaviour
        rather than a rough edge: the operator's remedy is to review more, and an
        import that proceeded anyway would leave a number that still looks like a
        guarantee.
        """
        # Before anything is written. A partially accepted lot is the state
        # acceptance sampling has no vocabulary for.
        #
        # `inspection_since` is a parameter rather than a constant here, and the
        # asymmetry is deliberate: the *floor* is per-category and the *count* is
        # tenant-wide over a window, because that is the shape
        # `inspected_dispositions` has and it is the caller who knows which
        # window of review this lot is being accepted against. A default would be
        # this module inventing a governance fact -- "review in the last N days
        # counts" -- that nobody set and that would silently widen every floor.
        inspected = await self._cases.inspected_dispositions(ctx, since=inspection_since)
        state = await self._sampling.acceptance_for(ctx, claim_category=claim_category, inspected=inspected)
        require_minimum_sample(state)

        owner = str(principal)
        disposed = 0
        for claim in claims:
            case = await self._cases.open_case(
                ctx,
                subject_reference=claim.subject_reference,
                predicate=claim.predicate,
                now=now,
            )
            # Routed to the principal that is about to decide, which is what
            # `record_disposition`'s owner check compares against. Re-routing an
            # already-routed case is permitted and audited, so a lot that
            # overlaps an axis a person was already looking at moves it here
            # visibly rather than silently deciding it under their name.
            await self._cases.route_case(ctx, case_id=case.case_id, owner_id=owner, now=now)
            await self._cases.record_disposition(
                ctx,
                case_id=case.case_id,
                disposition=DISPOSITION_MIGRATED_CANONICAL,
                now=now,
                actor_kind=DISPOSITION_BY_POLICY,
            )
            disposed += 1

        return LotAcceptance(
            claim_category=claim_category,
            disposed=disposed,
            inspected=state.inspected,
            min_sample=state.min_sample,
        )


__all__ = ["LotAcceptance", "MigratedClaim", "MigrationAcceptanceService"]
