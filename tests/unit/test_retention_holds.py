"""Legal holds: what a paused deletion looks like when there is nowhere to record one.

The shipped store cannot persist a hold, and the interesting behaviour is that it
answers reads and writes *differently* rather than uniformly no-opping. A read has a
truthful answer — with nowhere to place a hold, none exists — while a write that
silently did nothing would leave somebody believing a deletion was paused when the
next sweep will delete the record.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.retention import holds, policies

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SUBJECT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OTHER = uuid.UUID("33333333-3333-3333-3333-333333333333")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)


def _hold(*, review_in_days: int = 30, renewals: int = 0, justification: str | None = None) -> holds.LegalHold:
    return holds.LegalHold(
        hold_id=uuid.uuid4(),
        tenant_id=_TENANT,
        record_class=policies.RECORD_CONTEXT_RECEIPT,
        subject_id=_SUBJECT,
        placed_by="legal-ops",
        reason="litigation hold",
        placed_at=_NOW,
        review_date=_NOW + datetime.timedelta(days=review_in_days),
        renewal_count=renewals,
        renewal_justification=justification,
    )


def test_a_hold_expires_at_its_review_date_rather_than_running_indefinitely() -> None:
    """A hold with no end is a retention policy nobody approved. The review date is
    the end, and passing it makes the hold inactive rather than merely overdue."""
    hold = _hold(review_in_days=30)

    assert hold.is_active(_NOW) is True
    assert hold.is_active(hold.review_date - datetime.timedelta(seconds=1)) is True
    assert hold.is_active(hold.review_date) is False
    assert hold.is_active(hold.review_date + datetime.timedelta(days=1)) is False


def test_the_maximum_hold_period_is_bounded() -> None:
    """Bounded so a hold cannot become permanent retention by omission."""
    assert holds.MAX_HOLD_DAYS == 180


def test_a_renewed_hold_carries_the_justification_that_renewed_it() -> None:
    """A renewal without recorded re-justification is the case the policy forbids, so
    the field exists to be populated and a first placement has none."""
    fresh = _hold()
    assert (fresh.renewal_count, fresh.renewal_justification) == (0, None)

    renewed = _hold(renewals=2, justification="still under discovery")
    assert renewed.renewal_count == 2
    assert renewed.renewal_justification == "still under discovery"


def test_the_shipped_store_answers_reads_truthfully() -> None:
    """Not a refusal: with nowhere to record a hold, "nothing is held" is the correct
    answer, and refusing would stop every expiry sweep in a deployment that has no
    holds to honour."""
    store = holds.NoHoldStorage()

    assert store.active_holds(_TENANT, policies.RECORD_CONTEXT_RECEIPT, [_SUBJECT, _OTHER], now=_NOW) == {}
    assert store.held_overdue(_TENANT, now=_NOW) == ()


def test_the_shipped_store_refuses_writes_loudly() -> None:
    """The other half, and the reason reads and writes differ. A place() that
    silently did nothing would report a paused deletion that is not paused."""
    store = holds.NoHoldStorage()

    with pytest.raises(holds.HoldStorageUnavailable) as placed:
        store.place(_TENANT, policies.RECORD_CONTEXT_RECEIPT, _SUBJECT, placed_by="ops", reason="litigation")
    # The message names what cannot be recorded, so the operator learns why rather
    # than only that it failed.
    assert "placer" in str(placed.value) and "review date" in str(placed.value)

    with pytest.raises(holds.HoldStorageUnavailable) as renewed:
        store.renew(uuid.uuid4(), justification="still needed", approved_by="ops")
    assert "re-justification" in str(renewed.value)


def test_expiry_splits_candidates_into_deletable_and_held() -> None:
    """Held records come back *with* their holds rather than being filtered away: a
    suspended deletion has to be attributable to something, and a sweep that dropped
    them silently would make the paused clock invisible."""

    class _OneHeld:
        """A store that holds exactly one subject, so both sides of the split are real."""

        def active_holds(
            self,
            tenant_id: uuid.UUID,
            record_class: str,
            subject_ids: object,
            *,
            now: datetime.datetime,
        ) -> dict[uuid.UUID, holds.LegalHold]:
            return {_SUBJECT: _hold()}

        def held_overdue(self, tenant_id: uuid.UUID, *, now: datetime.datetime) -> tuple[holds.HeldOverdue, ...]:
            return ()

    deletable, held = holds.partition_by_hold(
        _OneHeld(),  # type: ignore[arg-type]
        _TENANT,
        policies.RECORD_CONTEXT_RECEIPT,
        [_SUBJECT, _OTHER],
        now=_NOW,
    )

    assert deletable == (_OTHER,)
    assert set(held) == {_SUBJECT}
    assert held[_SUBJECT].reason == "litigation hold"


def test_partitioning_no_candidates_asks_the_store_nothing() -> None:
    """An empty sweep must not query, because a store that raises on an empty id list
    would turn "nothing to delete" into an incident."""

    class _Exploding:
        def active_holds(self, *args: object, **kwargs: object) -> dict[uuid.UUID, holds.LegalHold]:
            raise AssertionError("the hold store was consulted for an empty candidate list")

        def held_overdue(self, *args: object, **kwargs: object) -> tuple[holds.HeldOverdue, ...]:
            return ()

    deletable, held = holds.partition_by_hold(
        _Exploding(),  # type: ignore[arg-type]
        _TENANT,
        policies.RECORD_CONTEXT_RECEIPT,
        [],
        now=_NOW,
    )
    assert (deletable, held) == ((), {})


def test_a_held_overdue_record_names_what_is_being_kept_and_why() -> None:
    """The operator report's row. It carries the hold itself, not a flag, because
    "why is this still here" is answered by the placer and the reason."""
    overdue = holds.HeldOverdue(
        record_class=policies.RECORD_CONTEXT_RECEIPT,
        subject_id=_SUBJECT,
        due_at=_NOW - datetime.timedelta(days=5),
        hold=_hold(),
    )

    assert overdue.due_at < _NOW
    assert overdue.hold.placed_by == "legal-ops"
    assert overdue.record_class == policies.RECORD_CONTEXT_RECEIPT
