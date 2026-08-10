"""The retention sweep: what becomes due, what a hold keeps, and what gets queued.

The sweep is the only thing that enforces a retention period when nobody has asked
for an erasure, which is almost always. Its failure modes are quiet by nature — a
period not enforced looks exactly like a period not yet reached — so the cases
worth pinning are the ones where "nothing happened" is the wrong answer: a due
derivative that was skipped, a held record enqueued anyway, a family reduction
never called.

A fake session keyed on the leading verb, matching the drain's own unit tests. The
assertions are about which records the sweep selected, held back, and queued, and
about the cause it queued them under — not about the SQL text, which the
integration tier pins against a real database.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from contextplane.retention import derivatives, holds, policies
from contextplane.types import TenantContext
from contextplane.workers import retention_expiry as sweep

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)


class _AsyncCM:
    """The `async with session_factory() as session` shape, and nothing more."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Answers the tenant scan and the due-derivative scan from declared batches.

    `due_batches` is a list per tenant rather than one list, because the sweep
    loops until a batch comes back short and a fake that answered the same rows
    forever would spin to the batch ceiling on every test.
    """

    def __init__(
        self,
        tenants: list[uuid.UUID],
        due_batches: dict[uuid.UUID, list[list[uuid.UUID]]],
        enqueued: dict[uuid.UUID, list[int]] | None = None,
    ) -> None:
        self._tenants = tenants
        self._due = {tenant: list(batches) for tenant, batches in due_batches.items()}
        self._enqueued = {tenant: list(counts) for tenant, counts in (enqueued or {}).items()}
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        self.executed.append((sql, params or {}))

        if sql.startswith("SELECT tenant_id FROM tenants"):
            return SimpleNamespace(all=lambda: [(tenant,) for tenant in self._tenants])

        if sql.startswith("SELECT r.derivative_id"):
            tenant = params["tenant"] if params else None
            batches = self._due.get(tenant, [])
            rows = batches.pop(0) if batches else []
            return SimpleNamespace(all=lambda: [(value,) for value in rows])

        if sql.startswith("INSERT INTO derivative_work_outbox"):
            tenant = None
            for seen_sql, seen_params in reversed(self.executed[:-1]):
                if seen_sql.startswith("SELECT r.derivative_id"):
                    tenant = seen_params.get("tenant")
                    break
            counts = self._enqueued.get(tenant)
            # Default: the database accepted every id offered. A test that wants a
            # conflict (the item was already queued for this cause) declares it.
            created = counts.pop(0) if counts else len(params["ids"] if params else [])
            return SimpleNamespace(fetchall=lambda: [(uuid.uuid4(),) for _ in range(created)])

        return SimpleNamespace()

    async def commit(self) -> None:
        self.commits += 1

    def enqueued_params(self) -> list[dict[str, Any]]:
        return [params for sql, params in self.executed if sql.startswith("INSERT INTO derivative_work_outbox")]


class _NoHolds:
    """The shipped store's read behaviour: nothing is ever held."""

    def active_holds(
        self,
        tenant_id: uuid.UUID,
        record_class: str,
        subject_ids: Any,
        *,
        now: datetime.datetime,
    ) -> dict[uuid.UUID, holds.LegalHold]:
        return {}

    def held_overdue(self, tenant_id: uuid.UUID, *, now: datetime.datetime) -> tuple[()]:
        return ()


class _HoldsOne:
    """Holds exactly one subject, so the partition has both sides populated."""

    def __init__(self, held: uuid.UUID) -> None:
        self._held = held
        self.asked: list[tuple[uuid.UUID, str]] = []

    def active_holds(
        self,
        tenant_id: uuid.UUID,
        record_class: str,
        subject_ids: Any,
        *,
        now: datetime.datetime,
    ) -> dict[uuid.UUID, holds.LegalHold]:
        self.asked.append((tenant_id, record_class))
        candidates = list(subject_ids)
        if self._held not in candidates:
            return {}
        return {
            self._held: holds.LegalHold(
                hold_id=uuid.uuid4(),
                tenant_id=tenant_id,
                record_class=record_class,
                subject_id=self._held,
                placed_by="legal@example.test",
                reason="litigation",
                placed_at=_NOW,
                review_date=_NOW + datetime.timedelta(days=30),
                renewal_count=0,
                renewal_justification=None,
            )
        }

    def held_overdue(self, tenant_id: uuid.UUID, *, now: datetime.datetime) -> tuple[()]:
        return ()


class _FixedClock:
    def now(self) -> datetime.datetime:
        return _NOW


def _factory(session: _FakeSession) -> Any:
    return lambda: _AsyncCM(session)


def _worker(session: _FakeSession, hold_store: Any = None, **kwargs: Any) -> sweep.RetentionExpiryWorker:
    return sweep.RetentionExpiryWorker(
        _factory(session),
        hold_store or _NoHolds(),
        clock=_FixedClock(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_a_derivative_past_its_expiry_is_queued_for_deletion() -> None:
    """The whole point: an artefact whose sources are all past retention gets removed.

    Its expiry was computed as the minimum across every source at registration, so
    reaching it means nothing that built this artefact is still retainable.
    """
    due = uuid.uuid4()
    session = _FakeSession([_TENANT], {_TENANT: [[due]]})

    report = await _worker(session, batch_size=10).run_once()

    assert report.enqueued == 1
    assert report.held == 0
    assert session.enqueued_params()[0]["ids"] == [due]


@pytest.mark.asyncio
async def test_the_work_is_queued_as_an_expiry_and_names_no_tombstone() -> None:
    """Expiry is its own cause, and conflating it with erasure would be a lie in the record.

    An erasure names the tombstone that ordered it; an expiry has none to name, and
    the outbox's uniqueness treats NULL tombstones as one cause so a sweep that runs
    twice enqueues once.
    """
    session = _FakeSession([_TENANT], {_TENANT: [[uuid.uuid4()]]})

    await _worker(session, batch_size=10).run_once()

    params = session.enqueued_params()[0]
    assert params["trigger"] == derivatives.TRIGGER_EXPIRY
    assert params["operation"] == derivatives.OPERATION_DELETE
    assert "tombstone_id" not in params


@pytest.mark.asyncio
async def test_a_held_record_is_kept_reported_and_never_queued() -> None:
    """A hold is the one legitimate reason a record outlives its period.

    Reported rather than silently skipped: a suspended deletion nobody can attribute
    to a hold is indistinguishable from one that was simply missed, and the paused
    clock defeats the fail-closed overdue behaviour by design.
    """
    held, free = uuid.uuid4(), uuid.uuid4()
    store = _HoldsOne(held)
    session = _FakeSession([_TENANT], {_TENANT: [[held, free]]})

    report = await _worker(session, store, batch_size=10).run_once()

    assert report.held == 1
    assert report.enqueued == 1
    assert session.enqueued_params()[0]["ids"] == [free]
    # Asked under the derivative class, not the class of whatever source built it.
    assert store.asked == [(_TENANT, policies.RECORD_DERIVATIVE)]


@pytest.mark.asyncio
async def test_a_batch_that_is_entirely_held_queues_nothing_and_still_reports_it() -> None:
    """The case a bare "enqueued=0" would read as "nothing was due"."""
    held = uuid.uuid4()
    session = _FakeSession([_TENANT], {_TENANT: [[held]]})

    report = await _worker(session, _HoldsOne(held), batch_size=10).run_once()

    assert (report.enqueued, report.held) == (0, 1)
    assert session.enqueued_params() == []


@pytest.mark.asyncio
async def test_the_family_reductions_run_per_tenant_on_the_sweeps_own_clock() -> None:
    """The families own their content clocks; the sweep only decides when to ask.

    Injected rather than imported: `signals`, `context` and `workspaces` all sit
    above `workers` in the import contract, so a sweep that named one would invert
    the graph.
    """
    calls: list[tuple[uuid.UUID, datetime.datetime]] = []

    async def reduce(ctx: TenantContext, now: datetime.datetime) -> int:
        calls.append((ctx.tenant_id, now))
        return 3

    session = _FakeSession([_TENANT, _OTHER_TENANT], {})
    worker = _worker(
        session,
        minimizers=(sweep.ExpiryMinimizer(record_class=policies.RECORD_EXTERNAL_SIGNAL, reduce=reduce),),
        batch_size=10,
    )

    report = await worker.run_once()

    assert [tenant for tenant, _ in calls] == [_TENANT, _OTHER_TENANT]
    assert {moment for _, moment in calls} == {_NOW}
    assert report.minimized == 6
    assert report.tenants == 2


@pytest.mark.asyncio
async def test_every_tenant_is_swept_including_ones_with_nothing_due() -> None:
    """Retention is not a property of being actively served.

    A tenant nobody has touched in a year is exactly the tenant whose records are
    most likely to be past their period.
    """
    due = uuid.uuid4()
    session = _FakeSession([_TENANT, _OTHER_TENANT], {_OTHER_TENANT: [[due]]})

    report = await _worker(session, batch_size=10).run_once()

    assert report.tenants == 2
    assert report.enqueued == 1


@pytest.mark.asyncio
async def test_an_empty_sweep_reports_no_work_rather_than_a_failure() -> None:
    """The common case, and it must be cheap and silent."""
    session = _FakeSession([_TENANT], {})

    report = await _worker(session, batch_size=10).run_once()

    assert not report.had_work
    assert (report.enqueued, report.minimized, report.held) == (0, 0, 0)
    assert report.ran_at == _NOW


@pytest.mark.asyncio
async def test_a_full_batch_is_followed_by_another_until_the_tenant_is_drained() -> None:
    """A backlog is cleared over several passes rather than one long transaction."""
    first = [uuid.uuid4(), uuid.uuid4()]
    second = [uuid.uuid4()]
    session = _FakeSession([_TENANT], {_TENANT: [first, second]})

    report = await _worker(session, batch_size=2).run_once()

    assert report.enqueued == 3
    assert [params["ids"] for params in session.enqueued_params()] == [first, second]


@pytest.mark.asyncio
async def test_a_backlog_that_outruns_the_ceiling_says_so_rather_than_looking_current() -> None:
    """`truncated` is the difference between "all clear" and "not keeping up".

    Both read as a finite number of items enqueued, and only one of them is a
    reason to change the schedule.
    """
    batches = [[uuid.uuid4()] for _ in range(sweep.MAX_BATCHES + 2)]
    session = _FakeSession([_TENANT], {_TENANT: batches})

    report = await _worker(session, batch_size=1).run_once()

    assert report.truncated is True
    assert report.enqueued == sweep.MAX_BATCHES


@pytest.mark.asyncio
async def test_work_already_queued_for_this_cause_is_not_counted_twice() -> None:
    """One cause, one item. A sweep that ran twice must cost nothing the second time.

    The uniqueness lives in the schema rather than the caller, because the caller is
    the part that gets retried; here the insert reports that it created nothing and
    the report must agree rather than counting what it offered.
    """
    session = _FakeSession([_TENANT], {_TENANT: [[uuid.uuid4()]]}, enqueued={_TENANT: [0]})

    report = await _worker(session, batch_size=10).run_once()

    assert report.enqueued == 0
    assert session.enqueued_params() != []


@pytest.mark.asyncio
async def test_held_overdue_is_answered_on_the_sweeps_clock() -> None:
    """Read through the worker so the report and the pass agree on `now`.

    Asking the store directly with a different instant would report a hold as
    active that this pass had already treated as lapsed.
    """
    session = _FakeSession([_TENANT], {})

    assert await _worker(session).held_overdue(_TENANT) == ()
