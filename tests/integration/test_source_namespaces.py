"""Registering what a replayed stream carries, and reading it back.

The write path's use of this is proved in `test_memory_events_envelope_gate.py`,
where a declared tier turns up on the advisory record. What is here is the
registry's own behaviour: the refusals that keep a declaration meaningful, and
the update semantics an operator correcting a tier depends on.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.exceptions import ValidationError
from contextplane.service.memory.source_namespaces import SourceNamespaceService
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 21, 12, 0, 0, tzinfo=datetime.UTC)
_REASON = "payroll exports carry employee compensation and are restricted for that reason"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def ctx(factory: async_sessionmaker[AsyncSession]) -> TenantContext:
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, now(), TRUE)"
            ),
            {"t": tenant_id, "s": f"msn-{tenant_id.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'operator', :sub, now())"
            ),
            {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:8]}"},
        )
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"], oidc_subject="op")


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession]) -> SourceNamespaceService:
    return SourceNamespaceService(factory, clock=FakeClock(_NOW))


@pytest.mark.asyncio
async def test_a_declared_stream_reads_back_its_tier(service: SourceNamespaceService, ctx: TenantContext) -> None:
    await service.register(
        ctx,
        source_system="hr",
        source_namespace="payroll",
        data_sensitivity="restricted",
        reason=_REASON,
    )

    found = await service.sensitivity_of(ctx, source_system="hr", source_namespace="payroll")

    assert found == "restricted"


@pytest.mark.asyncio
async def test_an_undeclared_stream_reads_back_nothing(service: SourceNamespaceService, ctx: TenantContext) -> None:
    """`None`, not a substituted default.

    The caller passes this straight into the manifest, where an absent tier is
    already read as the most restrictive. Substituting one here would be a second
    copy of that rule, in a second place, and the two would eventually disagree
    about which tier is strictest.
    """
    assert await service.sensitivity_of(ctx, source_system="hr", source_namespace="nope") is None


@pytest.mark.asyncio
async def test_re_declaring_corrects_the_tier_and_keeps_the_first_registration_date(
    service: SourceNamespaceService, ctx: TenantContext
) -> None:
    """An operator correcting `internal` to `restricted` is not a second stream.

    `registered_at` stays put because it answers "how long has this been
    governed", which a correction does not reset. `updated_at` and the actor move,
    so the row always says who made the declaration currently in force.
    """
    first = await service.register(
        ctx, source_system="hr", source_namespace="payroll", data_sensitivity="internal", reason=_REASON
    )

    second = await service.register(
        ctx,
        source_system="hr",
        source_namespace="payroll",
        data_sensitivity="restricted",
        reason="reclassified after review; this export includes compensation bands",
    )

    assert second.data_sensitivity == "restricted"
    assert second.registered_at == first.registered_at
    assert await service.sensitivity_of(ctx, source_system="hr", source_namespace="payroll") == "restricted"


@pytest.mark.asyncio
async def test_a_tier_outside_the_closed_scale_is_refused(service: SourceNamespaceService, ctx: TenantContext) -> None:
    """Refused at the boundary rather than by the CHECK, so the caller learns the
    scale instead of a constraint name."""
    with pytest.raises(ValidationError, match="handling tier"):
        await service.register(
            ctx,
            source_system="hr",
            source_namespace="payroll",
            data_sensitivity="ultra-secret",
            reason=_REASON,
        )


@pytest.mark.asyncio
async def test_a_tier_without_a_stated_reason_is_refused(service: SourceNamespaceService, ctx: TenantContext) -> None:
    """A handling tier nobody justified is one nobody will revisit -- the same bar
    the governed-magnitude registry holds a number to."""
    with pytest.raises(ValidationError, match="at least"):
        await service.register(
            ctx, source_system="hr", source_namespace="payroll", data_sensitivity="restricted", reason="prod"
        )


@pytest.mark.asyncio
async def test_one_tenants_declaration_is_not_anothers(
    service: SourceNamespaceService, ctx: TenantContext, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The key leads with `tenant_id`, and two tenants replaying the same upstream
    system classify it independently -- one company's Slack is not another's."""
    other = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, now(), TRUE)"
            ),
            {"t": other, "s": f"msn-{other.hex[:8]}"},
        )
    await service.register(
        ctx, source_system="chat", source_namespace="slack", data_sensitivity="internal", reason=_REASON
    )
    other_ctx = TenantContext(tenant_id=other, actor_id=ctx.actor_id, roles=["admin"], oidc_subject="op")

    assert await service.sensitivity_of(other_ctx, source_system="chat", source_namespace="slack") is None


@pytest.mark.asyncio
async def test_a_tenant_can_review_what_it_declared(service: SourceNamespaceService, ctx: TenantContext) -> None:
    await service.register(
        ctx, source_system="hr", source_namespace="payroll", data_sensitivity="restricted", reason=_REASON
    )
    await service.register(
        ctx, source_system="chat", source_namespace="slack", data_sensitivity="internal", reason=_REASON
    )

    listed = await service.list_for_tenant(ctx)

    assert [(n.source_system, n.data_sensitivity) for n in listed] == [
        ("chat", "internal"),
        ("hr", "restricted"),
    ]
