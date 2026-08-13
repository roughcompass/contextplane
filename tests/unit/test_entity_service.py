"""Unit tests for EntityService.update_entity schema validation.

`update_entity` must validate the merged attribute state (existing open rows
overlaid by the patch delta) against the entity_type schema before writing
any attribute rows. A validation failure must raise ValidationError before
any DB writes occur — the same failure shape create_entity uses.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.exceptions import ValidationError
from contextplane.service.catalog.entity import EntityService
from contextplane.service.catalog.schema import ValidationResult
from contextplane.storage.models import Attribute, Entity
from tests.helpers.context import random_producer_ctx as _ctx

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 5, 11, 12, 0, 0, tzinfo=datetime.UTC)


def _entity_row(tenant_id: uuid.UUID, entity_type: str = "api-capability") -> Entity:
    e = Entity()
    e.entity_id = uuid.uuid4()
    e.tenant_id = tenant_id
    e.entity_type = entity_type
    e.name = "test-cap"
    e.external_id = None
    e.is_active = True
    e.created_at = _NOW
    e.created_by = None
    return e


def _attr_row(tenant_id: uuid.UUID, entity_id: uuid.UUID, key: str, value: object) -> Attribute:
    a = Attribute()
    a.attr_id = uuid.uuid4()
    a.tenant_id = tenant_id
    a.entity_id = entity_id
    a.key = key
    a.value = value
    a.t_valid_from = _NOW - datetime.timedelta(days=1)
    a.t_valid_to = None
    a.t_ingested_at = _NOW - datetime.timedelta(days=1)
    a.t_invalidated_at = None
    a.created_by = None
    return a


def _build_service(
    entity: Entity,
    existing_attrs: list[Attribute],
    schema_mock: MagicMock,
) -> tuple[EntityService, AsyncMock]:
    """Build EntityService with a mocked session and a caller-controlled schema mock.

    The session supports one `session.execute` call that returns all existing
    open attribute rows. `session.get` returns the entity row.
    """
    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=iter(existing_attrs))

    session = AsyncMock()
    session.get = AsyncMock(return_value=entity)
    session.execute = AsyncMock(return_value=execute_result)
    session.add = MagicMock()

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)

    session_factory = MagicMock(return_value=session_cm)

    frozen_clock = MagicMock()
    frozen_clock.now = MagicMock(return_value=_NOW)

    svc = EntityService(
        session_factory=session_factory,
        clock=frozen_clock,
        vocabulary=MagicMock(),
        schema=schema_mock,
    )
    return svc, session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_entity_valid_attributes_passes() -> None:
    """update_entity succeeds when validate_capability returns a clean result."""
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id, entity_type="api-capability")
    existing = [_attr_row(ctx.tenant_id, entity.entity_id, "owner", "team-a")]

    schema_mock = MagicMock()
    schema_mock.validate_capability = AsyncMock(return_value=ValidationResult(valid=True, warnings=[]))

    svc, _session = _build_service(entity, existing, schema_mock)

    result = await svc.update_entity(ctx, entity.entity_id, {"owner": "team-b"})

    assert result.entity_id == entity.entity_id
    # validate_capability must have been called once with the merged attributes.
    schema_mock.validate_capability.assert_awaited_once()
    call_args = schema_mock.validate_capability.call_args
    assert call_args.args[1] == "api-capability"  # capability_type == entity_type
    assert call_args.args[2] == {"owner": "team-b"}  # merged: existing overridden by update


@pytest.mark.asyncio
async def test_update_entity_merged_attributes_sent_to_validator() -> None:
    """Merged attributes (existing + updates) reach validate_capability, not just the delta."""
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id, entity_type="api-capability")
    existing = [
        _attr_row(ctx.tenant_id, entity.entity_id, "owner", "team-a"),
        _attr_row(ctx.tenant_id, entity.entity_id, "version", "1.0.0"),
    ]

    schema_mock = MagicMock()
    schema_mock.validate_capability = AsyncMock(return_value=ValidationResult(valid=True, warnings=[]))

    svc, _ = _build_service(entity, existing, schema_mock)

    await svc.update_entity(ctx, entity.entity_id, {"owner": "team-b"})

    call_args = schema_mock.validate_capability.call_args
    merged = call_args.args[2]
    # The un-updated key must also appear in the merged payload.
    assert merged["version"] == "1.0.0"
    assert merged["owner"] == "team-b"


@pytest.mark.asyncio
async def test_update_entity_invalid_attributes_raises_validation_error() -> None:
    """update_entity raises ValidationError when validate_capability raises one."""
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id, entity_type="api-capability")
    existing = [_attr_row(ctx.tenant_id, entity.entity_id, "owner", "team-a")]

    schema_mock = MagicMock()
    schema_mock.validate_capability = AsyncMock(
        side_effect=ValidationError("capability attributes failed schema validation for type 'api-capability': ...")
    )

    svc, session = _build_service(entity, existing, schema_mock)

    with pytest.raises(ValidationError):
        await svc.update_entity(ctx, entity.entity_id, {"owner": 999})  # type mismatch

    # No attribute rows must have been written when validation fails.
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_update_entity_no_rows_written_on_validation_failure() -> None:
    """Attribute rows must not be partially written when validate_capability raises."""
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id, entity_type="api-capability")
    existing = [
        _attr_row(ctx.tenant_id, entity.entity_id, "k1", "v1"),
        _attr_row(ctx.tenant_id, entity.entity_id, "k2", "v2"),
    ]

    schema_mock = MagicMock()
    schema_mock.validate_capability = AsyncMock(side_effect=ValidationError("schema violation"))

    svc, session = _build_service(entity, existing, schema_mock)

    with pytest.raises(ValidationError):
        await svc.update_entity(ctx, entity.entity_id, {"k1": "new", "k2": "new"})

    # No closes or inserts should have happened.
    session.add.assert_not_called()
    for attr in existing:
        assert attr.t_valid_to is None, f"Attribute '{attr.key}' was incorrectly closed on validation failure."


@pytest.mark.asyncio
async def test_update_entity_validate_capability_called_with_entity_type() -> None:
    """validate_capability receives the entity's entity_type as capability_type."""
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id, entity_type="ml-model")
    existing: list[Attribute] = []

    schema_mock = MagicMock()
    schema_mock.validate_capability = AsyncMock(return_value=ValidationResult(valid=True, warnings=[]))

    svc, _ = _build_service(entity, existing, schema_mock)

    await svc.update_entity(ctx, entity.entity_id, {"framework": "pytorch"})

    call_args = schema_mock.validate_capability.call_args
    assert call_args.args[1] == "ml-model"


# ---------------------------------------------------------------------------
# create_entity: the generic path is validated too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entity_validates_against_entity_type_with_no_capability_type() -> None:
    """A generic create is validated, not waved through for lacking a capability_type.

    This is the bypass the profile-enforcement seam closes. Validation used to run
    only when the caller supplied a `capability_type`, so an entity created without
    one carried no property rules at all — while `update_entity` on that same row
    validated against its `entity_type` and would refuse what the create accepted.
    """
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id, entity_type="api-capability")

    schema_mock = MagicMock()
    schema_mock.validate_capability = AsyncMock(return_value=ValidationResult(valid=True, warnings=[]))
    svc, _session = _build_service(entity, [], schema_mock)
    svc._vocabulary.validate_value = AsyncMock(return_value=None)  # noqa: SLF001

    await svc.create_entity(ctx, entity_type="api-capability", name="test-cap", attributes={"owner": "team-a"})

    schema_mock.validate_capability.assert_awaited_once()
    call_args = schema_mock.validate_capability.call_args
    assert call_args.args[1] == "api-capability"
    assert call_args.args[2] == {"owner": "team-a"}


@pytest.mark.asyncio
async def test_create_entity_does_not_validate_the_same_type_twice() -> None:
    """A capability_type equal to the entity_type is one type, so it is checked once.

    Worth pinning because the obvious way to close the generic bypass — add an
    unconditional call beside the existing one — makes every capability create do
    the same work and emit the same warnings twice.
    """
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id, entity_type="api-capability")

    schema_mock = MagicMock()
    schema_mock.validate_capability = AsyncMock(return_value=ValidationResult(valid=True, warnings=[]))
    svc, _session = _build_service(entity, [], schema_mock)
    svc._vocabulary.validate_value = AsyncMock(return_value=None)  # noqa: SLF001

    await svc.create_entity(
        ctx,
        entity_type="api-capability",
        name="test-cap",
        capability_type="api-capability",
        attributes={"owner": "team-a"},
    )

    schema_mock.validate_capability.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_entity_validates_both_types_when_they_differ() -> None:
    """A distinct capability_type carries its own schema and is checked as well."""
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id, entity_type="service")

    schema_mock = MagicMock()
    schema_mock.validate_capability = AsyncMock(return_value=ValidationResult(valid=True, warnings=[]))
    svc, _session = _build_service(entity, [], schema_mock)
    svc._vocabulary.validate_value = AsyncMock(return_value=None)  # noqa: SLF001

    await svc.create_entity(
        ctx,
        entity_type="service",
        name="test-cap",
        capability_type="api-capability",
        attributes={"owner": "team-a"},
    )

    assert schema_mock.validate_capability.await_count == 2
    checked = [call.args[1] for call in schema_mock.validate_capability.await_args_list]
    assert checked == ["service", "api-capability"]


@pytest.mark.asyncio
async def test_create_entity_refuses_when_the_entity_type_validation_raises() -> None:
    """The generic check must be able to stop a create, not merely be called."""
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id, entity_type="api-capability")

    schema_mock = MagicMock()
    schema_mock.validate_capability = AsyncMock(side_effect=ValidationError("undeclared_property: surprise"))
    svc, session = _build_service(entity, [], schema_mock)
    svc._vocabulary.validate_value = AsyncMock(return_value=None)  # noqa: SLF001

    with pytest.raises(ValidationError, match="undeclared_property"):
        await svc.create_entity(ctx, entity_type="api-capability", name="test-cap", attributes={"surprise": "x"})

    session.add.assert_not_called()
