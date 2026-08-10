"""The differencing defence, against a real database and a real erasure.

Every floor in this system can hold perfectly while the disclosure happens. A cell
is published over six producers; one of them is erased; the cell is recomputed over
the five who remain and clears every floor it ever cleared. Both figures are lawful
and subtracting them names the erased producer's exact contribution.

So this file asserts the property no unit test can state, because it lives inside a
single `ON CONFLICT DO UPDATE` that Postgres evaluates against the row it is about
to overwrite:

- **A cell whose recomputed figure disagrees with the published one is withheld** —
  even though the new figure clears every floor, which is exactly the case a
  floors-only writer would publish.
- **Withholding is one-way.** Later passes recompute the same window and must not
  publish it again. A bare re-run of the aggregation job is the forbidden design,
  and the only way to show it is forbidden is to re-run it.
- **One version of a cell, ever.** No predecessor is left anywhere for a reader to
  find, which is what makes the first two properties worth anything.
- **The retraction is per cell, not per tenant.** A window the erasure did not
  touch keeps its figure; a writer that withheld everything after any erasure
  would be safe and useless.
- **An old window is reached at all.** It is outside the trailing pass, so the only
  thing that brings it back is the tombstone ledger — which never records which
  window it touched, because the row that would have said is gone.

Raw inserts for the fixture and the real erasure participant for the erasure. The
seeding is not what is under test; the participant is, because a test that deleted
the rows itself would prove the writer reacts to a delete this file performed
rather than to the erasure a person asked for.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy.exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.retention import policies, tombstones
from contextplane.service.memory.learning_reads import COHORT_TENANT
from contextplane.signals import aggregates
from contextplane.signals.erasure import SignalErasure
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)
_ERASED_AT = _NOW + datetime.timedelta(hours=1)
_AFTER = _NOW + datetime.timedelta(hours=2)

#: Yesterday, whole: the most recent window a pass at `_NOW` may compute.
_RECENT = datetime.datetime(2026, 8, 8, tzinfo=datetime.UTC)
#: Far enough back to sit outside a one-window trailing pass, so the only way to
#: reach it is the erasure-triggered recompute.
_OLD = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
#: Also outside the trailing pass, and untouched by the erasure: the control.
_UNTOUCHED = datetime.datetime(2026, 8, 4, tzinfo=datetime.UTC)

#: Six producers per window clears the five-actor floor with one to spare — which
#: is the point. After erasing one, five remain, and five still clears the floor.
#: A floors-only writer would happily publish the smaller figure.
_PRODUCERS = 6

_KEY_ID = "test-key"
_KEY_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"

_SOURCE_SYSTEM = "github"


def _salts() -> tombstones.KeyedTenantSalt:
    return tombstones.KeyedTenantSalt({_KEY_ID: bytes.fromhex(_KEY_HEX)}, active_key_id=_KEY_ID)


class _Clock:
    def __init__(self, now: datetime.datetime) -> None:
        self._now = now

    def now(self) -> datetime.datetime:
        return self._now


async def _seed_signals(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    producers: list[str],
    ingested_at: datetime.datetime,
) -> None:
    """One signal per producer, stamped into the window `ingested_at` falls in.

    `ingested_at` is the server's own write instant, which is why the writer windows
    on it: a window that has ended can never gain a row, so any later change to its
    figure is a removal.
    """
    for producer in producers:
        await session.execute(
            text(
                "INSERT INTO external_signals (signal_id, tenant_id, source_system, producer_id, "
                "                              producer_type, source_event_id, idempotency_key, "
                "                              content_digest, authority, classification, "
                "                              ingested_at, schema_version, payload) "
                "VALUES (:id, :t, :sys, :producer, 'human', :event, :idem, :digest, "
                "        'observer_extraction', 'internal', :at, 'v1', '{}'::jsonb)"
            ),
            {
                "id": uuid.uuid4(),
                "t": tenant_id,
                "sys": _SOURCE_SYSTEM,
                "producer": producer,
                "event": f"evt-{uuid.uuid4().hex[:12]}",
                "idem": f"idem-{uuid.uuid4().hex[:12]}",
                "digest": uuid.uuid4().hex,
                "at": ingested_at,
            },
        )


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    """A tenant whose producers filed signals across three complete windows."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    # The producer this test erases. Recorded as text on the signal, like every
    # other producer: the ledger names who reported by the originator's own id.
    erased_id = uuid.uuid4()
    others = [str(uuid.uuid4()) for _ in range(_PRODUCERS - 1)]
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'aggregate writer')"),
                {"t": tenant_id, "s": f"pag-{tenant_id.hex[:10]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'aggregate-writer-actor', :sub, :now)"
                ),
                {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}", "now": _NOW},
            )
            # Two windows the erased producer contributed to, and one they did not.
            await _seed_signals(
                session,
                tenant_id=tenant_id,
                producers=[str(erased_id), *others],
                ingested_at=_RECENT + datetime.timedelta(hours=9),
            )
            await _seed_signals(
                session,
                tenant_id=tenant_id,
                producers=[str(erased_id), *others],
                ingested_at=_OLD + datetime.timedelta(hours=9),
            )
            await _seed_signals(
                session,
                tenant_id=tenant_id,
                producers=[*others, str(uuid.uuid4())],
                ingested_at=_UNTOUCHED + datetime.timedelta(hours=9),
            )

        yield {
            "factory": factory,
            "tenant_id": tenant_id,
            "ctx": TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"]),
            "erased_id": erased_id,
        }
    finally:
        await engine.dispose()


def _writer(world: dict[str, Any], *, now: datetime.datetime, windows: int) -> aggregates.PrivacyAggregateWriter:
    return aggregates.PrivacyAggregateWriter(world["factory"], clock=_Clock(now), trailing_windows=windows)


async def _cell(world: dict[str, Any], window_start: datetime.datetime) -> Any:
    """The one stored row for the signal-mix cell over that window, or None."""
    async with world["factory"]() as session:
        return (
            await session.execute(
                text(
                    "SELECT actor_count, value, suppressed, partial, expires_at "
                    "  FROM privacy_aggregates "
                    " WHERE tenant_id = :t AND cohort_key = :c AND metric = :m AND window_start = :ws"
                ),
                {
                    "t": world["tenant_id"],
                    "c": COHORT_TENANT,
                    "m": aggregates.METRIC_SIGNAL_SOURCE_MIX,
                    "ws": window_start,
                },
            )
        ).one_or_none()


async def _versions(world: dict[str, Any], window_start: datetime.datetime) -> int:
    async with world["factory"]() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM privacy_aggregates "
                        " WHERE tenant_id = :t AND cohort_key = :c AND metric = :m AND window_start = :ws"
                    ),
                    {
                        "t": world["tenant_id"],
                        "c": COHORT_TENANT,
                        "m": aggregates.METRIC_SIGNAL_SOURCE_MIX,
                        "ws": window_start,
                    },
                )
            ).scalar_one()
        )


async def _erase(world: dict[str, Any]) -> dict[str, int]:
    """The real participant: deletes the producer's signals and tombstones each one."""
    erasure = SignalErasure(world["factory"], _salts(), clock=_Clock(_ERASED_AT))
    return await erasure.erase_actor(world["ctx"], world["erased_id"])


@pytest.mark.asyncio
async def test_a_cell_the_erasure_changed_is_withheld_even_though_it_clears_every_floor(
    world: dict[str, Any],
) -> None:
    """The whole reason this module exists, in one sequence.

    Six producers publish; one is erased; five remain, which is still above the
    five-actor floor and five events, which is still above the event floor. A writer
    that only applied the floors would publish the smaller figure, and the reader who
    saw both would know exactly what the erased producer contributed.
    """
    await _writer(world, now=_NOW, windows=1).run_once()

    before = await _cell(world, _RECENT)
    assert before is not None
    assert before.suppressed is False
    assert before.value["cells"] == {_SOURCE_SYSTEM: _PRODUCERS}
    assert before.actor_count == _PRODUCERS

    counts = await _erase(world)
    assert counts["signals"] == 2

    await _writer(world, now=_AFTER, windows=1).run_once()

    after = await _cell(world, _RECENT)
    assert after is not None
    assert after.suppressed is True
    # Not a smaller figure. No figure: the value and the actor count both go,
    # because an actor count is a number two versions can be subtracted from too.
    assert after.value is None
    assert after.actor_count == 0
    assert after.partial is False


@pytest.mark.asyncio
async def test_a_bare_re_run_never_republishes_a_withheld_cell(world: dict[str, Any]) -> None:
    """Withholding is one-way, and this is the case that makes it load-bearing.

    The withheld window is inside the trailing pass, so every later tick recomputes
    it from the surviving rows and offers a perfectly lawful figure. Publishing it
    one tick later is the same subtraction, just delayed.
    """
    await _writer(world, now=_NOW, windows=1).run_once()
    await _erase(world)
    await _writer(world, now=_AFTER, windows=1).run_once()

    for tick in range(1, 4):
        await _writer(world, now=_AFTER + datetime.timedelta(hours=tick), windows=1).run_once()
        cell = await _cell(world, _RECENT)
        assert cell is not None
        assert cell.suppressed is True, f"republished on tick {tick}"
        assert cell.value is None
        assert cell.actor_count == 0


@pytest.mark.asyncio
async def test_no_predecessor_of_a_cell_survives_anywhere(world: dict[str, Any]) -> None:
    """The structural half: a recompute has nowhere to leave the figure it replaced.

    Without it the other rules are conventions — a reader would simply select the
    older row instead of differencing two published ones.
    """
    await _writer(world, now=_NOW, windows=1).run_once()
    await _erase(world)
    await _writer(world, now=_AFTER, windows=1).run_once()
    await _writer(world, now=_AFTER + datetime.timedelta(hours=1), windows=1).run_once()

    assert await _versions(world, _RECENT) == 1


@pytest.mark.asyncio
async def test_an_old_window_is_reached_only_because_the_tombstone_ledger_says_to_look(
    world: dict[str, Any],
) -> None:
    """A tombstone never records which window it touched — the row that would say is gone.

    So the trigger is deliberately coarse: every cell computed before the newest
    erasure is re-examined, and the comparison decides which of them moved. This
    window is a week outside the trailing pass, so nothing else would revisit it.
    """
    await _writer(world, now=_NOW, windows=10).run_once()

    before = await _cell(world, _OLD)
    assert before is not None
    assert before.suppressed is False
    assert before.value["cells"] == {_SOURCE_SYSTEM: _PRODUCERS}

    await _erase(world)
    # One trailing window: this old cell is outside it entirely.
    await _writer(world, now=_AFTER, windows=1).run_once()

    after = await _cell(world, _OLD)
    assert after is not None
    assert after.suppressed is True
    assert after.value is None


@pytest.mark.asyncio
async def test_a_window_the_erasure_did_not_touch_keeps_its_figure(world: dict[str, Any]) -> None:
    """The retraction is per cell. A writer that withheld everything would be useless.

    This window is re-examined for the same reason the old one is — it was computed
    before the erasure — and it survives that examination because its figure did not
    move, which is the comparison doing the discriminating rather than the trigger.
    """
    await _writer(world, now=_NOW, windows=10).run_once()
    published = await _cell(world, _UNTOUCHED)
    assert published is not None
    assert published.suppressed is False

    await _erase(world)
    await _writer(world, now=_AFTER, windows=1).run_once()

    after = await _cell(world, _UNTOUCHED)
    assert after is not None
    assert after.suppressed is False
    assert after.value == published.value
    assert after.actor_count == published.actor_count


@pytest.mark.asyncio
async def test_a_stored_cell_expires_with_the_records_it_summarizes(world: dict[str, Any]) -> None:
    """An aggregate is a derivative and may not outlive its sources.

    Anchored at the window's end rather than at the computation, so recomputing an
    old window cannot extend the life of an aggregate over old records.
    """
    await _writer(world, now=_NOW, windows=10).run_once()

    cell = await _cell(world, _OLD)
    assert cell is not None
    assert cell.expires_at == policies.expiry_deadline(
        policies.RECORD_EXTERNAL_SIGNAL, _OLD + aggregates.WINDOW
    )


@pytest.mark.asyncio
async def test_the_schema_refuses_a_reported_cell_below_the_approved_floor(
    world: dict[str, Any],
) -> None:
    """The floor is checked where the row lands, not only in the job that computes it.

    Code may be stricter; nothing may be looser, and a writer nobody has read yet
    cannot talk the database into publishing a cell three people contributed to.
    """
    async with world["factory"]() as session:
        with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_aggregate_meets_the_floor"):
            await session.execute(
                text(
                    "INSERT INTO privacy_aggregates (tenant_id, cohort_key, metric, window_start, "
                    "  window_end, actor_count, value, suppressed, partial, policy_version, "
                    "  computed_at, expires_at) "
                    "VALUES (:t, :c, 'hand_written', :ws, :we, 3, '{}'::jsonb, FALSE, FALSE, :v, :now, :exp)"
                ),
                {
                    "t": world["tenant_id"],
                    "c": COHORT_TENANT,
                    "ws": _RECENT,
                    "we": _RECENT + aggregates.WINDOW,
                    "v": policies.POLICY_VERSION,
                    "now": _NOW,
                    "exp": _NOW + datetime.timedelta(days=1),
                },
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_every_metric_is_computed_against_the_real_database(world: dict[str, Any]) -> None:
    """The grouping-set statements run, and each metric materializes its own cell.

    A metric that only ever ran against a fake session would be a statement nobody
    had executed — the shape most likely to be wrong in a way unit tests cannot see.
    """
    await _writer(world, now=_NOW, windows=1).run_once()

    async with world["factory"]() as session:
        metrics = {
            str(row[0])
            for row in (
                await session.execute(
                    text("SELECT DISTINCT metric FROM privacy_aggregates WHERE tenant_id = :t"),
                    {"t": world["tenant_id"]},
                )
            ).all()
        }
    assert metrics == set(aggregates.AGGREGATE_METRICS)
