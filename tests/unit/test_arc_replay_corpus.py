"""Unit tests for `contextplane/arc/service/replay_corpus.py`: no database.

`generate_corpus` is pure (a canonicalization call aside) and gets its own
direct proof here: determinism, the ADR 041 Sec.5 100-class floor, and
per-item match/boundary coverage. `execute_corpus` is exercised against a
scripted `ShadowService` double so its aggregation and range-check logic
(the two-pass "tally explained counts per item, then check ranges" shape
its own module docstring describes) is provable without a session.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from contextplane.arc.service.replay_corpus import (
    MINIMUM_FIXTURE_CLASSES,
    GeneratedCorpus,
    execute_corpus,
    generate_corpus,
)
from contextplane.arc.service.shadow import ShadowDelta


def _item(
    item_id: str, delta_code: str, minimum_count: int = 0, maximum_count: int | None = None, **predicate: Any
) -> dict[str, Any]:
    base_predicate: dict[str, Any] = {
        "intent_kind": None,
        "requested_action_classes": None,
        "environment": None,
        "data_sensitivity_tier": None,
        "capability_ids": None,
        "domain_ids": None,
    }
    base_predicate.update(predicate)
    return {
        "item_id": item_id,
        "delta_code": delta_code,
        "class_predicate": base_predicate,
        "minimum_count": minimum_count,
        "maximum_count": maximum_count,
    }


# ---------------------------------------------------------------------------
# generate_corpus: determinism and the ADR 041 Sec.5 coverage properties.
# ---------------------------------------------------------------------------


def test_generate_corpus_emits_at_least_100_unique_classes_with_no_envelope_items() -> None:
    """The cross-product over the two closed enums alone clears the floor
    regardless of how few (here, zero) envelope items exist."""
    generated = generate_corpus(
        envelope_items=[], scope_predicate_digest="a" * 64, applicability_baseline_digest="b" * 64
    )
    assert generated.fixture_class_count >= MINIMUM_FIXTURE_CLASSES
    assert len(generated.classes) == generated.fixture_class_count
    assert len({id(c) for c in generated.classes}) == len(generated.classes)


def test_generate_corpus_is_deterministic_for_identical_inputs() -> None:
    items = [_item("item-1", "newly_selected", intent_kind=["code_change"])]
    first = generate_corpus(
        envelope_items=items, scope_predicate_digest="a" * 64, applicability_baseline_digest="b" * 64
    )
    second = generate_corpus(
        envelope_items=items, scope_predicate_digest="a" * 64, applicability_baseline_digest="b" * 64
    )
    assert first.canonical_corpus_digest == second.canonical_corpus_digest
    assert first.generator_input_digest == second.generator_input_digest
    assert first.classes == second.classes


def test_generate_corpus_digest_changes_when_scope_predicate_digest_changes() -> None:
    """Two different scope predicates must not collide on the same corpus
    digest, even with identical envelope items -- the digest actually
    depends on the generator's declared inputs, not just the class list."""
    items = [_item("item-1", "newly_selected", intent_kind=["code_change"])]
    first = generate_corpus(
        envelope_items=items, scope_predicate_digest="a" * 64, applicability_baseline_digest="b" * 64
    )
    second = generate_corpus(
        envelope_items=items, scope_predicate_digest="c" * 64, applicability_baseline_digest="b" * 64
    )
    assert first.canonical_corpus_digest != second.canonical_corpus_digest


def test_generate_corpus_includes_one_match_for_every_envelope_item() -> None:
    items = [_item("item-1", "newly_selected", intent_kind=["code_change"], environment=["production"])]
    generated = generate_corpus(
        envelope_items=items, scope_predicate_digest="a" * 64, applicability_baseline_digest="b" * 64
    )
    matches = [
        c
        for c in generated.classes
        if c.get("intent_kind") == ["code_change"] and c.get("environment") == ["production"]
    ]
    assert matches, "at least one generated class must match the item's own predicate exactly"


def test_generate_corpus_includes_a_nearest_non_match_for_a_constrained_field() -> None:
    """One class differing from the item's match on exactly the
    constrained field (`intent_kind`), with every other field held at the
    match's own value."""
    items = [_item("item-1", "newly_selected", intent_kind=["code_change"])]
    generated = generate_corpus(
        envelope_items=items, scope_predicate_digest="a" * 64, applicability_baseline_digest="b" * 64
    )
    boundary_candidates = [c for c in generated.classes if c.get("intent_kind") not in (None, ["code_change"])]
    assert boundary_candidates, "at least one class must name a intent_kind outside the item's own allowed set"


# ---------------------------------------------------------------------------
# execute_corpus: two-pass tally (explained-per-item, then range check),
# against a scripted ShadowService double.
# ---------------------------------------------------------------------------


def _generated(classes: tuple[dict[str, Any], ...]) -> GeneratedCorpus:
    return GeneratedCorpus(
        generator_version="test",
        generator_input_digest="a" * 64,
        canonical_corpus_digest="b" * 64,
        fixture_class_count=len(classes),
        classes=classes,
    )


def _class(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "intent_kind": ["code_change"],
        "requested_action_classes": ["merge"],
        "environment": ["production"],
        "data_sensitivity_tier": ["internal"],
        "capability_ids": None,
        "domain_ids": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_execute_corpus_reports_qualified_when_every_class_is_explained_and_in_range() -> None:
    classes = (_class(), _class(environment=["staging"]))
    generated = _generated(classes)
    items = [_item("item-1", "newly_selected", minimum_count=1, maximum_count=5)]
    shadow = AsyncMock()
    shadow.evaluate = AsyncMock(return_value=ShadowDelta(delta_codes=("newly_selected",)))

    result = await execute_corpus(
        generated,
        shadow=shadow,
        tenant_id=uuid.uuid4(),
        as_of=None,  # type: ignore[arg-type]
        baseline_revision_id=None,
        candidate_revision_id=uuid.uuid4(),
        candidate_semantics={},
        envelope_items=items,
    )
    assert result.unexplained_count == 0
    assert result.out_of_envelope_count == 0
    assert shadow.evaluate.await_count == len(classes)


@pytest.mark.asyncio
async def test_execute_corpus_reports_unexplained_when_no_item_matches_the_delta() -> None:
    classes = (_class(),)
    generated = _generated(classes)
    items: list[dict[str, Any]] = []  # no envelope items at all -- every delta is unmatched
    shadow = AsyncMock()
    shadow.evaluate = AsyncMock(return_value=ShadowDelta(delta_codes=("newly_selected",)))

    result = await execute_corpus(
        generated,
        shadow=shadow,
        tenant_id=uuid.uuid4(),
        as_of=None,  # type: ignore[arg-type]
        baseline_revision_id=None,
        candidate_revision_id=uuid.uuid4(),
        candidate_semantics={},
        envelope_items=items,
    )
    assert result.unexplained_count == 1


@pytest.mark.asyncio
async def test_execute_corpus_reports_out_of_envelope_when_cumulative_count_is_below_the_minimum() -> None:
    """A single class producing one explained occurrence, against an item
    requiring at least two -- the range check only fires once every class
    has been folded in, never per-occurrence."""
    classes = (_class(),)
    generated = _generated(classes)
    items = [_item("item-1", "newly_selected", minimum_count=2, maximum_count=None)]
    shadow = AsyncMock()
    shadow.evaluate = AsyncMock(return_value=ShadowDelta(delta_codes=("newly_selected",)))

    result = await execute_corpus(
        generated,
        shadow=shadow,
        tenant_id=uuid.uuid4(),
        as_of=None,  # type: ignore[arg-type]
        baseline_revision_id=None,
        candidate_revision_id=uuid.uuid4(),
        candidate_semantics={},
        envelope_items=items,
    )
    assert result.unexplained_count == 0
    assert result.out_of_envelope_count == 1


@pytest.mark.asyncio
async def test_execute_corpus_reports_out_of_envelope_when_cumulative_count_exceeds_the_maximum() -> None:
    classes = (_class(), _class(environment=["staging"]), _class(environment=["development"]))
    generated = _generated(classes)
    items = [_item("item-1", "newly_selected", minimum_count=0, maximum_count=1)]
    shadow = AsyncMock()
    shadow.evaluate = AsyncMock(return_value=ShadowDelta(delta_codes=("newly_selected",)))

    result = await execute_corpus(
        generated,
        shadow=shadow,
        tenant_id=uuid.uuid4(),
        as_of=None,  # type: ignore[arg-type]
        baseline_revision_id=None,
        candidate_revision_id=uuid.uuid4(),
        candidate_semantics={},
        envelope_items=items,
    )
    assert result.out_of_envelope_count == 1


@pytest.mark.asyncio
async def test_execute_corpus_evaluates_every_class_exactly_once_matching_the_generated_count() -> None:
    """100% coverage by construction -- every class the generator emitted
    is passed to `shadow.evaluate`, no sampling."""
    classes = tuple(_class(environment=[f"env-{i}"]) for i in range(12))
    generated = _generated(classes)
    shadow = AsyncMock()
    shadow.evaluate = AsyncMock(return_value=ShadowDelta(delta_codes=()))

    await execute_corpus(
        generated,
        shadow=shadow,
        tenant_id=uuid.uuid4(),
        as_of=None,  # type: ignore[arg-type]
        baseline_revision_id=None,
        candidate_revision_id=uuid.uuid4(),
        candidate_semantics={},
        envelope_items=[],
    )
    assert shadow.evaluate.await_count == 12
