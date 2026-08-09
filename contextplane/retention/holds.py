"""Legal holds: the question every expiry has to ask before it deletes anything.

A hold suspends retention expiry and pauses the erasure-deadline clock for a
named record set. Deleting under one is the compliance defect this whole module
exists to make structurally hard: not "remember to check", but "the sweep cannot
select a record without having asked".

**There is no hold storage yet, and this module says so rather than pretending.**
Placing a hold needs a table, an audit trail with placer, scope, reason and review
date, and renewal with recorded re-justification — none of which exists. So the
shipped store is `NoHoldStorage`, and it does two things that a silent stub would
not:

- it answers "is this record held?" with a truthful *no* — with nowhere to place
  a hold, no hold can exist, so no answer is being guessed;
- it *refuses* every attempt to place or renew one, loudly. An operator who needs
  a hold gets an error naming what is missing, instead of a success that holds
  nothing and a deletion three months later that nobody can account for.

The seam is the point. Every expiry and erasure path already routes its decision
through `HoldStore`, so introducing the table is a change of implementation and
of nothing above it. A version of this module that skipped the consult "until
holds exist" would need every one of those call sites revisited, and the ones
that were missed would be invisible.

**Held-overdue is a reported state, not a silent one.** A paused clock defeats
the fail-closed overdue behaviour by design, so the records it is defeating it for
have to be visible: `held_overdue` is the operator report, and it exists here so
that the shipped store returning an empty one is a statement ("no holds are
possible") rather than an omission.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from contextplane.exceptions import RegistryError

#: The approved ceiling on a single hold. A hold that outlives it must be renewed
#: with recorded re-justification rather than drifting into permanence, which is
#: what an unbounded hold becomes.
MAX_HOLD_DAYS = 180


class HoldStorageUnavailable(RegistryError):
    """Raised when a hold is placed or renewed and there is nowhere to record it.

    Named for what is missing rather than for the operation that failed, because
    the fix is the same in both cases and it is not a retry.
    """


@dataclasses.dataclass(frozen=True)
class LegalHold:
    """One placed hold, with everything needed to account for it later.

    Every field is required except the renewal justification, which is absent
    exactly on a hold's first placement — a renewal without one is what the
    policy forbids, and making the field optional at construction while requiring
    it on renewal is how that distinction stays expressible.
    """

    hold_id: uuid.UUID
    tenant_id: uuid.UUID
    record_class: str
    subject_id: uuid.UUID
    placed_by: str
    reason: str
    placed_at: datetime.datetime
    review_date: datetime.datetime
    renewal_count: int
    renewal_justification: str | None

    def is_active(self, now: datetime.datetime) -> bool:
        """Whether this hold still suspends expiry at `now`.

        A hold past its review date stops suspending: the review is what keeps it
        alive, and treating an unreviewed hold as indefinite is how a hold becomes
        a way of never deleting anything.
        """
        return now < self.review_date


@dataclasses.dataclass(frozen=True)
class HeldOverdue:
    """A record whose retention period has passed but which a hold is keeping.

    The operator report's row. It names the hold as well as the record, because
    "why is this still here" is the only question worth asking about it.
    """

    record_class: str
    subject_id: uuid.UUID
    due_at: datetime.datetime
    hold: LegalHold


class HoldStore(Protocol):
    """Where the answer to "is this record held?" comes from."""

    def active_holds(
        self,
        tenant_id: uuid.UUID,
        record_class: str,
        subject_ids: Iterable[uuid.UUID],
        *,
        now: datetime.datetime,
    ) -> Mapping[uuid.UUID, LegalHold]:
        """The subset of `subject_ids` under an active hold, by id.

        Batch rather than per-record: an expiry sweep asks about a page of
        candidates at once, and a per-record call would make the consult expensive
        enough that somebody would eventually hoist it out of the loop.
        """
        ...

    def held_overdue(self, tenant_id: uuid.UUID, *, now: datetime.datetime) -> Sequence[HeldOverdue]:
        """Every record a hold is keeping past its retention period."""
        ...


class NoHoldStorage:
    """The shipped store: no hold can exist, and none can be placed.

    Truthful on reads, refusing on writes. See the module docstring for why those
    are two different behaviours rather than one uniform no-op.
    """

    def active_holds(
        self,
        tenant_id: uuid.UUID,
        record_class: str,
        subject_ids: Iterable[uuid.UUID],
        *,
        now: datetime.datetime,
    ) -> Mapping[uuid.UUID, LegalHold]:
        """No record is held, because no hold can be placed."""
        return {}

    def held_overdue(self, tenant_id: uuid.UUID, *, now: datetime.datetime) -> Sequence[HeldOverdue]:
        """Nothing is being kept past its period by a hold, for the same reason."""
        return ()

    def place(
        self,
        tenant_id: uuid.UUID,
        record_class: str,
        subject_id: uuid.UUID,
        *,
        placed_by: str,
        reason: str,
    ) -> LegalHold:
        """Refuse: there is nowhere to record a hold, its placer, or its review date."""
        msg = (
            "legal holds have no storage in this deployment: a hold cannot be placed, "
            "audited with its placer, scope, reason and review date, or renewed with "
            "recorded re-justification"
        )
        raise HoldStorageUnavailable(msg)

    def renew(self, hold_id: uuid.UUID, *, justification: str, approved_by: str) -> LegalHold:
        """Refuse: a renewal that records no re-justification is the case the policy forbids."""
        msg = "legal holds have no storage in this deployment: a renewal cannot record its re-justification or approval"
        raise HoldStorageUnavailable(msg)


def partition_by_hold(
    store: HoldStore,
    tenant_id: uuid.UUID,
    record_class: str,
    candidates: Sequence[uuid.UUID],
    *,
    now: datetime.datetime,
) -> tuple[tuple[uuid.UUID, ...], Mapping[uuid.UUID, LegalHold]]:
    """Split expiry candidates into the deletable ones and the held ones.

    The one function every expiry path calls, so the consult is a step that
    happens rather than a rule each sweep implements. Returns the held records
    with their holds rather than merely excluding them: a sweep that drops them
    silently makes the paused clock invisible, and the whole reason holds are
    reportable is that a suspended deletion has to be attributable to something.
    """
    if not candidates:
        return (), {}
    held = store.active_holds(tenant_id, record_class, candidates, now=now)
    deletable = tuple(subject_id for subject_id in candidates if subject_id not in held)
    return deletable, held


__all__ = [
    "MAX_HOLD_DAYS",
    "HeldOverdue",
    "HoldStorageUnavailable",
    "HoldStore",
    "LegalHold",
    "NoHoldStorage",
    "partition_by_hold",
]
