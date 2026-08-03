"""Per-tenant strategy configuration, and the line an override may not cross.

An override changes how well claims are found. It must never change what they are
allowed to mean — so enablement, floor, prompt, and model are configurable, and
the schema, the predicate set, and the namespace template are not reachable from
configuration at all.

Also covers the judgement that decides when a strategy is a defective prompt
rather than a transient failure, because retrying non-conformance costs a real
call per attempt and produces the same wrong output every time.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.exceptions import NotFoundError, ValidationError
from registry.extraction.config import (
    CONFORMANCE_TARGET,
    MIN_CONFORMANCE_SAMPLE,
    StrategyConfigService,
    judge_conformance,
)
from registry.extraction.strategies import OBSERVATION, STRATEGIES, SUMMARY
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def config(factory: async_sessionmaker[AsyncSession]) -> StrategyConfigService:
    return StrategyConfigService(factory, clock=FakeClock(_NOW))


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"cfg-{tid.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return tid, aid


def _ctx(tid: uuid.UUID, aid: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["admin"], oidc_subject="s")


def _counter(name: str, **labels: str) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


# --- defaults ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tenant_with_no_configuration_gets_every_strategy_enabled(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    """Absence means defaults, not disabled. A deployment needing a row per tenant
    per strategy before extraction did anything would look broken on every new
    tenant, and somebody would insert rows with whatever values were handy."""
    tid, _ = await _seed_tenant(factory)

    resolved = await config.resolve(tid)

    assert {r.strategy.strategy_id for r in resolved} == set(STRATEGIES)
    assert all(r.is_enabled for r in resolved)
    assert all(not r.prompt_is_overridden for r in resolved)


@pytest.mark.asyncio
async def test_the_default_floor_comes_from_the_strategy(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    tid, _ = await _seed_tenant(factory)
    resolved = await config.resolve_one(tid, OBSERVATION.strategy_id)
    assert resolved.confidence_floor == OBSERVATION.default_confidence_floor


@pytest.mark.asyncio
async def test_an_unknown_strategy_is_not_found(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    tid, _ = await _seed_tenant(factory)
    with pytest.raises(NotFoundError):
        await config.resolve_one(tid, "not_a_strategy")


# --- what an override may change ---------------------------------------------


@pytest.mark.asyncio
async def test_a_prompt_override_replaces_the_shipped_prompt(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    tid, aid = await _seed_tenant(factory)
    await config.upsert(
        _ctx(tid, aid), strategy_id=OBSERVATION.strategy_id, prompt_override="only find timeouts"
    )

    resolved = await config.resolve_one(tid, OBSERVATION.strategy_id)
    assert resolved.strategy.system_prompt == "only find timeouts"
    assert resolved.prompt_is_overridden


@pytest.mark.asyncio
async def test_an_override_cannot_widen_the_predicate_set(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    """The line that matters. A tenant able to add predicates through a config
    field would be redefining the shared vocabulary locally, which is exactly
    what a deployment-wide ontology exists to prevent."""
    tid, aid = await _seed_tenant(factory)
    await config.upsert(
        _ctx(tid, aid),
        strategy_id=OBSERVATION.strategy_id,
        prompt_override="you may use the predicate anything_i_like",
    )

    resolved = await config.resolve_one(tid, OBSERVATION.strategy_id)
    assert resolved.strategy.permitted_predicates == OBSERVATION.permitted_predicates
    assert "anything_i_like" not in resolved.strategy.permitted_predicates


@pytest.mark.asyncio
async def test_an_override_cannot_change_the_output_schema(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    """Schema-constrained output is the containment layer that makes prose
    unparseable. A configurable schema would make it optional."""
    tid, aid = await _seed_tenant(factory)
    await config.upsert(
        _ctx(tid, aid), strategy_id=OBSERVATION.strategy_id, prompt_override="return free text"
    )

    resolved = await config.resolve_one(tid, OBSERVATION.strategy_id)
    assert resolved.strategy.output_schema == OBSERVATION.output_schema


@pytest.mark.asyncio
async def test_an_override_cannot_change_the_namespace_template(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    tid, aid = await _seed_tenant(factory)
    await config.upsert(
        _ctx(tid, aid), strategy_id=OBSERVATION.strategy_id, prompt_override="x"
    )

    resolved = await config.resolve_one(tid, OBSERVATION.strategy_id)
    assert resolved.strategy.namespace_template == OBSERVATION.namespace_template


@pytest.mark.asyncio
async def test_an_empty_prompt_override_is_refused(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    """It is not an override. It would leave the model with no instructions while
    extraction kept running, which is the worst available outcome: output keeps
    arriving and it is nonsense."""
    tid, aid = await _seed_tenant(factory)
    with pytest.raises(ValidationError, match="empty prompt override"):
        await config.upsert(
            _ctx(tid, aid), strategy_id=OBSERVATION.strategy_id, prompt_override="   "
        )


@pytest.mark.asyncio
async def test_a_floor_outside_zero_to_one_is_refused(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    tid, aid = await _seed_tenant(factory)
    for bad in (-0.1, 1.5):
        with pytest.raises(ValidationError, match="between 0 and 1"):
            await config.upsert(
                _ctx(tid, aid), strategy_id=OBSERVATION.strategy_id, confidence_floor=bad
            )


# --- update semantics --------------------------------------------------------


@pytest.mark.asyncio
async def test_omitting_a_field_leaves_it_unchanged(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    """An operator disabling a strategy must not silently lose their prompt."""
    tid, aid = await _seed_tenant(factory)
    await config.upsert(
        _ctx(tid, aid), strategy_id=OBSERVATION.strategy_id, prompt_override="my prompt"
    )
    await config.upsert(_ctx(tid, aid), strategy_id=OBSERVATION.strategy_id, is_enabled=False)

    resolved = await config.resolve_one(tid, OBSERVATION.strategy_id)
    assert resolved.strategy.system_prompt == "my prompt"
    assert not resolved.is_enabled


@pytest.mark.asyncio
async def test_clearing_an_override_restores_the_shipped_prompt(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    """`None` already means leave alone, so removing an override needs its own
    flag. One nullable field cannot say both."""
    tid, aid = await _seed_tenant(factory)
    await config.upsert(
        _ctx(tid, aid), strategy_id=OBSERVATION.strategy_id, prompt_override="my prompt"
    )
    await config.upsert(
        _ctx(tid, aid), strategy_id=OBSERVATION.strategy_id, clear_prompt_override=True
    )

    resolved = await config.resolve_one(tid, OBSERVATION.strategy_id)
    assert resolved.strategy.system_prompt == OBSERVATION.system_prompt
    assert not resolved.prompt_is_overridden


@pytest.mark.asyncio
async def test_setting_and_clearing_at_once_is_refused(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    """Ambiguous. Picking one silently would be a coin flip on the operator's
    behalf."""
    tid, aid = await _seed_tenant(factory)
    with pytest.raises(ValidationError, match="both set and clear"):
        await config.upsert(
            _ctx(tid, aid),
            strategy_id=OBSERVATION.strategy_id,
            prompt_override="x",
            clear_prompt_override=True,
        )


@pytest.mark.asyncio
async def test_one_tenants_configuration_does_not_affect_another(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    tid_a, aid_a = await _seed_tenant(factory)
    tid_b, _ = await _seed_tenant(factory)
    await config.upsert(
        _ctx(tid_a, aid_a), strategy_id=OBSERVATION.strategy_id, is_enabled=False
    )

    assert not (await config.resolve_one(tid_a, OBSERVATION.strategy_id)).is_enabled
    assert (await config.resolve_one(tid_b, OBSERVATION.strategy_id)).is_enabled


@pytest.mark.asyncio
async def test_configuring_one_strategy_leaves_the_others_at_defaults(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    tid, aid = await _seed_tenant(factory)
    await config.upsert(_ctx(tid, aid), strategy_id=SUMMARY.strategy_id, is_enabled=False)

    assert (await config.resolve_one(tid, OBSERVATION.strategy_id)).is_enabled
    assert not (await config.resolve_one(tid, SUMMARY.strategy_id)).is_enabled


@pytest.mark.asyncio
async def test_a_disabled_strategy_is_still_listed(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    """A caller that could not see it would be unable to tell "switched off" from
    "not in this build"."""
    tid, aid = await _seed_tenant(factory)
    await config.upsert(_ctx(tid, aid), strategy_id=SUMMARY.strategy_id, is_enabled=False)

    ids = {r.strategy.strategy_id for r in await config.resolve(tid)}
    assert SUMMARY.strategy_id in ids


# --- namespaces --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_namespace_is_scoped_by_tenant_and_actor(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    tid, aid = await _seed_tenant(factory)
    resolved = await config.resolve_one(tid, OBSERVATION.strategy_id)

    namespace = resolved.namespace_for(tenant_id=tid, actor_id=aid, session_id="s1")
    assert str(tid) in namespace
    assert str(aid) in namespace


@pytest.mark.asyncio
async def test_the_summary_namespace_is_scoped_by_session(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    """A summary is about one session, so grouping it by actor alone would mix
    every conversation together."""
    tid, aid = await _seed_tenant(factory)
    resolved = await config.resolve_one(tid, SUMMARY.strategy_id)

    namespace = resolved.namespace_for(tenant_id=tid, actor_id=aid, session_id="abc-123")
    assert "abc-123" in namespace


@pytest.mark.asyncio
async def test_different_strategies_land_in_different_namespaces(
    factory: async_sessionmaker[AsyncSession], config: StrategyConfigService
) -> None:
    """Grouping is the point. One namespace for everything would make it
    decorative."""
    tid, aid = await _seed_tenant(factory)
    namespaces = {
        r.namespace_for(tenant_id=tid, actor_id=aid, session_id="s1")
        for r in await config.resolve(tid)
    }
    assert len(namespaces) == len(STRATEGIES)


# --- the defective-prompt verdict --------------------------------------------


def test_sustained_non_conformance_is_reported_as_defective() -> None:
    """The exit criterion. Retrying non-conformance costs a call per attempt and
    produces the same wrong output; a person has to change the prompt."""
    verdict = judge_conformance("s", candidates=100, staged=50)

    assert verdict.is_defective
    assert "defective prompt" in verdict.reason
    assert "not a transient failure" in verdict.reason


def test_a_strategy_at_target_is_not_defective() -> None:
    verdict = judge_conformance("s", candidates=100, staged=96)
    assert not verdict.is_defective
    assert verdict.ratio >= CONFORMANCE_TARGET


def test_exactly_at_target_is_not_defective() -> None:
    """The boundary. An off-by-one here means a strategy meeting its target gets
    reported as broken."""
    verdict = judge_conformance("s", candidates=100, staged=95)
    assert verdict.ratio == CONFORMANCE_TARGET
    assert not verdict.is_defective


def test_a_small_sample_yields_no_verdict() -> None:
    """Two refusals out of two is not evidence. Judging it would make every
    strategy defective on its first quiet hour, after which nobody believes the
    signal."""
    verdict = judge_conformance("s", candidates=2, staged=0)

    assert not verdict.is_defective
    assert not verdict.sample_is_sufficient
    assert "sample too small" in verdict.reason


def test_the_sample_boundary_is_inclusive() -> None:
    at_minimum = judge_conformance("s", candidates=MIN_CONFORMANCE_SAMPLE, staged=0)
    below = judge_conformance("s", candidates=MIN_CONFORMANCE_SAMPLE - 1, staged=0)

    assert at_minimum.is_defective
    assert not below.is_defective


def test_an_empty_sample_is_not_a_failure() -> None:
    """A quiet period is not a broken prompt."""
    verdict = judge_conformance("s", candidates=0, staged=0)
    assert verdict.ratio == 1.0
    assert not verdict.is_defective


def test_a_defective_verdict_is_counted_per_strategy() -> None:
    """Which prompt is broken, not merely that something is."""
    metric = "registry_extraction_strategy_defective_total"
    before = _counter(metric, strategy="probe_strategy")

    judge_conformance("probe_strategy", candidates=50, staged=1)

    assert _counter(metric, strategy="probe_strategy") == before + 1


def test_a_healthy_verdict_is_not_counted_as_defective() -> None:
    metric = "registry_extraction_strategy_defective_total"
    before = _counter(metric, strategy="healthy_strategy")

    judge_conformance("healthy_strategy", candidates=50, staged=50)

    assert _counter(metric, strategy="healthy_strategy") == before
