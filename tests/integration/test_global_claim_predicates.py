"""One shared vocabulary: predicates that mean the same thing in every tenant.

Two tenants each defining `depends_on` their own way cannot corroborate or
contradict each other — their claims are not comparable, which defeats the
point of a shared graph. So claim predicates can be defined once for the whole
deployment.

The tests that matter here are the collision rules. Shadowing is the failure
this exists to prevent, and it fails silently: the same name meaning two things
makes claims look comparable when they are not.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.exceptions import ConflictError, NotFoundError, ValidationError, VocabularyError
from registry.service.global_vocabulary import GlobalVocabularyService
from registry.service.vocabulary import VocabularyService
from registry.storage.models import CLAIM_PREDICATE_KIND, VocabularyValue
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"vocab-{tid.hex[:8]}", "now": _NOW},
        )
    return tid


@pytest.fixture
def globals_(factory: async_sessionmaker[AsyncSession]) -> GlobalVocabularyService:
    return GlobalVocabularyService(factory, clock=FakeClock(_NOW))


@pytest.fixture
def tenant_vocab(factory: async_sessionmaker[AsyncSession]) -> VocabularyService:
    return VocabularyService(factory)


def _ctx(tid: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tid, actor_id=uuid.uuid4(), roles=["admin"], oidc_subject="s")


def _name() -> str:
    return f"pred_{uuid.uuid4().hex[:10]}"


async def _add_local(
    factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, value: str
) -> None:
    async with factory() as session, session.begin():
        session.add(
            VocabularyValue(
                vocab_id=uuid.uuid4(),
                tenant_id=tid,
                kind=CLAIM_PREDICATE_KIND,
                value=value,
                is_system=False,
                deprecated_at=None,
                created_at=_NOW,
                value_type="string",
                claim_category="dependency",
                definition="a local term",
            )
        )


# --- a global predicate resolves identically everywhere ---------------------------


@pytest.mark.asyncio
async def test_a_global_predicate_resolves_in_a_tenant_that_never_defined_it(
    globals_: GlobalVocabularyService, tenant_vocab: VocabularyService, tenant: uuid.UUID
) -> None:
    """The property the whole requirement exists for."""
    name = _name()
    await globals_.create_predicate(
        value=name, value_type="entity_ref", claim_category="dependency", definition="depends on"
    )

    await tenant_vocab.validate_value(_ctx(tenant), CLAIM_PREDICATE_KIND, name)


@pytest.mark.asyncio
async def test_two_tenants_resolve_a_global_predicate_to_the_same_declared_type(
    globals_: GlobalVocabularyService, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Claims are comparable only if the type is identical everywhere. One row,
    so there is nowhere for a second meaning to live."""
    name = _name()
    await globals_.create_predicate(
        value=name, value_type="duration_seconds", claim_category="operational_lifecycle",
        definition="time to restore",
    )

    predicates = {p.value: p for p in await globals_.list_predicates()}

    assert predicates[name].value_type == "duration_seconds"
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM vocabulary_values "
                    "WHERE kind = :k AND value = :v AND tenant_id IS NULL"
                ),
                {"k": CLAIM_PREDICATE_KIND, "v": name},
            )
        ).scalar_one()
    assert rows == 1


@pytest.mark.asyncio
async def test_a_deprecated_global_predicate_stops_validating(
    globals_: GlobalVocabularyService, tenant_vocab: VocabularyService, tenant: uuid.UUID
) -> None:
    """Retired, not removed. The row stays because claims still reference it
    and a deprecated predicate must still explain what they meant."""
    name = _name()
    await globals_.create_predicate(
        value=name, value_type="string", claim_category="dependency", definition="x"
    )
    await globals_.deprecate_predicate(value=name)

    with pytest.raises(VocabularyError, match="deprecated"):
        await tenant_vocab.validate_value(_ctx(tenant), CLAIM_PREDICATE_KIND, name)


@pytest.mark.asyncio
async def test_deprecation_preserves_the_first_timestamp(
    globals_: GlobalVocabularyService
) -> None:
    """When a term was retired is a fact about the vocabulary, not about the
    last time somebody called this."""
    name = _name()
    await globals_.create_predicate(
        value=name, value_type="string", claim_category="dependency", definition="x"
    )
    first = await globals_.deprecate_predicate(value=name)
    again = await globals_.deprecate_predicate(value=name)

    assert first.deprecated_at == again.deprecated_at


# --- collision, both directions ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_tenant_cannot_redefine_a_global_predicate(
    globals_: GlobalVocabularyService, tenant_vocab: VocabularyService, tenant: uuid.UUID
) -> None:
    """Shadowing is the failure. The same name meaning two things makes claims
    from two tenants look comparable when they are not."""
    name = _name()
    await globals_.create_predicate(
        value=name, value_type="entity_ref", claim_category="dependency", definition="x"
    )

    with pytest.raises(ConflictError, match="organization scope"):
        await tenant_vocab.add_value(_ctx(tenant), CLAIM_PREDICATE_KIND, name)


@pytest.mark.asyncio
async def test_a_tenant_cannot_reuse_a_deprecated_global_name(
    globals_: GlobalVocabularyService, tenant_vocab: VocabularyService, tenant: uuid.UUID
) -> None:
    """Deprecated is included deliberately: the name carries its old meaning in
    every claim already written against it, and reuse would retype those."""
    name = _name()
    await globals_.create_predicate(
        value=name, value_type="entity_ref", claim_category="dependency", definition="x"
    )
    await globals_.deprecate_predicate(value=name)

    with pytest.raises(ConflictError):
        await tenant_vocab.add_value(_ctx(tenant), CLAIM_PREDICATE_KIND, name)


@pytest.mark.asyncio
async def test_an_operator_cannot_promote_a_name_a_tenant_already_uses(
    globals_: GlobalVocabularyService, factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """The harder direction, and the one that matters.

    Promoting a term while a tenant means something else by it would silently
    retype every claim they have already written. The local term has to be
    reconciled first, never absorbed.
    """
    name = _name()
    await _add_local(factory, tenant, name)

    with pytest.raises(ConflictError, match="already exists locally"):
        await globals_.create_predicate(
            value=name, value_type="entity_ref", claim_category="dependency", definition="x"
        )


@pytest.mark.asyncio
async def test_a_global_predicate_cannot_be_defined_twice(
    globals_: GlobalVocabularyService
) -> None:
    name = _name()
    await globals_.create_predicate(
        value=name, value_type="string", claim_category="dependency", definition="x"
    )
    with pytest.raises(ConflictError, match="already exists"):
        await globals_.create_predicate(
            value=name, value_type="integer", claim_category="dependency", definition="y"
        )


# --- what a predicate must declare --------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_value_type_is_refused(globals_: GlobalVocabularyService) -> None:
    with pytest.raises(ValidationError, match="unknown value type"):
        await globals_.create_predicate(
            value=_name(), value_type="duration", claim_category="dependency", definition="x"
        )


@pytest.mark.asyncio
async def test_prose_is_refused_outside_the_one_category_that_allows_it(
    globals_: GlobalVocabularyService
) -> None:
    """A predicate accepting prose accepts anything, and a claim whose value is
    a paragraph cannot be compared or contradicted."""
    with pytest.raises(ValidationError, match="session-summary"):
        await globals_.create_predicate(
            value=_name(), value_type="prose", claim_category="dependency", definition="x"
        )


@pytest.mark.asyncio
async def test_prose_is_permitted_for_session_summary(
    globals_: GlobalVocabularyService
) -> None:
    await globals_.create_predicate(
        value=_name(), value_type="prose", claim_category="session_summary", definition="x"
    )


@pytest.mark.asyncio
async def test_a_predicate_without_a_definition_is_refused(
    globals_: GlobalVocabularyService
) -> None:
    """An undefined term is how two tenants end up meaning different things by
    one name, which is the situation this replaces."""
    with pytest.raises(ValidationError, match="definition"):
        await globals_.create_predicate(
            value=_name(), value_type="string", claim_category="dependency", definition="   "
        )


# --- the keyhole stays a keyhole -------------------------------------------------------


@pytest.mark.asyncio
async def test_no_other_vocabulary_kind_may_be_global(
    factory: async_sessionmaker[AsyncSession]
) -> None:
    """The nullable column exists for exactly one kind. Without this the
    nullability would silently apply to every vocabulary in the system."""
    with pytest.raises((ValueError, Exception)):
        async with factory() as session, session.begin():
            session.add(
                VocabularyValue(
                    vocab_id=uuid.uuid4(),
                    tenant_id=None,
                    kind="edge_rel",
                    value="global_edge",
                    is_system=False,
                    deprecated_at=None,
                    created_at=_NOW,
                )
            )


@pytest.mark.asyncio
async def test_the_inventory_shows_local_divergence(
    globals_: GlobalVocabularyService, factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Governance needs to see which local terms exist and where, to decide
    what should become global."""
    name = _name()
    await _add_local(factory, tenant, name)

    inventory = await globals_.local_predicate_inventory()

    assert (tenant, name) in inventory


@pytest.mark.asyncio
async def test_deprecating_an_undefined_predicate_is_not_found(
    globals_: GlobalVocabularyService
) -> None:
    with pytest.raises(NotFoundError):
        await globals_.deprecate_predicate(value=_name())
