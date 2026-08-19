"""Every entity write is checked against the profile its tenant is bound to.

The bypass this file guards is not a missing check — it is a check that ran on
one path and not the others. Property validation used to happen only when a
caller named a capability-specific type, so a generic entity, a sync write and a
promoted claim each wrote whatever they were handed while a capability write
next to them was refused. A profile that four of five writers ignore is not
governance, it is documentation.

So the tests here come in two halves. The first half proves the rules: what an
`active` binding refuses, what a `validating` binding merely reports, and what an
unbound tenant is still allowed to do. The second half proves the *coverage* —
that the writers actually resolve through the seam, and that the structural gate
which keeps new writers honest reports the shipped tree as clean while failing on
a writer that bypasses it.

Two things are deliberately asserted about mode rather than about content.
Advisory is not a weaker set of rules, it is the same rules not enforced: the
same write that an active binding refuses must come back from a validating
binding carrying the identical violation and having been allowed. A test that
only checked "advisory does not raise" would pass for an advisory mode that
checked nothing at all.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.entities import validation as entity_validation
from contextplane.entities.validation import (
    ADVISORY,
    MANDATORY,
    MAX_REPORTED_VIOLATIONS,
    UNBOUND,
    EntityValidator,
)
from contextplane.exceptions import ValidationError
from contextplane.profile.compiler import compile_profile
from contextplane.profile.schemas.common import PropertyDefinition
from contextplane.profile.schemas.entity import EntityTypeDefinition
from contextplane.service.catalog import attribute_writes
from contextplane.service.catalog.schema import SchemaService
from contextplane.types import TenantContext
from scripts.check_profile_write_coverage import FIXED_KEYS, REGISTRY, VALIDATED, check, read_writer

_NAMESPACE = "northwind"
_WAREHOUSE = f"{_NAMESPACE}:warehouse"
_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)


class _FixedClock:
    def now(self) -> datetime.datetime:
        return _NOW


def _profile_document() -> str:
    """One entity type with a required property, a typed one, and an extension point."""
    warehouse = EntityTypeDefinition(
        namespace=_NAMESPACE,
        type_name="warehouse",
        properties=(
            PropertyDefinition(name="region", value_type="string", required=True, min_cardinality=1),
            PropertyDefinition(name="capacity", value_type="integer"),
            PropertyDefinition(name="channel", value_type="enum", enum_values=("air", "sea")),
        ),
        extension_points=("local_code",),
    )
    return compile_profile(entities=[warehouse], relationships=[], interfaces=[]).document


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _tenant(session: AsyncSession) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'enforcement')"),
        {"t": tenant_id, "s": f"pe-{tenant_id.hex[:10]}"},
    )
    return tenant_id


async def _bind(session: AsyncSession, tenant_id: uuid.UUID, *, state: str, document: str | None = None) -> uuid.UUID:
    """Publish a revision and bind this tenant to it in `state`."""
    revision_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO profile_revisions ("
            "  profile_revision_id, profile_family, profile_name, semantic_version,"
            "  canonical_document, document_digest, compatibility, published_by, published_at"
            ") VALUES (:rid, 'platform', :name, '1.0.0', CAST(:doc AS JSONB), :digest,"
            "          'backward_compatible', 'conformance', :now)"
        ),
        {
            "rid": revision_id,
            "name": f"enforcement-{revision_id.hex[:12]}",
            "doc": document if document is not None else _profile_document(),
            "digest": revision_id.hex,
            "now": _NOW,
        },
    )
    await session.execute(
        text(
            "INSERT INTO profile_bindings ("
            "  binding_id, tenant_id, profile_revision_id, extension_set_digest, state,"
            "  effective_from, actor, reason, recorded_at"
            ") VALUES (:bid, :tid, :rid, :digest, :state, :now, 'conformance', 'test', :now)"
        ),
        {
            "bid": uuid.uuid4(),
            "tid": tenant_id,
            "rid": revision_id,
            "digest": revision_id.hex,
            "state": state,
            "now": _NOW,
        },
    )
    return revision_id


async def _validate(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    attributes: dict[str, object],
    *,
    entity_type: str = _WAREHOUSE,
) -> entity_validation.EntityValidationResult:
    return await EntityValidator(factory).validate(tenant_id=tenant_id, entity_type=entity_type, attributes=attributes)


def _codes(result: entity_validation.EntityValidationResult) -> list[str]:
    return sorted(violation.code for violation in result.violations)


# --- what the profile says --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tenant_with_no_binding_is_unbound_and_unconstrained(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deployment that has adopted no profile keeps writing as it always did."""
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)

    result = await _validate(factory, tenant_id, {"anything": "at all"}, entity_type="unknown:type")

    assert result.mode == UNBOUND
    assert result.violations == ()
    assert result.profile_revision_id is None
    assert result.valid


@pytest.mark.asyncio
async def test_an_active_binding_refuses_a_property_the_type_never_declared(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        revision_id = await _bind(session, tenant_id, state="active")

    result = await _validate(factory, tenant_id, {"region": "eu-west", "surprise": "x"})

    assert result.mode == MANDATORY
    assert _codes(result) == ["undeclared_property"]
    assert result.violations[0].property_name == "surprise"
    assert result.profile_revision_id == revision_id
    assert not result.valid


@pytest.mark.asyncio
async def test_a_declared_extension_point_is_not_an_undeclared_property(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Naming the points is the tenant's permission to add to a shared type."""
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")

    result = await _validate(factory, tenant_id, {"region": "eu-west", "local_code": "W-1"})

    assert result.violations == ()
    assert result.valid


@pytest.mark.asyncio
async def test_an_active_binding_refuses_an_undeclared_entity_type(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")

    result = await _validate(factory, tenant_id, {}, entity_type=f"{_NAMESPACE}:no_such_type")

    assert _codes(result) == ["unknown_entity_type"]
    assert not result.valid


@pytest.mark.asyncio
async def test_a_required_property_left_out_is_a_violation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")

    result = await _validate(factory, tenant_id, {"capacity": 10})

    assert _codes(result) == ["missing_required_property"]
    assert result.violations[0].property_name == "region"


@pytest.mark.asyncio
async def test_a_value_of_the_wrong_declared_type_is_a_violation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")

    result = await _validate(factory, tenant_id, {"region": "eu-west", "capacity": "ten"})

    assert _codes(result) == ["wrong_value_type"]
    assert result.violations[0].property_name == "capacity"


@pytest.mark.asyncio
async def test_a_boolean_does_not_satisfy_an_integer_property(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`bool` is an `int` in Python, so `True` would pass a plain isinstance check."""
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")

    result = await _validate(factory, tenant_id, {"region": "eu-west", "capacity": True})

    assert _codes(result) == ["wrong_value_type"]


@pytest.mark.asyncio
async def test_a_value_outside_a_declared_enum_is_a_violation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")

    result = await _validate(factory, tenant_id, {"region": "eu-west", "channel": "rail"})

    assert _codes(result) == ["value_not_in_enum"]


# --- advisory is the same rules, unenforced ---------------------------------------


@pytest.mark.asyncio
async def test_a_validating_binding_reports_the_identical_violation_and_allows_the_write(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Advisory must find what mandatory finds; only the consequence differs.

    Both halves are asserted against one input. A test that checked only "advisory
    does not raise" would pass for an advisory mode that ran no checks at all,
    which is the failure worth ruling out — a validation window that reports
    nothing looks exactly like a clean tenant.
    """
    offending = {"region": "eu-west", "surprise": "x"}

    async with factory() as session, session.begin():
        strict_tenant = await _tenant(session)
        await _bind(session, strict_tenant, state="active")
        lenient_tenant = await _tenant(session)
        await _bind(session, lenient_tenant, state="validating")

    strict = await _validate(factory, strict_tenant, offending)
    lenient = await _validate(factory, lenient_tenant, offending)

    assert strict.mode == MANDATORY
    assert lenient.mode == ADVISORY
    assert _codes(strict) == _codes(lenient) == ["undeclared_property"]
    assert not strict.valid
    assert lenient.valid


@pytest.mark.asyncio
async def test_an_active_binding_governs_even_while_another_profile_is_validating(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant trying out its next profile is still governed by its current one.

    Reporting against the candidate while enforcing nothing would leave the live
    profile unenforced for the whole validation window, which is the opposite of
    what a validation window is for.
    """
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        active_revision = await _bind(session, tenant_id, state="active")
        await _bind(session, tenant_id, state="validating")

    result = await _validate(factory, tenant_id, {"region": "eu-west", "surprise": "x"})

    assert result.mode == MANDATORY
    assert result.profile_revision_id == active_revision


# --- results are bounded -----------------------------------------------------------


@pytest.mark.asyncio
async def test_violations_are_capped_and_the_result_says_it_truncated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An error body echoing one violation per undeclared property is not safe to return."""
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")

    flood = {f"junk_{index:03d}": index for index in range(MAX_REPORTED_VIOLATIONS * 3)}
    flood["region"] = "eu-west"

    result = await _validate(factory, tenant_id, flood)

    assert len(result.violations) == MAX_REPORTED_VIOLATIONS
    assert result.truncated


@pytest.mark.asyncio
async def test_a_result_within_the_cap_is_not_marked_truncated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The flag has to distinguish a short list from a cut one, or it says nothing."""
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")

    result = await _validate(factory, tenant_id, {"region": "eu-west", "surprise": "x"})

    assert len(result.violations) == 1
    assert not result.truncated


# --- the writers actually resolve through it ---------------------------------------


@pytest.mark.asyncio
async def test_schema_service_refuses_a_profile_violation_for_a_type_with_no_registered_schema(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A schema-free type is not an unchecked type.

    `entity_type_schemas` holds a tenant's own JSON Schema and most types have
    none. If the profile check sat behind that lookup, every type without a
    registered schema would be exactly as unvalidated as before this existed.
    """
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")

    service = SchemaService(factory, _FixedClock(), validator=EntityValidator(factory))
    ctx = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["admin"])

    with pytest.raises(ValidationError, match="profile"):
        await service.validate_entity_attributes(ctx, _WAREHOUSE, {"region": "eu-west", "surprise": "x"})


@pytest.mark.asyncio
async def test_schema_service_reports_an_advisory_violation_as_a_warning(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="validating")

    service = SchemaService(factory, _FixedClock(), validator=EntityValidator(factory))
    ctx = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["admin"])

    result = await service.validate_entity_attributes(ctx, _WAREHOUSE, {"region": "eu-west", "surprise": "x"})

    assert result.valid
    assert any("undeclared_property" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_schema_service_without_a_validator_still_validates_its_own_registry(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The profile check is additive; wiring no validator must not disable the registry."""
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")

    service = SchemaService(factory, _FixedClock())
    ctx = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["admin"])

    result = await service.validate_entity_attributes(ctx, _WAREHOUSE, {"surprise": "x"})

    assert result.valid
    assert result.warnings == []


@pytest.mark.asyncio
async def test_the_promotion_writer_refuses_an_attribute_the_profile_never_declared(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A promoted claim lands in the canonical graph and must be governed there too.

    The predicate vocabulary check that already ran says the *predicate* is
    current; it says nothing about whether this entity's type declares it.
    """
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")
        entity_id = await _entity(session, tenant_id)
        await _predicate(session, "surprise")

    with pytest.raises(ValidationError, match="profile"):
        async with factory() as session, session.begin():
            await attribute_writes.write_attribute(
                session,
                tenant_id=tenant_id,
                entity_id=entity_id,
                key="surprise",
                value="x",
                valid_from=_NOW,
                valid_to=None,
                actor_id=uuid.uuid4(),
            )


@pytest.mark.asyncio
async def test_the_promotion_writer_accepts_a_declared_property(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The negative test above proves nothing unless the positive path still works."""
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")
        entity_id = await _entity(session, tenant_id)
        actor_id = await _actor(session, tenant_id)
        await _predicate(session, "region")

    async with factory() as session, session.begin():
        attr_id, superseded, _ = await attribute_writes.write_attribute(
            session,
            tenant_id=tenant_id,
            entity_id=entity_id,
            key="region",
            value="eu-west",
            valid_from=_NOW,
            valid_to=None,
            actor_id=actor_id,
        )

    assert attr_id is not None
    assert superseded is None


@pytest.mark.asyncio
async def test_the_promotion_writer_does_not_demand_properties_it_is_not_writing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A promotion writes one attribute and cannot be asked to complete the entity.

    `region` is required by the type. Writing `capacity` alone must not fail for
    the absence of `region` — otherwise no promotion could ever write the second
    property of a type that requires a first.
    """
    async with factory() as session, session.begin():
        tenant_id = await _tenant(session)
        await _bind(session, tenant_id, state="active")
        entity_id = await _entity(session, tenant_id)
        actor_id = await _actor(session, tenant_id)
        await _predicate(session, "capacity")

    async with factory() as session, session.begin():
        attr_id, _, _ = await attribute_writes.write_attribute(
            session,
            tenant_id=tenant_id,
            entity_id=entity_id,
            key="capacity",
            value=10,
            valid_from=_NOW,
            valid_to=None,
            actor_id=actor_id,
        )

    assert attr_id is not None


# --- coverage of the writers is enforced structurally -------------------------------


def test_the_shipped_tree_has_no_unregistered_or_bypassing_entity_writer() -> None:
    """The gate's own verdict on the tree it ships with."""
    assert check() == []


def test_every_registered_validating_writer_actually_reaches_the_validator() -> None:
    """Read the modules rather than trusting the registry's own claim about them.

    The registry says which writers validate. This asserts the syntax trees agree,
    so an entry cannot outlive the call it describes.
    """
    repo_root = _repo_root()
    for entry in REGISTRY:
        if entry.kind != VALIDATED:
            continue
        path = repo_root / entry.path
        facts = read_writer(path, path.read_text(encoding="utf-8"))
        assert facts.writes, f"{entry.path} is registered as a writer but writes nothing"
        assert facts.validates, f"{entry.path} is registered as validating but reaches no validator"


def test_every_fixed_key_writer_writes_only_the_keys_its_entry_names() -> None:
    """A fixed-key entry is a claim about the module, checked against the module."""
    repo_root = _repo_root()
    for entry in REGISTRY:
        if entry.kind != FIXED_KEYS:
            continue
        path = repo_root / entry.path
        facts = read_writer(path, path.read_text(encoding="utf-8"))
        assert not facts.has_dynamic_key, f"{entry.path} writes a caller-nameable key under a fixed-key entry"
        assert facts.fixed_keys <= entry.keys, f"{entry.path} writes keys outside its entry: {facts.fixed_keys}"


def test_the_gate_reports_a_writer_that_neither_validates_nor_is_registered() -> None:
    """The gate has to be able to fail, or a green run means nothing.

    Exercised against a synthesized module rather than by mutating the tree: the
    check walks the real repository, so this asserts the reader's verdict on a
    bypassing writer, which is the decision the walk then acts on.
    """
    source = (
        "from contextplane.storage.models import Attribute\n"
        "def write(session, key, value):\n"
        "    session.add(Attribute(key=key, value=value))\n"
    )
    facts = read_writer(_repo_root() / "contextplane" / "does_not_exist.py", source)

    assert facts.writes
    assert not facts.validates
    assert facts.has_dynamic_key


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


async def _entity(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    entity_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO entities (entity_id, tenant_id, entity_type, name, is_active, created_at)"
            " VALUES (:eid, :tid, :etype, :name, TRUE, :now)"
        ),
        {"eid": entity_id, "tid": tenant_id, "etype": _WAREHOUSE, "name": f"w-{entity_id.hex[:8]}", "now": _NOW},
    )
    return entity_id


async def _actor(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """A real actor row, because `attributes.created_by` is a foreign key to one."""
    actor_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO actors (actor_id, tenant_id, oidc_subject, created_at)" " VALUES (:aid, :tid, :sub, :now)"),
        {"aid": actor_id, "tid": tenant_id, "sub": f"conformance-{actor_id.hex[:12]}", "now": _NOW},
    )
    return actor_id


async def _predicate(session: AsyncSession, value: str) -> None:
    """Register the claim predicate the promotion writer's vocabulary check demands.

    The metadata columns are not decoration: a `claim_predicate` row without a
    value type, category, definition and cardinality is refused by a check
    constraint, because a predicate nobody defined cannot be reasoned about.
    """
    from contextplane.storage.models import CLAIM_PREDICATE_KIND

    await session.execute(
        text(
            "INSERT INTO vocabulary_values"
            " (vocab_id, tenant_id, kind, value, value_type, claim_category, definition,"
            "  value_cardinality, created_at)"
            " VALUES (:vid, NULL, :kind, :value, 'string', 'attribute', :definition, 'single', :now)"
            " ON CONFLICT DO NOTHING"
        ),
        {
            "vid": uuid.uuid4(),
            "kind": CLAIM_PREDICATE_KIND,
            "value": value,
            "definition": f"conformance fixture predicate {value!r}",
            "now": _NOW,
        },
    )
