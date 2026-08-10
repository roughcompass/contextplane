"""Legal holds: the question every expiry has to ask before it deletes anything.

A hold suspends retention expiry and pauses the erasure-deadline clock for a
named record set. Deleting under one is the compliance defect this whole module
exists to make structurally hard: not "remember to check", but "the sweep cannot
select a record without having asked".

**Holds have storage now.** `PostgresHoldStore` places them, renews them under
the approved escalation, and reports the ones keeping a record past its period. A
hold is a row with its placer, scope, reason and review date; a renewal is a
re-justification row and an approval row at an escalated level, because the policy
admits a renewal only with both and a single row could carry only the latest of
each.

**The consult is awaited, because the store it reaches is a database.** The seam
shipped synchronous, which was answerable only while the only store held nothing;
a Postgres store cannot be, and this codebase has no synchronous database access
anywhere to fall back on. The alternatives were each worse than the `await`: a
second, synchronous connection pool beside the async one; a cached snapshot that
would miss a hold placed since its last refresh, which is a deletion under a live
hold; or pushing the check into each sweep's own SQL, which removes the seam that
makes the consult unmissable. Reads stay batched per record class, so the cost is
one query per page of candidates rather than one per record.

`NoHoldStorage` is still the store for a deployment that has not migrated the
hold tables, and its two behaviours are unchanged:

- it answers "is this record held?" with a truthful *no* — with nowhere to place
  a hold, no hold can exist, so no answer is being guessed;
- it *refuses* every attempt to place or renew one, loudly. An operator who needs
  a hold gets an error naming what is missing, instead of a success that holds
  nothing and a deletion three months later that nobody can account for.

The seam is still the point: it is why turning holds on is a change to the
consult's calling convention and to no call site's *logic*. A version of this
module that had skipped the consult "until holds exist" would need every one of
those paths found and revisited instead, and the ones that were missed would be
invisible.

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

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    async def active_holds(
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

    async def held_overdue(self, tenant_id: uuid.UUID, *, now: datetime.datetime) -> Sequence[HeldOverdue]:
        """Every record a hold is keeping past its retention period."""
        ...


class NoHoldStorage:
    """The shipped store: no hold can exist, and none can be placed.

    Truthful on reads, refusing on writes. See the module docstring for why those
    are two different behaviours rather than one uniform no-op.
    """

    async def active_holds(
        self,
        tenant_id: uuid.UUID,
        record_class: str,
        subject_ids: Iterable[uuid.UUID],
        *,
        now: datetime.datetime,
    ) -> Mapping[uuid.UUID, LegalHold]:
        """No record is held, because no hold can be placed."""
        return {}

    async def held_overdue(self, tenant_id: uuid.UUID, *, now: datetime.datetime) -> Sequence[HeldOverdue]:
        """Nothing is being kept past its period by a hold, for the same reason."""
        return ()

    async def place(
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

    async def renew(self, hold_id: uuid.UUID, *, justification: str, approved_by: str) -> LegalHold:
        """Refuse: a renewal that records no re-justification is the case the policy forbids."""
        msg = "legal holds have no storage in this deployment: a renewal cannot record its re-justification or approval"
        raise HoldStorageUnavailable(msg)


async def partition_by_hold(
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
    held = await store.active_holds(tenant_id, record_class, candidates, now=now)
    deletable = tuple(subject_id for subject_id in candidates if subject_id not in held)
    return deletable, held


#: The approval ladder a renewal has to climb, lowest authority first. A renewal
#: is admitted only by an approver at or above the rung its sequence number calls
#: for, so the second renewal cannot be signed off by whoever signed the first.
#: Ranks are the 1-based positions in this tuple; rank 0 is reserved for "no
#: approval recorded", which is the state a renewal may never be stored in.
APPROVAL_LEVELS: tuple[str, ...] = ("tenant_owner", "operator", "counsel")


class HoldRenewalRefused(RegistryError):
    """Raised when a renewal is attempted without what the policy requires of it.

    Distinct from `HoldStorageUnavailable`: storage exists and is working: this
    renewal is the one the policy forbids. Conflating them would let an operator
    read a refused renewal as an outage and retry it unchanged.
    """


def required_rank(sequence: int) -> int:
    """The lowest approval rank that may sign off renewal number `sequence`.

    Escalating, then capped: each renewal needs one rung higher than the last
    until the ladder runs out, after which every further renewal needs the top.
    Capping rather than refusing outright is deliberate — a hold that genuinely
    must outlive the ladder is a real case, and forcing it to lapse instead would
    make the deletion happen precisely where the stakes are highest. What it may
    not do is get quieter over time.
    """
    return min(max(sequence, 1), len(APPROVAL_LEVELS))


@dataclasses.dataclass(frozen=True)
class HeldRecordSource:
    """Where a held record's own retention deadline is read from.

    Injected rather than imported. `retention` sits below the families whose
    records it holds, so naming their tables in Python here would invert the
    import contract; the composition root supplies this map for the same reason
    it supplies the expiry sweep's per-family minimizers.

    `due_at_sql` is a SQL expression over `t`, the aliased source table, yielding
    the instant that record's retention period ends. An expression rather than a
    column because the two shapes in use are genuinely different: a derivative
    stores its deadline outright, while a signal's is its ingestion time plus the
    policy's duration.
    """

    table: str
    id_column: str
    due_at_sql: str


_ACTIVE_HOLDS_SQL = """
SELECT hold_id, subject_id, placed_by, reason, placed_at, review_date, renewal_count
  FROM legal_holds
 WHERE tenant_id = :tenant
   AND record_class = :record_class
   AND subject_id = ANY(:subject_ids)
   AND review_date > :now
"""

_LATEST_JUSTIFICATION_SQL = """
SELECT justification
  FROM legal_hold_renewals
 WHERE hold_id = :hold
 ORDER BY sequence DESC
 LIMIT 1
"""

_HOLD_BY_ID_SQL = """
SELECT hold_id, tenant_id, record_class, subject_id, placed_by, reason,
       placed_at, review_date, renewal_count
  FROM legal_holds
 WHERE hold_id = :hold
"""

_LAST_APPROVAL_RANK_SQL = """
SELECT a.approval_rank
  FROM legal_hold_renewals AS n
  JOIN legal_hold_approvals AS a ON a.renewal_id = n.renewal_id
 WHERE n.hold_id = :hold
 ORDER BY n.sequence DESC
 LIMIT 1
"""


class PostgresHoldStore:
    """The real store: holds live in `legal_holds` and every consult reads them.

    Reads are batched per record class because that is how the sweep asks. Writes
    are the interesting half:

    **Placing** refuses a review date beyond the approved ceiling before the
    database does. Both checks exist on purpose — the database's is the one that
    binds operator tooling and a psql session, and this one is the one that
    produces an error naming the ceiling rather than a constraint name.

    **Renewing** writes three rows and updates a fourth, and that is the policy
    rendered in storage rather than an implementation detail. The re-justification
    and the approval are separate records by separate parties; a renewal that
    could be stored with either missing is the renewal the policy forbids, so
    neither is a nullable column on the hold. The escalation is checked against
    the *previous* renewal's recorded rank, not against a counter, because the
    counter is derivable from rows that could disagree with it.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sources: Mapping[str, HeldRecordSource],
    ) -> None:
        self._session_factory = session_factory
        self._sources = dict(sources)

    async def active_holds(
        self,
        tenant_id: uuid.UUID,
        record_class: str,
        subject_ids: Iterable[uuid.UUID],
        *,
        now: datetime.datetime,
    ) -> Mapping[uuid.UUID, LegalHold]:
        """The subset of `subject_ids` a live hold covers, by id.

        A hold past its review date is not returned: the review is what keeps a
        hold alive, and treating an unreviewed one as still in force is how a hold
        becomes permanent by inattention.
        """
        candidates = list(subject_ids)
        if not candidates:
            return {}
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(_ACTIVE_HOLDS_SQL).bindparams(bindparam("subject_ids", type_=ARRAY(PG_UUID))),
                    {"tenant": tenant_id, "record_class": record_class, "subject_ids": candidates, "now": now},
                )
            ).all()
            held: dict[uuid.UUID, LegalHold] = {}
            for row in rows:
                subject = uuid.UUID(str(row.subject_id))
                held[subject] = LegalHold(
                    hold_id=uuid.UUID(str(row.hold_id)),
                    tenant_id=tenant_id,
                    record_class=record_class,
                    subject_id=subject,
                    placed_by=row.placed_by,
                    reason=row.reason,
                    placed_at=row.placed_at,
                    review_date=row.review_date,
                    renewal_count=row.renewal_count,
                    renewal_justification=(
                        await self._latest_justification(session, uuid.UUID(str(row.hold_id)))
                        if row.renewal_count
                        else None
                    ),
                )
            return held

    async def _latest_justification(self, session: AsyncSession, hold_id: uuid.UUID) -> str | None:
        result = await session.execute(text(_LATEST_JUSTIFICATION_SQL), {"hold": hold_id})
        return result.scalar_one_or_none()

    async def held_overdue(self, tenant_id: uuid.UUID, *, now: datetime.datetime) -> Sequence[HeldOverdue]:
        """Every record a live hold is keeping past its own retention deadline.

        Computed per record class against the injected sources, because the
        deadline lives in the family's table and not in the hold. A held record
        class with no source is reported rather than skipped: "something is held
        and this store cannot date it" is a state an operator has to see, and
        omitting it would render the same as a clean report.
        """
        overdue: list[HeldOverdue] = []
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT hold_id, record_class, subject_id, placed_by, reason, placed_at,"
                        " review_date, renewal_count FROM legal_holds"
                        " WHERE tenant_id = :tenant AND review_date > :now"
                        " ORDER BY placed_at"
                    ),
                    {"tenant": tenant_id, "now": now},
                )
            ).all()
            for row in rows:
                hold = LegalHold(
                    hold_id=uuid.UUID(str(row.hold_id)),
                    tenant_id=tenant_id,
                    record_class=row.record_class,
                    subject_id=uuid.UUID(str(row.subject_id)),
                    placed_by=row.placed_by,
                    reason=row.reason,
                    placed_at=row.placed_at,
                    review_date=row.review_date,
                    renewal_count=row.renewal_count,
                    renewal_justification=(
                        await self._latest_justification(session, uuid.UUID(str(row.hold_id)))
                        if row.renewal_count
                        else None
                    ),
                )
                due_at = await self._due_at(session, hold, now=now)
                if due_at is not None and due_at <= now:
                    overdue.append(
                        HeldOverdue(
                            record_class=hold.record_class,
                            subject_id=hold.subject_id,
                            due_at=due_at,
                            hold=hold,
                        )
                    )
        return tuple(overdue)

    async def _due_at(
        self, session: AsyncSession, hold: LegalHold, *, now: datetime.datetime
    ) -> datetime.datetime | None:
        """When the held record's retention period ends, or `now` if unknowable.

        An unmapped record class returns `now` rather than `None`, which lands the
        hold in the report. That is the fail-closed direction: a hold this store
        cannot date is exactly the one nobody is tracking.
        """
        source = self._sources.get(hold.record_class)
        if source is None:
            return now
        result = await session.execute(
            text(f"SELECT {source.due_at_sql} AS due_at FROM {source.table} AS t WHERE t.{source.id_column} = :id"),  # noqa: S608 - `sources` is a closed map built at wiring; no value here comes from a request
            {"id": hold.subject_id},
        )
        return result.scalar_one_or_none()

    async def place(
        self,
        tenant_id: uuid.UUID,
        record_class: str,
        subject_id: uuid.UUID,
        *,
        placed_by: str,
        reason: str,
        review_in_days: int = MAX_HOLD_DAYS,
        now: datetime.datetime | None = None,
    ) -> LegalHold:
        """Place a hold, refusing anything past the approved ceiling."""
        if not 0 < review_in_days <= MAX_HOLD_DAYS:
            msg = (
                f"a hold's review date must fall within {MAX_HOLD_DAYS} days of its placement; "
                f"{review_in_days} was requested"
            )
            raise HoldRenewalRefused(msg)
        if not reason.strip():
            msg = "a hold must record why it was placed"
            raise HoldRenewalRefused(msg)

        placed_at = now or datetime.datetime.now(datetime.UTC)
        hold = LegalHold(
            hold_id=uuid.uuid4(),
            tenant_id=tenant_id,
            record_class=record_class,
            subject_id=subject_id,
            placed_by=placed_by,
            reason=reason,
            placed_at=placed_at,
            review_date=placed_at + datetime.timedelta(days=review_in_days),
            renewal_count=0,
            renewal_justification=None,
        )
        async with self._session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO legal_holds (hold_id, tenant_id, record_class, subject_id,"
                    " placed_by, reason, placed_at, review_date, renewal_count)"
                    " VALUES (:hold, :tenant, :record_class, :subject, :placed_by, :reason,"
                    " :placed_at, :review_date, 0)"
                ),
                {
                    "hold": hold.hold_id,
                    "tenant": tenant_id,
                    "record_class": record_class,
                    "subject": subject_id,
                    "placed_by": placed_by,
                    "reason": reason,
                    "placed_at": hold.placed_at,
                    "review_date": hold.review_date,
                },
            )
            await session.commit()
        return hold

    async def renew(
        self,
        hold_id: uuid.UUID,
        *,
        justification: str,
        approved_by: str,
        approval_level: str,
        review_in_days: int = MAX_HOLD_DAYS,
        now: datetime.datetime | None = None,
    ) -> LegalHold:
        """Extend a hold, recording the re-justification and its escalated approval.

        Refuses before it writes anything: a renewal that failed halfway would
        leave a hold extended with no approval behind it, which is the shape the
        policy is guarding against.
        """
        if not justification.strip():
            msg = "a renewal must record why the hold is still legally necessary"
            raise HoldRenewalRefused(msg)
        if approval_level not in APPROVAL_LEVELS:
            msg = f"unknown approval level {approval_level!r}; the ladder is {list(APPROVAL_LEVELS)}"
            raise HoldRenewalRefused(msg)
        if not 0 < review_in_days <= MAX_HOLD_DAYS:
            msg = f"a renewal may extend a hold by at most {MAX_HOLD_DAYS} days; {review_in_days} was requested"
            raise HoldRenewalRefused(msg)

        rank = APPROVAL_LEVELS.index(approval_level) + 1
        renewed_at = now or datetime.datetime.now(datetime.UTC)

        async with self._session_factory() as session:
            row = (await session.execute(text(_HOLD_BY_ID_SQL), {"hold": hold_id})).one_or_none()
            if row is None:
                msg = f"no hold {hold_id} to renew"
                raise HoldRenewalRefused(msg)

            sequence = row.renewal_count + 1
            needed = required_rank(sequence)
            if rank < needed:
                msg = (
                    f"renewal {sequence} of this hold needs approval at {APPROVAL_LEVELS[needed - 1]!r} "
                    f"or above; {approval_level!r} is below it"
                )
                raise HoldRenewalRefused(msg)

            previous = (await session.execute(text(_LAST_APPROVAL_RANK_SQL), {"hold": hold_id})).scalar_one_or_none()
            if previous is not None and rank <= previous and previous < len(APPROVAL_LEVELS):
                msg = (
                    f"a renewal must escalate: the previous one was approved at "
                    f"{APPROVAL_LEVELS[previous - 1]!r} and {approval_level!r} is no higher"
                )
                raise HoldRenewalRefused(msg)

            new_review = renewed_at + datetime.timedelta(days=review_in_days)
            renewal_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO legal_hold_renewals (renewal_id, hold_id, sequence, justification,"
                    " requested_by, previous_review_date, new_review_date, recorded_at)"
                    " VALUES (:renewal, :hold, :sequence, :justification, :requested_by,"
                    " :previous_review, :new_review, :now)"
                ),
                {
                    "renewal": renewal_id,
                    "hold": hold_id,
                    "sequence": sequence,
                    "justification": justification,
                    "requested_by": approved_by,
                    "previous_review": row.review_date,
                    "new_review": new_review,
                    "now": renewed_at,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO legal_hold_approvals (approval_id, renewal_id, approved_by,"
                    " approval_level, approval_rank, approved_at)"
                    " VALUES (:approval, :renewal, :approved_by, :level, :rank, :now)"
                ),
                {
                    "approval": uuid.uuid4(),
                    "renewal": renewal_id,
                    "approved_by": approved_by,
                    "level": approval_level,
                    "rank": rank,
                    "now": renewed_at,
                },
            )
            await session.execute(
                text(
                    "UPDATE legal_holds SET review_date = :review, renewal_count = :count,"
                    " placed_at = :placed WHERE hold_id = :hold"
                ),
                # `placed_at` moves with the renewal so the database's ceiling check
                # measures the extension it just granted rather than the original
                # placement, which every renewal past day 180 would otherwise fail.
                {"review": new_review, "count": sequence, "placed": renewed_at, "hold": hold_id},
            )
            await session.commit()

        return LegalHold(
            hold_id=hold_id,
            tenant_id=uuid.UUID(str(row.tenant_id)),
            record_class=row.record_class,
            subject_id=uuid.UUID(str(row.subject_id)),
            placed_by=row.placed_by,
            reason=row.reason,
            placed_at=renewed_at,
            review_date=new_review,
            renewal_count=sequence,
            renewal_justification=justification,
        )


__all__ = [
    "APPROVAL_LEVELS",
    "MAX_HOLD_DAYS",
    "HeldOverdue",
    "HeldRecordSource",
    "HoldRenewalRefused",
    "HoldStorageUnavailable",
    "HoldStore",
    "LegalHold",
    "NoHoldStorage",
    "PostgresHoldStore",
    "partition_by_hold",
]
