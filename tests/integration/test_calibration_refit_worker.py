"""The calibration-refit worker and its admin surface, against real Postgres.

`ConfirmationService.adjudicate` writes judged outcomes that nothing loads
without this worker: `load_observations -> fit -> publish` has no other
caller. These tests seed real adjudications for two distinct strategies --
one that reaches the evaluation-set floor and one that does not -- run the
worker, and check the service's own real gates decided what this design
requires: enough evidence gets a mapping published and activated, not enough
gets refused with no row at all. The admin surface is then driven over real
HTTP (`EntitlementAuthHarness`) to prove it reflects the same state the
worker produced, and that `:refit` reaches the identical sequence on demand.

**Shared-container caution:** `pg_container` is one Postgres for the whole
test session, and the worker's own strategy-discovery walk is global -- it
is not scoped to what this file seeded. Every assertion below is keyed on
this file's own randomly-generated strategy ids, never on a global count of
"how many strategies were considered," so a claim staged by an unrelated
test file in the same session can never make these assertions flaky.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.catalog.global_vocabulary import GlobalVocabularyService
from registry.service.memory.calibration import UNCALIBRATED, CalibrationService
from registry.service.memory.claim_authority import Evidence
from registry.service.memory.claim_ontology import seed_ontology
from registry.service.memory.claim_writer import ClaimService
from registry.service.memory.confirmation import VERDICT_CORRECT, VERDICT_INCORRECT, ConfirmationService
from registry.workers.calibration_refit import CalibrationRefitWorker
from tests.helpers.auth_harness import EntitlementAuthHarness, bearer_headers, patch_validator_for_actor
from tests.helpers.clock import FakeClock
from tests.helpers.context import claim_producer_ctx as _ctx
from tests.helpers.seeding import seed_entity as _seed_entity

_NOW = datetime.datetime(2026, 8, 5, 12, 0, tzinfo=datetime.UTC)
_PROVIDER = "anthropic"
_MODEL = "claude-haiku-4-5-20251001"


def _at(minutes: int) -> datetime.datetime:
    return _NOW + datetime.timedelta(minutes=minutes)


def _strategy_id() -> str:
    """A strategy id unique to this test run -- never a name a production
    strategy or another test file's fixture would ever pick, so the worker's
    global walk of the shared container can never attribute another test's
    rows to this one's triple."""
    return f"calib-refit-test-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def ontology(factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"calibrefit-{tid.hex[:8]}", "now": _NOW},
        )
    return tid


async def _seed_actor(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                "                    actor_kind, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, 'human', :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return aid


async def _seed_judged_triple(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    aid: uuid.UUID,
    subject: uuid.UUID,
    *,
    strategy_id: str,
    n: int,
    n_correct: int,
    provider_confidence: float = 0.9,
    at: int = 0,
) -> None:
    """Stage `n` claims under `strategy_id`, each carrying `provider_confidence`
    as its self-report, and adjudicate every one -- `n_correct` of them
    `correct`, the rest `incorrect`. One claim per adjudication (all judged by
    the same actor) rather than several reviewers judging one claim: the
    uniqueness constraint is on (claim_id, adjudicated_by), and this is the
    cheaper way to reach a given observation count.
    """
    clock = FakeClock(_at(at))
    claims = ClaimService(factory, clock=clock)
    confirmations = ConfirmationService(factory, claims, clock=clock)
    namespace = f"{strategy_id}-ns"
    for i in range(n):
        claim = await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value="platform",
            evidence=(Evidence(kind="session_event", ref=f"e{i}"),),
            provider_confidence=provider_confidence,
            namespace=namespace,
            strategy_id=strategy_id,
        )
        verdict = VERDICT_CORRECT if i < n_correct else VERDICT_INCORRECT
        await confirmations.adjudicate(
            _ctx(tid, aid),
            claim_id=claim.claim_id,
            verdict=verdict,
            observed_confidence=provider_confidence,
        )


def _worker(factory: async_sessionmaker[AsyncSession]) -> CalibrationRefitWorker:
    clock = FakeClock(_NOW)
    calibration = CalibrationService(factory, clock=clock)
    # Oversized on purpose -- the shared container can carry other tests'
    # distinct strategy ids by the time this runs, and a production-sized
    # batch walked in `ORDER BY strategy_id` could push this file's own
    # triples outside the window and never consider them.
    return CalibrationRefitWorker(
        factory, calibration, provider_id=_PROVIDER, model_id=_MODEL, clock=clock, batch_size=100_000
    )


async def _mapping_row(factory: async_sessionmaker[AsyncSession], *, strategy_id: str) -> dict[str, object] | None:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT version, status, n_adjudicated, measured_error "
                        "FROM memory_calibration_mapping "
                        "WHERE provider_id = :p AND model_id = :m AND strategy_id = :s"
                    ),
                    {"p": _PROVIDER, "m": _MODEL, "s": strategy_id},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# run_once: real semantics per triple
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_well_calibrated_triple_at_the_floor_is_published_and_activated(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Exactly the evaluation-set floor, all falling in one bin whose observed
    rate matches its own raw self-report -- a fit with zero measured error,
    which is well within the accuracy bound."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    strategy_id = _strategy_id()

    await _seed_judged_triple(factory, tid, aid, subject, strategy_id=strategy_id, n=200, n_correct=180)

    report = await _worker(factory).run_once()

    # Global across the shared container -- other tests' own triples may also
    # be walked in the same tick. This file's own triple is what the row
    # assertions below pin.
    assert report.considered >= 1

    row = await _mapping_row(factory, strategy_id=strategy_id)
    assert row is not None
    assert row["status"] == "active"
    assert row["n_adjudicated"] == 200

    calibration = CalibrationService(factory, clock=FakeClock(_NOW))
    active = await calibration.load_active(provider_id=_PROVIDER, model_id=_MODEL, strategy_id=strategy_id)
    assert active is not None
    assert active.meets_target


@pytest.mark.asyncio
async def test_a_triple_below_the_evaluation_floor_is_refused_with_no_row_at_all(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Publish's own gate, exercised end to end: too little evidence to check
    the accuracy target even in principle, so nothing is stored -- not an
    active mapping, not a failed one."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    strategy_id = _strategy_id()

    await _seed_judged_triple(factory, tid, aid, subject, strategy_id=strategy_id, n=5, n_correct=5)

    await _worker(factory).run_once()

    assert await _mapping_row(factory, strategy_id=strategy_id) is None
    calibration = CalibrationService(factory, clock=FakeClock(_NOW))
    assert await calibration.active_version(provider_id=_PROVIDER, model_id=_MODEL, strategy_id=strategy_id) == (
        UNCALIBRATED
    )


@pytest.mark.asyncio
async def test_one_triples_run_once_result_does_not_depend_on_the_others(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Both triples considered in the same tick, each landing at the outcome
    its own evidence earns -- proof that the below-floor triple's refusal is
    not somehow bleeding into the well-evidenced one, or vice versa."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    thin_strategy = _strategy_id()
    thick_strategy = _strategy_id()

    await _seed_judged_triple(factory, tid, aid, subject, strategy_id=thin_strategy, n=3, n_correct=3, at=0)
    await _seed_judged_triple(factory, tid, aid, subject, strategy_id=thick_strategy, n=200, n_correct=180, at=1)

    await _worker(factory).run_once()

    assert await _mapping_row(factory, strategy_id=thin_strategy) is None
    thick_row = await _mapping_row(factory, strategy_id=thick_strategy)
    assert thick_row is not None
    assert thick_row["status"] == "active"


# ---------------------------------------------------------------------------
# Admin surface: GET reflects real state, :refit re-runs one triple
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_client(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    slug = f"calibadmin-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        admin_persona = harness.add_persona(slug, roles=["admin"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(admin_persona)
            with patch_validator_for_actor(admin_persona):
                yield {"client": client, "slug": slug, "pg_url": pg_container}


@pytest.mark.asyncio
async def test_admin_get_reflects_a_triple_the_worker_published(
    factory: async_sessionmaker[AsyncSession], ontology: None, admin_client: dict[str, Any]
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    strategy_id = _strategy_id()

    await _seed_judged_triple(factory, tid, aid, subject, strategy_id=strategy_id, n=200, n_correct=180)
    await _worker(factory).run_once()

    client: httpx.AsyncClient = admin_client["client"]
    slug = admin_client["slug"]
    resp = await client.get("/v1/admin/memory-calibration", headers=bearer_headers(tenant_slug=slug))
    assert resp.status_code == 200, resp.text

    rows = {row["strategy_id"]: row for row in resp.json()}
    assert strategy_id in rows
    assert rows[strategy_id]["status"] == "active"
    assert rows[strategy_id]["provider_id"] == _PROVIDER
    assert rows[strategy_id]["model_id"] == _MODEL
    assert rows[strategy_id]["n_adjudicated"] == 200


@pytest.mark.asyncio
async def test_admin_refit_reruns_one_triple_and_the_get_route_reflects_it(
    factory: async_sessionmaker[AsyncSession], ontology: None, admin_client: dict[str, Any]
) -> None:
    """A triple below the floor when the worker last ran, refit on demand
    through the admin route after enough evidence accumulates -- proving
    `:refit` reaches the identical `load_observations -> fit -> publish`
    sequence the worker itself calls, not a second implementation of it."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    strategy_id = _strategy_id()

    await _seed_judged_triple(factory, tid, aid, subject, strategy_id=strategy_id, n=5, n_correct=5, at=0)
    await _worker(factory).run_once()
    assert await _mapping_row(factory, strategy_id=strategy_id) is None

    # More evidence arrives before the worker's next scheduled tick -- an
    # operator refits this one triple now instead of waiting.
    await _seed_judged_triple(factory, tid, aid, subject, strategy_id=strategy_id, n=195, n_correct=176, at=10)

    client: httpx.AsyncClient = admin_client["client"]
    slug = admin_client["slug"]
    resp = await client.post(
        "/v1/admin/memory-calibration:refit",
        json={"provider_id": _PROVIDER, "model_id": _MODEL, "strategy_id": strategy_id},
        headers=bearer_headers(tenant_slug=slug),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["strategy_id"] == strategy_id
    assert body["activated"] is True
    assert body["version"] != UNCALIBRATED
    assert body["n_adjudicated"] == 200

    get_resp = await client.get("/v1/admin/memory-calibration", headers=bearer_headers(tenant_slug=slug))
    assert get_resp.status_code == 200, get_resp.text
    rows = {row["strategy_id"]: row for row in get_resp.json()}
    assert rows[strategy_id]["status"] == "active"
    assert rows[strategy_id]["version"] == body["version"]

    row = await _mapping_row(factory, strategy_id=strategy_id)
    assert row is not None
    assert row["status"] == "active"
    # fitted_by names the admin actor who triggered this refit, not a NULL
    # system-run row -- the on-demand path is attributable to whoever asked
    # for it, unlike the periodic worker's own unattended ticks.
    async with factory() as session:
        fitted_by = (
            await session.execute(
                text(
                    "SELECT fitted_by FROM memory_calibration_mapping "
                    "WHERE provider_id = :p AND model_id = :m AND strategy_id = :s AND status = 'active'"
                ),
                {"p": _PROVIDER, "m": _MODEL, "s": strategy_id},
            )
        ).scalar_one()
    assert fitted_by is not None
