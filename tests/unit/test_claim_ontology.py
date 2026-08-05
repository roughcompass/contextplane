"""Unit tests for ``seed_ontology``'s idempotent-write behavior.

``ONTOLOGY`` itself -- the static cardinality/type declarations per predicate --
is already covered by ``test_predicate_cardinality.py``, ``test_claim_compare.py``,
and ``test_confidence_decay.py``. None of those call ``seed_ontology``. This file
is the narrower gap: given a ``GlobalVocabularyService``, does seeding skip what
already exists, create what is missing with the declared fields, and block
(without creating, without raising) on a name a tenant already means something
else by.

``GlobalVocabularyService`` is mocked directly (``list_predicates`` /
``create_predicate`` as ``AsyncMock``s) rather than through a session router --
``seed_ontology`` never touches a session itself, it only calls the service, so
faking the service is the actual unit boundary here.
"""

from __future__ import annotations

import inspect
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.exceptions import ConflictError
from registry.service.catalog.global_vocabulary import (
    CARDINALITY_SINGLE,
    GlobalVocabularyService,
)
from registry.service.memory.claim_ontology import ONTOLOGY, PredicateSeed, seed_ontology


def _existing(value: str) -> MagicMock:
    row = MagicMock()
    row.value = value
    return row


def _service(*, existing: list[str] | None = None, create_side_effect: object | None = None) -> MagicMock:
    svc = MagicMock(spec=GlobalVocabularyService)
    svc.list_predicates = AsyncMock(return_value=[_existing(v) for v in (existing or [])])
    svc.create_predicate = AsyncMock(side_effect=create_side_effect)
    return svc


_SEED_A = PredicateSeed("already_here", "string", "ownership_stewardship", "a predicate the deployment already has.")
_SEED_B = PredicateSeed(
    "brand_new",
    "url",
    "operational_lifecycle",
    "a predicate this deployment is missing.",
    value_cardinality=CARDINALITY_SINGLE,
)
_SEED_C = PredicateSeed(
    "tenant_owns_this_name", "boolean", "decision_rationale", "a name a tenant already means something else by."
)


@pytest.mark.asyncio
async def test_seed_ontology_skips_a_predicate_already_present() -> None:
    svc = _service(existing=[_SEED_A.value])

    result = await seed_ontology(svc, ontology=(_SEED_A,))

    assert result.already_present == (_SEED_A.value,)
    assert result.created == ()
    assert result.blocked_by_local == ()
    svc.create_predicate.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_ontology_creates_a_missing_predicate_with_its_declared_fields() -> None:
    svc = _service(existing=[])

    result = await seed_ontology(svc, ontology=(_SEED_B,))

    assert result.created == (_SEED_B.value,)
    assert result.already_present == ()
    assert result.blocked_by_local == ()
    svc.create_predicate.assert_awaited_once_with(
        value="brand_new",
        value_type="url",
        claim_category="operational_lifecycle",
        definition=_SEED_B.definition,
        value_cardinality=CARDINALITY_SINGLE,
    )


@pytest.mark.asyncio
async def test_seed_ontology_blocks_and_logs_on_a_local_name_collision_without_creating(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tenant already means something by this name: promoting it would retype
    their claims, so the write is refused and recorded rather than either
    silently created or silently dropped."""
    svc = _service(existing=[], create_side_effect=ConflictError("already exists locally"))

    with caplog.at_level(logging.WARNING, logger="registry.service.memory.claim_ontology"):
        result = await seed_ontology(svc, ontology=(_SEED_C,))

    assert result.blocked_by_local == (_SEED_C.value,)
    assert result.created == ()
    assert result.already_present == ()
    assert "blocked_by_local_predicate" in caplog.text
    assert _SEED_C.value in caplog.text


@pytest.mark.asyncio
async def test_seed_ontology_is_additive_over_a_mixed_batch() -> None:
    """Re-running adds what is missing and touches nothing that exists -- the
    three outcomes are independent per predicate, not a single verdict for the
    whole call."""
    svc = _service(existing=[_SEED_A.value], create_side_effect=None)

    async def _create(**kwargs: object) -> None:
        if kwargs["value"] == _SEED_C.value:
            raise ConflictError("already exists locally")

    svc.create_predicate = AsyncMock(side_effect=_create)

    result = await seed_ontology(svc, ontology=(_SEED_A, _SEED_B, _SEED_C))

    assert result.already_present == (_SEED_A.value,)
    assert result.created == (_SEED_B.value,)
    assert result.blocked_by_local == (_SEED_C.value,)
    # Only the two non-present seeds ever reach create_predicate.
    assert svc.create_predicate.await_count == 2


@pytest.mark.asyncio
async def test_seed_ontology_never_calls_create_for_an_empty_ontology() -> None:
    svc = _service(existing=[])

    result = await seed_ontology(svc, ontology=())

    assert result == type(result)(created=(), already_present=(), blocked_by_local=())
    svc.create_predicate.assert_not_awaited()


def test_seed_ontology_default_ontology_argument_is_the_real_shipped_ontology() -> None:
    """``make dev-seed`` calls ``seed_ontology(service)`` with no ``ontology``
    argument -- the default has to be the real, shipped ``ONTOLOGY`` or a
    production seed run would seed nothing."""
    default = inspect.signature(seed_ontology).parameters["ontology"].default
    assert default is ONTOLOGY


@pytest.mark.asyncio
async def test_seed_ontology_reads_predicates_before_deciding_anything() -> None:
    """``list_predicates`` is awaited exactly once per call, not once per seed --
    the idempotency check is one query against the existing set, not N."""
    svc = _service(existing=[])

    await seed_ontology(svc, ontology=(_SEED_A, _SEED_B, _SEED_C))

    svc.list_predicates.assert_awaited_once_with()
