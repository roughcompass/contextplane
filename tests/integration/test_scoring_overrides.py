"""A tenant's scoring override, through the lifecycle that governs it.

The accessor's unit tests prove the fallback chain against a fake session. These
prove the thing the fake cannot: that an override only governs once it has been
*activated*, and stops governing when the binding is rolled back. Publishing and
planning are not governance — that distinction is the entire reason ADR 0004
chose the binding lifecycle over a settings field, and it is worth exactly
nothing if it holds only in a mock.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane import ranking
from contextplane.profile import bindings as profile_bindings
from contextplane.profile import service as profile_service
from contextplane.profile.schemas.entity import EntityTypeDefinition
from contextplane.profile.scoring import (
    SOURCE_CORE,
    SOURCE_EXTENSION,
    ScoringOverrideRefused,
    resolve_weights,
)

_NAMESPACE = "scoring"
_MODEL = "salience-weights@1"
_START = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.UTC)

#: A reweighting that sums to one and differs from core on every term, so a test
#: asserting "the override governs" cannot pass by accidentally reading core.
_TENANT_WEIGHTS = {
    "state_change": 0.20,
    "outcome_decisive": 0.20,
    "human_engagement": 0.40,
    "novelty": 0.05,
    "entity_density": 0.10,
    "tool_diversity": 0.05,
}

_REASON = (
    "This tenant's agents work alongside reviewers who correct them constantly, so human engagement is "
    "the strongest available signal that an episode mattered, and tool activity is the weakest because "
    "their agents call many tools per turn regardless of whether anything was settled."
)


class _MovableClock:
    """Advances only when a test asks. Two activations in one millisecond would
    order arbitrarily and the test would pass on how fast the machine is."""

    def __init__(self) -> None:
        self._now = _START

    def now(self) -> datetime.datetime:
        return self._now

    def advance(self, *, minutes: int) -> None:
        self._now = self._now + datetime.timedelta(minutes=minutes)


@pytest_asyncio.fixture
async def tenant(pg_container: str) -> AsyncIterator[dict[str, object]]:
    """A tenant with a published core revision and nothing bound yet."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    clock = _MovableClock()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'scoring')"),
                {"t": tenant_id, "s": f"sc-{tenant_id.hex[:10]}"},
            )
        publisher = profile_service.ProfileService(factory, clock=clock)
        revision = await publisher.publish_revision(
            profile_family="platform",
            profile_name=f"sc-{tenant_id.hex[:12]}",
            semantic_version="1.0.0",
            entities=(EntityTypeDefinition(namespace=_NAMESPACE, type_name="widget"),),
            relationships=(),
            interfaces=(),
            compatibility="backward_compatible",
            published_by="platform@example.test",
        )
        yield {
            "factory": factory,
            "tenant": tenant_id,
            "clock": clock,
            "publisher": publisher,
            "bindings": profile_bindings.BindingService(factory, clock=clock),
            "revision": revision,
        }
    finally:
        await engine.dispose()


async def _publish_override(fixture: dict[str, object], weights: dict[str, float]) -> uuid.UUID:
    publisher: profile_service.ProfileService = fixture["publisher"]  # type: ignore[assignment]
    revision: profile_service.PublishedRevision = fixture["revision"]  # type: ignore[assignment]
    published = await publisher.publish_extension(
        tenant_id=fixture["tenant"],  # type: ignore[arg-type]
        namespace=_NAMESPACE,
        target_core_revision_id=revision.profile_revision_id,
        entities=(),
        relationships=(),
        interfaces=(),
        published_by="operator@example.test",
        scoring_overrides={_MODEL: {"parameters": weights, "reason": _REASON}},
    )
    return published.extension_revision_id


async def _activate(fixture: dict[str, object], *extensions: uuid.UUID) -> profile_bindings.Binding:
    service: profile_bindings.BindingService = fixture["bindings"]  # type: ignore[assignment]
    revision: profile_service.PublishedRevision = fixture["revision"]  # type: ignore[assignment]
    clock: _MovableClock = fixture["clock"]  # type: ignore[assignment]
    planned = await service.plan_binding(
        tenant_id=fixture["tenant"],  # type: ignore[arg-type]
        profile_revision_id=revision.profile_revision_id,
        extension_revision_ids=tuple(extensions),
        effective_from=clock.now(),
        actor="operator@example.test",
        reason="adopting tenant scoring weights",
        audit_reference="CHG-9",
    )
    await service.start_validation(
        tenant_id=fixture["tenant"],  # type: ignore[arg-type]
        binding_id=planned.binding_id,
        actor="operator@example.test",
        reason="validating",
    )
    return await service.activate(
        tenant_id=fixture["tenant"],  # type: ignore[arg-type]
        binding_id=planned.binding_id,
        actor="operator@example.test",
        reason="cutover",
        audit_reference="CHG-9",
    )


async def _resolved(fixture: dict[str, object]) -> object:
    factory: async_sessionmaker[AsyncSession] = fixture["factory"]  # type: ignore[assignment]
    async with factory() as session:
        return await resolve_weights(session, tenant_id=fixture["tenant"], model_id=_MODEL)  # type: ignore[arg-type]


# --- the lifecycle ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_published_but_unbound_override_governs_nothing(tenant: dict[str, object]) -> None:
    """Publishing is not governance. An extension sitting in the table with no
    binding is a proposal, and a resolver that read it would make the whole
    plan-validate-activate sequence decorative."""
    await _publish_override(tenant, _TENANT_WEIGHTS)

    resolved = await _resolved(tenant)
    assert resolved.value == ranking.weights(_MODEL)  # type: ignore[attr-defined]
    assert resolved.source == SOURCE_CORE  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_planned_but_unactivated_override_governs_nothing(tenant: dict[str, object]) -> None:
    """The task's named case. A planned binding is a scheduled intention and the
    tenant is still governed by whatever is active, which here is nothing."""
    extension_id = await _publish_override(tenant, _TENANT_WEIGHTS)
    service: profile_bindings.BindingService = tenant["bindings"]  # type: ignore[assignment]
    revision: profile_service.PublishedRevision = tenant["revision"]  # type: ignore[assignment]
    clock: _MovableClock = tenant["clock"]  # type: ignore[assignment]
    await service.plan_binding(
        tenant_id=tenant["tenant"],  # type: ignore[arg-type]
        profile_revision_id=revision.profile_revision_id,
        extension_revision_ids=(extension_id,),
        effective_from=clock.now(),
        actor="operator@example.test",
        reason="planned only",
        audit_reference="CHG-9",
    )

    resolved = await _resolved(tenant)
    assert resolved.source == SOURCE_CORE  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_activated_override_governs(tenant: dict[str, object]) -> None:
    """The control for both tests above. Without it they would pass on a resolver
    that never finds an override at all."""
    extension_id = await _publish_override(tenant, _TENANT_WEIGHTS)
    await _activate(tenant, extension_id)

    resolved = await _resolved(tenant)
    assert resolved.value == _TENANT_WEIGHTS  # type: ignore[attr-defined]
    assert resolved.source == SOURCE_EXTENSION  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_activating_a_binding_without_the_extension_restores_core(tenant: dict[str, object]) -> None:
    """The rollback path in the form the lifecycle actually offers: activating a
    binding that names no extension closes the previous one, and the tenant is
    back on the committed defaults with no data deleted."""
    extension_id = await _publish_override(tenant, _TENANT_WEIGHTS)
    await _activate(tenant, extension_id)
    assert (await _resolved(tenant)).source == SOURCE_EXTENSION  # type: ignore[attr-defined]

    clock: _MovableClock = tenant["clock"]  # type: ignore[assignment]
    clock.advance(minutes=30)
    await _activate(tenant)

    resolved = await _resolved(tenant)
    assert resolved.value == ranking.weights(_MODEL)  # type: ignore[attr-defined]
    assert resolved.source == SOURCE_CORE  # type: ignore[attr-defined]


# --- what publication refuses ------------------------------------------------------


@pytest.mark.asyncio
async def test_weights_that_do_not_sum_to_one_are_refused_at_publication(tenant: dict[str, object]) -> None:
    """Refused before the row exists. A tenant whose bad weighting was rejected
    at activation would leave a document in the table that nothing can use and
    nothing will clean up."""
    bad = dict(_TENANT_WEIGHTS) | {"human_engagement": 0.90}
    with pytest.raises(ScoringOverrideRefused, match="sum to"):
        await _publish_override(tenant, bad)

    factory: async_sessionmaker[AsyncSession] = tenant["factory"]  # type: ignore[assignment]
    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM profile_extensions WHERE tenant_id = :t"),
                {"t": tenant["tenant"]},
            )
        ).scalar_one()
    assert count == 0, "a refused override must not leave a row behind"


@pytest.mark.asyncio
async def test_a_partial_override_is_refused(tenant: dict[str, object]) -> None:
    """Naming three of six weights leaves the rest at core and sums to something
    that is not one, so the tenant scores on a scale nobody designed."""
    partial = {"state_change": 0.5, "human_engagement": 0.5}
    with pytest.raises(ScoringOverrideRefused, match="names every weight"):
        await _publish_override(tenant, partial)


@pytest.mark.asyncio
async def test_an_override_without_a_reason_is_refused(tenant: dict[str, object]) -> None:
    """The same bar the committed registry holds itself to. A tenant's override
    is easier to publish than the core is to change, so if anything the looser
    artifact needs the tighter rule."""
    publisher: profile_service.ProfileService = tenant["publisher"]  # type: ignore[assignment]
    revision: profile_service.PublishedRevision = tenant["revision"]  # type: ignore[assignment]
    with pytest.raises(ScoringOverrideRefused, match="at least 20 words"):
        await publisher.publish_extension(
            tenant_id=tenant["tenant"],  # type: ignore[arg-type]
            namespace=_NAMESPACE,
            target_core_revision_id=revision.profile_revision_id,
            entities=(),
            relationships=(),
            interfaces=(),
            published_by="operator@example.test",
            scoring_overrides={_MODEL: {"parameters": _TENANT_WEIGHTS, "reason": "we like it better"}},
        )


@pytest.mark.asyncio
async def test_an_extension_carrying_no_override_publishes_normally(tenant: dict[str, object]) -> None:
    """Most extensions extend the entity model and say nothing about scoring. The
    argument is optional and its absence means the core defaults."""
    publisher: profile_service.ProfileService = tenant["publisher"]  # type: ignore[assignment]
    revision: profile_service.PublishedRevision = tenant["revision"]  # type: ignore[assignment]
    published = await publisher.publish_extension(
        tenant_id=tenant["tenant"],  # type: ignore[arg-type]
        namespace=_NAMESPACE,
        target_core_revision_id=revision.profile_revision_id,
        entities=(EntityTypeDefinition(namespace=_NAMESPACE, type_name="gadget"),),
        relationships=(),
        interfaces=(),
        published_by="operator@example.test",
    )
    await _activate(tenant, published.extension_revision_id)

    assert (await _resolved(tenant)).source == SOURCE_CORE  # type: ignore[attr-defined]
