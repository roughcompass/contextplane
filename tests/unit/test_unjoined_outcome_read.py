"""Unit tests for the bounded read over outcomes that bound but never joined.

**What this tier can and cannot prove, stated up front.** The read's exclusions
live in SQL, so a faked session cannot demonstrate a row being filtered out --
the fake returns whatever it is handed regardless of the statement. Asserting
"a young outcome is absent" against a mock would therefore pass with the age
predicate deleted, which is worse than no test: it reads as coverage of the
boundary while pinning nothing about it.

So the boundaries are split by what actually holds them:

- The *instant* the age window is computed from is Python, and it is asserted
  here exactly -- `now - older_than`, bound as a parameter, off by nothing.
- The *predicates* that do the excluding are asserted here structurally (the
  statement filters on that instant, restricts to the outcome subject type, and
  excludes by `NOT EXISTS` over a receipt binding on the same reference), which
  pins the shape against silent removal.
- The *semantics* of both exclusions -- a young outcome is not reported, a
  joined outcome is never reported however old -- are proven against real SQL
  in `tests/integration/test_control_plane_availability.py`, because that is
  the only place they can fail honestly.
- Empty versus failed is pure Python and is proven here: an empty result is a
  page, and a failing query raises rather than becoming one.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from contextplane.signals.reads import (
    SUBJECT_OUTCOME,
    SUBJECT_RECEIPT,
    UnjoinedOutcomeReadService,
)
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 8, 11, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_CTX = TenantContext(tenant_id=_TENANT, actor_id=uuid.uuid4(), roles=["operator"])


@dataclasses.dataclass(frozen=True)
class _Row:
    """A row with attribute access, which is how the read projects it."""

    signal_id: uuid.UUID
    reference_id: uuid.UUID
    kind: str
    external_id: str
    bound_at: datetime.datetime


class _FakeResult:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def all(self) -> list[_Row]:
        return self._rows


def _service(
    rows: list[_Row] | None = None, *, error: Exception | None = None
) -> tuple[UnjoinedOutcomeReadService, AsyncMock]:
    """The service over a faked session, returning *rows* or raising *error*."""
    session = AsyncMock()
    if error is not None:
        session.execute = AsyncMock(side_effect=error)
    else:
        session.execute = AsyncMock(return_value=_FakeResult(rows or []))

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return UnjoinedOutcomeReadService(MagicMock(return_value=session_cm)), session


def _row(*, bound_at: datetime.datetime = _NOW, external_id: str = "wrong-id") -> _Row:
    return _Row(
        signal_id=uuid.uuid4(),
        reference_id=uuid.uuid4(),
        kind="deployment",
        external_id=external_id,
        bound_at=bound_at,
    )


def _statement(session: AsyncMock) -> str:
    return str(session.execute.await_args.args[0])


def _params(session: AsyncMock) -> dict[str, object]:
    params: dict[str, object] = session.execute.await_args.args[1]
    return params


# ---------------------------------------------------------------------------
# the age window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_age_window_is_bound_as_now_minus_the_supplied_age() -> None:
    """The cutoff is arithmetic, and this is the assertion that pins it.

    An inverted sign here would report the newest outcomes instead of the stuck
    ones, and every other assertion in this file would still pass.
    """
    service, session = _service()

    await service.unjoined(_CTX, older_than=datetime.timedelta(hours=6), now=_NOW, bound=50)

    assert _params(session)["bound_before"] == _NOW - datetime.timedelta(hours=6)
    assert _params(session)["tenant"] == _TENANT


@pytest.mark.asyncio
async def test_a_zero_age_reads_every_unjoined_outcome_up_to_the_instant() -> None:
    """Zero is a legitimate age: "everything unjoined right now"."""
    service, session = _service()

    await service.unjoined(_CTX, older_than=datetime.timedelta(0), now=_NOW, bound=10)

    assert _params(session)["bound_before"] == _NOW


@pytest.mark.asyncio
async def test_a_negative_age_is_refused_rather_than_reading_the_future() -> None:
    service, session = _service()

    with pytest.raises(ValueError, match="older_than must not be negative"):
        await service.unjoined(_CTX, older_than=datetime.timedelta(hours=-1), now=_NOW, bound=10)

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_statement_filters_on_the_age_instant_it_was_given() -> None:
    """Structural, and deliberately so -- see this module's docstring."""
    service, session = _service()

    await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=10)

    assert "binding.bound_at < :bound_before" in _statement(session)


# ---------------------------------------------------------------------------
# the joined exclusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_statement_excludes_outcomes_a_receipt_already_cites() -> None:
    """The exclusion is `NOT EXISTS` over a receipt binding on the same reference.

    Pinned structurally because a mock cannot filter. The behaviour itself is
    proven against real SQL in the control-plane availability suite, including
    the case a `LEFT JOIN ... IS NULL` would get wrong: one reference cited by
    two receipts.
    """
    service, session = _service()

    await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=10)

    statement = _statement(session)
    assert "NOT EXISTS" in statement
    assert "receipt_binding.subject_type = :receipt_subject" in statement
    assert "receipt_binding.reference_id = binding.reference_id" in statement
    assert _params(session)["receipt_subject"] == SUBJECT_RECEIPT


@pytest.mark.asyncio
async def test_only_outcome_bindings_are_considered_in_the_first_place() -> None:
    service, session = _service()

    await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=10)

    assert "binding.subject_type = :outcome_subject" in _statement(session)
    assert _params(session)["outcome_subject"] == SUBJECT_OUTCOME


def test_the_two_subject_types_are_not_the_same_spelling() -> None:
    """Guards the pair the statement's whole meaning rests on.

    If these ever became equal, `NOT EXISTS` would exclude every outcome by
    matching its own binding, the read would return nothing, and an operator
    would read that as "no stuck outcomes" rather than as a broken query.
    """
    assert SUBJECT_OUTCOME != SUBJECT_RECEIPT


# ---------------------------------------------------------------------------
# empty is not failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_unjoined_outcomes_is_an_empty_page_not_an_error() -> None:
    service, _ = _service([])

    page = await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=10)

    assert page.items == ()
    assert page.truncated is False


@pytest.mark.asyncio
async def test_a_failed_query_raises_instead_of_reading_as_nothing_stuck() -> None:
    """The distinction the runbook depends on.

    An operator running this during an incident must not be told "clean" by a
    read that could not reach the database. Swallowing the error into an empty
    page is the one failure mode that would make the diagnostic actively
    dangerous, so the error propagates untouched.
    """
    failure = OperationalError("SELECT 1", {}, Exception("connection refused"))
    service, _ = _service(error=failure)

    with pytest.raises(OperationalError):
        await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=10)


# ---------------------------------------------------------------------------
# projection and bounding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reported_outcome_carries_the_work_it_named() -> None:
    """The external id is the operator's repair target, so it is projected."""
    row = _row(bound_at=_NOW - datetime.timedelta(days=2), external_id="deploy-4711")
    service, _ = _service([row])

    page = await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=10)

    assert len(page.items) == 1
    item = page.items[0]
    assert item.signal_id == row.signal_id
    assert item.reference_id == row.reference_id
    assert item.kind == "deployment"
    assert item.external_id == "deploy-4711"
    assert item.bound_at == row.bound_at


@pytest.mark.asyncio
async def test_the_bound_is_requested_one_over_so_truncation_is_detectable() -> None:
    """A bound of N fetches N+1: the extra row is how truncation is known."""
    service, session = _service()

    await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=25)

    assert _params(session)["limit"] == 26


@pytest.mark.asyncio
async def test_an_over_full_read_is_trimmed_and_flagged() -> None:
    rows = [_row() for _ in range(4)]
    service, _ = _service(rows)

    page = await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=3)

    assert len(page.items) == 3
    assert page.truncated is True
    assert [item.signal_id for item in page.items] == [row.signal_id for row in rows[:3]]


@pytest.mark.asyncio
async def test_an_exactly_full_read_is_not_flagged_as_truncated() -> None:
    """The off-by-one that would report truncation on every full page."""
    rows = [_row() for _ in range(3)]
    service, _ = _service(rows)

    page = await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=3)

    assert len(page.items) == 3
    assert page.truncated is False


@pytest.mark.asyncio
async def test_oldest_binding_is_reported_first() -> None:
    """Ordering is the read's own, not the caller's to sort back into shape."""
    service, session = _service()

    await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=10)

    assert "ORDER BY binding.bound_at, binding.subject_id" in _statement(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [0, -1])
async def test_a_bound_below_one_is_refused(bound: int) -> None:
    service, session = _service()

    with pytest.raises(ValueError, match="bound must be at least 1"):
        await service.unjoined(_CTX, older_than=datetime.timedelta(hours=1), now=_NOW, bound=bound)

    session.execute.assert_not_awaited()
