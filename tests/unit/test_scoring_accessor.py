"""The fallback chain, in both directions, and the case where it refuses.

Both directions matter and they fail differently. An accessor that never finds an
override makes every tenant's governance a no-op that looks like it worked; one
that always finds an override scores every tenant by somebody else's weights.
Testing only the first is the easy mistake, because on a deployment with no
extensions it passes either way.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane import ranking
from contextplane.profile.bindings import extension_set_digest
from contextplane.profile.scoring import (
    SCORING_NAMESPACE,
    SOURCE_CORE,
    SOURCE_EXTENSION,
    ScoringOverrideRefused,
    resolve_weights,
)

_MODEL = "salience-weights@1"
_TENANT = uuid.uuid4()
_CORE_REVISION = uuid.uuid4()


def _extension(extension_id: uuid.UUID, document: dict[str, Any]) -> Any:
    return MagicMock(extension_revision_id=extension_id, canonical_document=document)


def _session(*, binding: Any | None, extensions: list[Any] | None = None) -> AsyncMock:
    """A session answering the accessor's two queries and refusing a third.

    Refusing rather than returning empty: a query this fake does not recognise is
    a query the accessor grew without anybody deciding what it should return, and
    an empty answer would let that land looking correct.
    """
    rows = extensions or []

    async def execute(statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        result = MagicMock()
        if "FROM profile_bindings" in sql:
            result.one_or_none = MagicMock(return_value=binding)
            return result
        if "FROM profile_extensions" in sql:
            assert params is not None
            assert params["ns"] == SCORING_NAMESPACE
            result.all = MagicMock(return_value=rows)
            return result
        raise AssertionError(f"unexpected SQL in the scoring accessor: {sql}")

    session = AsyncMock()
    session.execute = execute
    return session


@pytest.mark.asyncio
async def test_a_tenant_with_no_binding_gets_the_committed_core() -> None:
    """The common case, and not a degraded one. Most tenants have no extension
    and never will."""
    resolved = await resolve_weights(_session(binding=None), tenant_id=_TENANT, model_id=_MODEL)
    assert resolved.value == ranking.weights(_MODEL)
    assert resolved.source == SOURCE_CORE


@pytest.mark.asyncio
async def test_a_bound_extension_overrides_the_core() -> None:
    """The other direction. An accessor that never finds an override makes every
    tenant's published governance a no-op that looks like it worked."""
    extension_id = uuid.uuid4()
    override = {"state_change": 0.5, "outcome_decisive": 0.5}
    session = _session(
        binding=MagicMock(
            profile_revision_id=_CORE_REVISION, extension_set_digest=extension_set_digest([extension_id])
        ),
        extensions=[_extension(extension_id, {"magnitudes": {_MODEL: override}})],
    )

    resolved = await resolve_weights(session, tenant_id=_TENANT, model_id=_MODEL)
    assert resolved.value == override
    assert resolved.source == SOURCE_EXTENSION
    assert resolved.value != ranking.weights(_MODEL)


@pytest.mark.asyncio
async def test_the_resolved_value_says_where_it_came_from() -> None:
    """A caller logging 'tenant X scored with W' and unable to say whether W was
    theirs has recorded the number and lost the fact that matters."""
    resolved = await resolve_weights(_session(binding=None), tenant_id=_TENANT, model_id=_MODEL)
    assert resolved.source in (SOURCE_CORE, SOURCE_EXTENSION)
    assert resolved.model_id == _MODEL


@pytest.mark.asyncio
async def test_a_bound_extension_that_names_another_magnitude_leaves_this_one_alone() -> None:
    """An extension overrides what it names and nothing else, which is what makes
    core the default rather than a fallback."""
    extension_id = uuid.uuid4()
    session = _session(
        binding=MagicMock(
            profile_revision_id=_CORE_REVISION, extension_set_digest=extension_set_digest([extension_id])
        ),
        extensions=[_extension(extension_id, {"magnitudes": {"entity-search-hybrid-fusion@1": {"semantic": 1.0}}})],
    )

    resolved = await resolve_weights(session, tenant_id=_TENANT, model_id=_MODEL)
    assert resolved.value == ranking.weights(_MODEL)
    assert resolved.source == SOURCE_CORE


@pytest.mark.asyncio
async def test_a_binding_whose_extension_set_does_not_verify_is_refused() -> None:
    """The case that must not fall back quietly.

    A digest mismatch means the extensions found are not the ones the binding
    activated. Scoring with them applies an override nobody bound; scoring
    without them presents that as normal operation. Both are worse than stopping.
    """
    session = _session(
        binding=MagicMock(profile_revision_id=_CORE_REVISION, extension_set_digest="a-digest-of-something-else"),
        extensions=[_extension(uuid.uuid4(), {"magnitudes": {_MODEL: {"state_change": 1.0}}})],
    )

    with pytest.raises(ScoringOverrideRefused, match="do not digest to the set this binding activated"):
        await resolve_weights(session, tenant_id=_TENANT, model_id=_MODEL)


@pytest.mark.asyncio
async def test_a_binding_with_no_extensions_verifies_and_resolves_to_core() -> None:
    """`bound to core with no extensions` is a real configuration, and the empty
    set has a digest precisely so it does not read as three-valued."""
    session = _session(
        binding=MagicMock(profile_revision_id=_CORE_REVISION, extension_set_digest=extension_set_digest([])),
        extensions=[],
    )

    resolved = await resolve_weights(session, tenant_id=_TENANT, model_id=_MODEL)
    assert resolved.source == SOURCE_CORE


@pytest.mark.asyncio
async def test_two_extensions_overriding_one_magnitude_are_refused() -> None:
    """Whichever won would depend on a row order nothing pins, so a tenant would
    get a different weighting depending on how the planner felt."""
    first, second = uuid.uuid4(), uuid.uuid4()
    session = _session(
        binding=MagicMock(
            profile_revision_id=_CORE_REVISION, extension_set_digest=extension_set_digest([first, second])
        ),
        extensions=[
            _extension(first, {"magnitudes": {_MODEL: {"state_change": 1.0}}}),
            _extension(second, {"magnitudes": {_MODEL: {"state_change": 0.1}}}),
        ],
    )

    with pytest.raises(ScoringOverrideRefused, match="more than one bound extension"):
        await resolve_weights(session, tenant_id=_TENANT, model_id=_MODEL)


@pytest.mark.asyncio
async def test_an_unknown_magnitude_is_refused_by_the_registry_not_invented_here() -> None:
    """The accessor resolves values for governed magnitudes; it does not decide
    which magnitudes exist. A second place answering that question is a second
    registry."""
    with pytest.raises(ranking.UngovernedMagnitude):
        await resolve_weights(_session(binding=None), tenant_id=_TENANT, model_id="not-a-magnitude@1")


@pytest.mark.asyncio
async def test_extensions_are_read_only_against_the_bound_core_revision() -> None:
    """An extension targeting a core revision the tenant is not bound to has not
    been activated, and reading it would apply governance from a profile that is
    not in force."""
    captured: dict[str, Any] = {}

    async def execute(statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        result = MagicMock()
        if "FROM profile_bindings" in sql:
            result.one_or_none = MagicMock(
                return_value=MagicMock(
                    profile_revision_id=_CORE_REVISION, extension_set_digest=extension_set_digest([])
                )
            )
            return result
        captured.update(params or {})
        result.all = MagicMock(return_value=[])
        return result

    session = AsyncMock()
    session.execute = execute
    await resolve_weights(session, tenant_id=_TENANT, model_id=_MODEL)
    assert captured["core"] == _CORE_REVISION
    assert captured["tid"] == _TENANT
