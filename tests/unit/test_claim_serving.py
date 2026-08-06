"""Unit tests for claim_serving.py: the citation guarantee, the recall label,
and the visibility double-filter, all without Postgres.

All DB interaction is mocked at session.execute via an SQL-string-keyed
router, mirroring test_promotion_sweep_worker.py's mock-factory pattern.
`registry.service.governance.visibility.is_visible` /
`fetch_shared_with_tenants_one` are monkeypatched at their source module
(they are imported locally inside `_visible_subjects`, so patching the
module attribute -- not a local name -- is what takes effect).

Coverage:
- `ServedClaim.__post_init__`: cannot be constructed without citations, and
  cannot be constructed with any label/trust other than the fixed recall
  ones -- the platform's read-path anti-injection defense is structural, not
  a convention a caller could forget. Also: the dataclass is frozen, so the
  label cannot be tampered with *after* construction either.
- `ClaimQuery.__post_init__` and `retrieve`'s own inline copy of the same two
  checks (persona validity, limit/top_k range), pinned by type and message.
  Both raise this codebase's `ValidationError`, not a bare `ValueError` --
  `tests/unit/test_memory_mcp_tools.py` and `tests/integration/test_memory_rest.py`
  separately pin that the MCP tool and REST router catch sites that translate
  it into a `ToolError`/422 stayed in step with that type.
- `_claim_visible`: the claim's own visibility rule in isolation.
- `_visible_subjects`: the subject's visibility rule in isolation, including
  the empty-input short circuit.
- `get`/`query`/`retrieve`: the double filter wired end to end -- a claim
  that is public but whose subject is not visible is withheld, and a claim
  that is private but whose subject *is* visible is also withheld. A
  positive control (both checks pass) proves these are not vacuously true --
  a `get`/`query`/`retrieve` that always returned nothing would pass every
  negative test here and fail only the positive ones.
- Inline provenance: an L3/architect persona gets the evidence excerpt
  inline; every other persona gets the handle only.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import registry.service.governance.visibility as visibility_module
from registry.exceptions import ValidationError
from registry.service.memory.claim_serving import (
    PERSONA_AGENT,
    PERSONA_ARCHITECT,
    PERSONA_L1,
    PERSONA_L3,
    RECALL_LABEL,
    RECALL_TRUST,
    Citation,
    ClaimQuery,
    ClaimServingService,
    ServedClaim,
    UncitedClaimError,
)
from tests.helpers.clock import FakeClock
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _claim_row(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = dict(
        claim_id=uuid.uuid4(),
        subject_entity_id=uuid.uuid4(),
        predicate="owned_by_team",
        value="platform",
        claim_category="ownership_stewardship",
        confidence=0.9,
        source_authority="owner_extraction",
        asserted_valid_from=_NOW,
        asserted_valid_to=None,
        confirms_claim_id=None,
        created_at=_NOW,
        confidence_scored_at=_NOW,
        confidence_hold_until=None,
        namespace="team/platform",
        visibility="public",
        owning_tenant_id=uuid.uuid4(),
    )
    defaults.update(overrides)
    return defaults


def _citation_row(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = dict(evidence_kind="session_event", evidence_ref="e1", evidence_excerpt="said so")
    defaults.update(overrides)
    return defaults


def _mapping_result(*, all_rows: list[dict] | None = None, first_row: dict | None = None) -> MagicMock:
    result = MagicMock()
    mapped = MagicMock()
    mapped.all = MagicMock(return_value=all_rows or [])
    mapped.first = MagicMock(return_value=first_row)
    result.mappings = MagicMock(return_value=mapped)
    return result


def _scalars_result(items: list) -> MagicMock:
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=items)
    result.scalars = MagicMock(return_value=scalars)
    return result


def _serving_session_factory(
    *,
    query_rows: list[dict] | None = None,
    by_id_row: dict | None = None,
    citations_rows: list[dict] | None = None,
    entities: list[Any] | None = None,
    semantic_rows: list[dict] | None = None,
    lexical_rows: list[dict] | None = None,
) -> tuple[MagicMock, list[str]]:
    """SQL-string-keyed AsyncMock session factory for `ClaimServingService`.

    Routes on a distinguishing substring of each of the module's five query
    shapes (structural query, by-id, provenance, subject-visibility lookup,
    the two hybrid-search arms) and raises on anything else, so a query this
    module starts issuing without the test knowing about it fails loudly
    rather than silently returning nothing.
    """
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        if "ORDER BY c.asserted_valid_from DESC" in sql:
            return _mapping_result(all_rows=query_rows or [])
        if "WHERE c.claim_id = :cid" in sql:
            return _mapping_result(first_row=by_id_row)
        if "FROM memory_claim_provenance" in sql:
            return _mapping_result(all_rows=citations_rows or [])
        if "FROM entities" in sql:
            return _scalars_result(entities or [])
        if "ORDER BY emb.vector <=>" in sql:
            return _mapping_result(all_rows=semantic_rows or [])
        if "ORDER BY lex_rank DESC" in sql:
            return _mapping_result(all_rows=lexical_rows or [])
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory, executed


def _refusing_factory() -> MagicMock:
    """A factory that fails the test if the service ever opens a session.

    Used to prove a validation check runs before any database work.
    """

    def _refuse() -> Any:
        raise AssertionError("the database was touched despite the validation failure")

    factory = MagicMock()
    factory.side_effect = _refuse
    return factory


# ---------------------------------------------------------------------------
# ServedClaim: the citation guarantee and the recall label are structural
# ---------------------------------------------------------------------------


def _served_claim_kwargs(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = dict(
        claim_id=uuid.uuid4(),
        subject_entity_id=uuid.uuid4(),
        predicate="owned_by_team",
        value="platform",
        claim_category="ownership_stewardship",
        confidence=0.9,
        authority="owner_extraction",
        valid_from=_NOW,
        valid_to=None,
        as_of=_NOW,
        human_confirmed=False,
        citations=(Citation(kind="session_event", ref="e1"),),
    )
    defaults.update(overrides)
    return defaults


def test_a_served_claim_cannot_be_constructed_without_citations() -> None:
    with pytest.raises(UncitedClaimError, match="no citations"):
        ServedClaim(**_served_claim_kwargs(citations=()))


def test_a_served_claim_cannot_be_constructed_with_a_different_trust_value() -> None:
    """Confidence never substitutes for the label -- a caller cannot opt a claim
    out of "untrusted" by passing a different value, even alongside a real
    citation."""
    with pytest.raises(UncitedClaimError, match="recalled and untrusted"):
        ServedClaim(**_served_claim_kwargs(trust="trusted"))


def test_a_served_claim_cannot_be_constructed_with_a_different_label_value() -> None:
    with pytest.raises(UncitedClaimError, match="recalled and untrusted"):
        ServedClaim(**_served_claim_kwargs(label="operator-authored"))


def test_a_served_claim_with_both_citations_and_the_fixed_label_constructs_cleanly() -> None:
    """Positive control: the guard is a check on specific bad inputs, not a
    blanket refusal -- proves the two tests above fail for the reason they
    claim to, not because construction never succeeds at all."""
    claim = ServedClaim(**_served_claim_kwargs())
    assert claim.label == RECALL_LABEL
    assert claim.trust == RECALL_TRUST


def test_a_served_claim_is_frozen_so_the_label_cannot_be_tampered_with_after_construction() -> None:
    """The invariant holds at construction *and* stays held -- a caller cannot
    build a compliant claim and then flip its trust label after the fact."""
    claim = ServedClaim(**_served_claim_kwargs())
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.trust = "trusted"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ClaimQuery / retrieve: persona and limit validation
# ---------------------------------------------------------------------------


def test_claim_query_rejects_an_unknown_persona() -> None:
    with pytest.raises(ValidationError, match="unknown persona"):
        ClaimQuery(persona="l2")


def test_claim_query_rejects_a_limit_below_one() -> None:
    with pytest.raises(ValidationError, match="limit must be"):
        ClaimQuery(limit=0)


def test_claim_query_rejects_a_limit_above_the_maximum() -> None:
    with pytest.raises(ValidationError, match="limit must be"):
        ClaimQuery(limit=ClaimQuery.MAX_LIMIT + 1)


def test_claim_query_accepts_a_known_persona_and_an_in_range_limit() -> None:
    """Positive control for the two checks above."""
    query = ClaimQuery(persona=PERSONA_L1, limit=ClaimQuery.MAX_LIMIT)
    assert query.persona == PERSONA_L1
    assert query.limit == ClaimQuery.MAX_LIMIT


@pytest.mark.asyncio
async def test_retrieve_rejects_an_unknown_persona_before_touching_the_database() -> None:
    service = ClaimServingService(_refusing_factory(), clock=FakeClock(_NOW))
    with pytest.raises(ValidationError, match="unknown persona"):
        await service.retrieve(tenant_context(), query="x", embedder=MagicMock(), persona="l2")


@pytest.mark.asyncio
async def test_retrieve_rejects_a_top_k_above_the_maximum_before_touching_the_database() -> None:
    service = ClaimServingService(_refusing_factory(), clock=FakeClock(_NOW))
    with pytest.raises(ValidationError, match="top_k must be"):
        await service.retrieve(tenant_context(), query="x", embedder=MagicMock(), top_k=ClaimQuery.MAX_LIMIT + 1)


# ---------------------------------------------------------------------------
# _claim_visible: the claim's own visibility rule, in isolation
# ---------------------------------------------------------------------------


def test_claim_visible_is_true_for_the_owning_tenant_even_if_the_claim_is_private() -> None:
    ctx = tenant_context()
    row = {"owning_tenant_id": ctx.tenant_id, "visibility": "private"}
    assert ClaimServingService._claim_visible(ctx, row) is True


def test_claim_visible_is_true_for_a_public_claim_from_a_foreign_tenant() -> None:
    ctx = tenant_context()
    row = {"owning_tenant_id": uuid.uuid4(), "visibility": "public"}
    assert ClaimServingService._claim_visible(ctx, row) is True


def test_claim_visible_is_false_for_a_private_claim_from_a_foreign_tenant() -> None:
    ctx = tenant_context()
    row = {"owning_tenant_id": uuid.uuid4(), "visibility": "private"}
    assert ClaimServingService._claim_visible(ctx, row) is False


def test_claim_visible_is_false_for_a_tenant_shared_claim_from_a_foreign_tenant() -> None:
    """Tenant-shared is not resolved beyond the owning tenant at this layer --
    the claim tables carry no per-claim share list, per the module's own
    docstring."""
    ctx = tenant_context()
    row = {"owning_tenant_id": uuid.uuid4(), "visibility": "tenant-shared"}
    assert ClaimServingService._claim_visible(ctx, row) is False


# ---------------------------------------------------------------------------
# _visible_subjects: the subject's visibility rule, in isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_visible_subjects_short_circuits_on_an_empty_list_without_querying() -> None:
    service = ClaimServingService(_refusing_factory(), clock=FakeClock(_NOW))
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=AssertionError("must not query for zero ids"))
    result = await service._visible_subjects(session, tenant_context(), [])
    assert result == set()


@pytest.mark.asyncio
async def test_visible_subjects_keeps_only_the_entities_the_visibility_rule_approves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible_id, hidden_id = uuid.uuid4(), uuid.uuid4()
    entities = [MagicMock(entity_id=visible_id), MagicMock(entity_id=hidden_id)]
    factory, _ = _serving_session_factory(entities=entities)
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        visibility_module, "is_visible", MagicMock(side_effect=lambda ctx, entity, acl: entity.entity_id == visible_id)
    )

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    async with factory() as session:
        result = await service._visible_subjects(session, tenant_context(), [visible_id, hidden_id])

    assert result == {visible_id}


# ---------------------------------------------------------------------------
# get(): the double filter, wired end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_none_for_a_claim_that_does_not_exist() -> None:
    factory, _ = _serving_session_factory(by_id_row=None)
    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    assert await service.get(tenant_context(), uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_withholds_a_public_claim_whose_subject_is_not_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public claim about a private capability must not disclose that the
    capability exists -- the subject check gates even a claim that passes
    its own visibility check on its own."""
    ctx = tenant_context()
    subject_id = uuid.uuid4()
    row = _claim_row(subject_entity_id=subject_id, visibility="public", owning_tenant_id=uuid.uuid4())
    factory, _ = _serving_session_factory(by_id_row=row, entities=[MagicMock(entity_id=subject_id)])
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(visibility_module, "is_visible", MagicMock(return_value=False))

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    assert await service.get(ctx, row["claim_id"]) is None


@pytest.mark.asyncio
async def test_get_withholds_a_private_claim_even_though_its_subject_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the double filter: a subject may be visible while an
    observer's private note about it is not -- checking the subject alone
    would leak the note."""
    ctx = tenant_context()
    subject_id = uuid.uuid4()
    row = _claim_row(subject_entity_id=subject_id, visibility="private", owning_tenant_id=uuid.uuid4())
    factory, executed = _serving_session_factory(by_id_row=row, entities=[MagicMock(entity_id=subject_id)])
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(visibility_module, "is_visible", MagicMock(return_value=True))

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    assert await service.get(ctx, row["claim_id"]) is None
    # The claim-visibility check gates before the subject is ever resolved.
    assert not any("FROM entities" in sql for sql in executed)


@pytest.mark.asyncio
async def test_get_returns_the_served_claim_when_both_the_claim_and_its_subject_are_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for both tests above: proves `get` is not simply
    always returning `None` (which would make the two negative tests above
    trivially, uselessly true)."""
    ctx = tenant_context()
    subject_id = uuid.uuid4()
    row = _claim_row(subject_entity_id=subject_id, visibility="public", owning_tenant_id=uuid.uuid4())
    citation = _citation_row()
    factory, _ = _serving_session_factory(
        by_id_row=row, citations_rows=[citation], entities=[MagicMock(entity_id=subject_id)]
    )
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(visibility_module, "is_visible", MagicMock(return_value=True))

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    served = await service.get(ctx, row["claim_id"])

    assert served is not None
    assert served.claim_id == row["claim_id"]
    assert served.citations and served.citations[0].ref == "e1"
    assert served.label == RECALL_LABEL
    assert served.trust == RECALL_TRUST


# ---------------------------------------------------------------------------
# query(): subject-level visibility filtering over a result set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_excludes_a_row_whose_subject_is_not_visible_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = tenant_context()
    visible_subject, hidden_subject = uuid.uuid4(), uuid.uuid4()
    kept = _claim_row(subject_entity_id=visible_subject, value="kept")
    dropped = _claim_row(subject_entity_id=hidden_subject, value="dropped")
    factory, _ = _serving_session_factory(
        query_rows=[kept, dropped],
        citations_rows=[_citation_row()],
        entities=[MagicMock(entity_id=visible_subject)],
    )
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(visibility_module, "is_visible", MagicMock(return_value=True))

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    served = await service.query(ctx, ClaimQuery())

    assert [c.value for c in served] == ["kept"]


@pytest.mark.asyncio
async def test_query_applies_min_confidence_after_visibility_not_instead_of_it(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = tenant_context()
    subject_id = uuid.uuid4()
    row = _claim_row(subject_entity_id=subject_id, confidence=0.9)
    factory, _ = _serving_session_factory(
        query_rows=[row], citations_rows=[_citation_row()], entities=[MagicMock(entity_id=subject_id)]
    )
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(visibility_module, "is_visible", MagicMock(return_value=True))

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    below = await service.query(ctx, ClaimQuery(min_confidence=0.999))
    above = await service.query(ctx, ClaimQuery(min_confidence=0.0))

    assert below == ()
    assert len(above) == 1


# ---------------------------------------------------------------------------
# retrieve(): the same double filter over the fused ranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_filters_the_fused_ranking_by_both_claim_and_subject_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = tenant_context()
    visible_subject, hidden_subject = uuid.uuid4(), uuid.uuid4()
    kept = _claim_row(
        subject_entity_id=visible_subject, visibility="public", owning_tenant_id=uuid.uuid4(), value="kept"
    )
    claim_only_hidden = _claim_row(
        subject_entity_id=visible_subject, visibility="private", owning_tenant_id=uuid.uuid4(), value="claim-hidden"
    )
    subject_only_hidden = _claim_row(
        subject_entity_id=hidden_subject, visibility="public", owning_tenant_id=uuid.uuid4(), value="subject-hidden"
    )
    factory, _ = _serving_session_factory(
        semantic_rows=[kept, claim_only_hidden, subject_only_hidden],
        lexical_rows=[],
        citations_rows=[_citation_row()],
        entities=[MagicMock(entity_id=visible_subject)],
    )
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(visibility_module, "is_visible", MagicMock(return_value=True))

    embedder = MagicMock(model_version="m1")
    embedder.encode = MagicMock(return_value=[[0.1, 0.2]])

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    served = await service.retrieve(ctx, query="anything", embedder=embedder, top_k=5)

    assert [c.value for c in served] == ["kept"]


@pytest.mark.asyncio
async def test_retrieve_applies_min_confidence_after_the_visibility_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = tenant_context()
    subject_id = uuid.uuid4()
    row = _claim_row(subject_entity_id=subject_id, confidence=0.9)
    factory, _ = _serving_session_factory(
        semantic_rows=[row],
        lexical_rows=[],
        citations_rows=[_citation_row()],
        entities=[MagicMock(entity_id=subject_id)],
    )
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(visibility_module, "is_visible", MagicMock(return_value=True))
    embedder = MagicMock(model_version="m1")
    embedder.encode = MagicMock(return_value=[[0.1, 0.2]])

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    below = await service.retrieve(ctx, query="x", embedder=embedder, min_confidence=0.999)
    above = await service.retrieve(ctx, query="x", embedder=embedder, min_confidence=0.0)

    assert below == ()
    assert len(above) == 1


@pytest.mark.asyncio
async def test_retrieve_returns_no_results_when_neither_arm_finds_anything() -> None:
    factory, _ = _serving_session_factory(semantic_rows=[], lexical_rows=[])
    embedder = MagicMock(model_version="m1")
    embedder.encode = MagicMock(return_value=[[0.1, 0.2]])

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    served = await service.retrieve(tenant_context(), query="nothing matches", embedder=embedder)

    assert served == ()


# ---------------------------------------------------------------------------
# Persona depth: inline provenance vs. handle-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l1_persona_gets_the_citation_handle_without_the_excerpt(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = tenant_context()
    subject_id = uuid.uuid4()
    row = _claim_row(subject_entity_id=subject_id)
    citation = _citation_row(evidence_excerpt="the platform team owns it")
    factory, _ = _serving_session_factory(
        by_id_row=row, citations_rows=[citation], entities=[MagicMock(entity_id=subject_id)]
    )
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(visibility_module, "is_visible", MagicMock(return_value=True))

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    served = await service.get(ctx, row["claim_id"], persona=PERSONA_L1)

    assert served is not None
    assert served.citations[0].ref == "e1"
    assert served.citations[0].excerpt is None


@pytest.mark.asyncio
async def test_architect_persona_gets_the_excerpt_inlined(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = tenant_context()
    subject_id = uuid.uuid4()
    row = _claim_row(subject_entity_id=subject_id)
    citation = _citation_row(evidence_excerpt="the platform team owns it")
    factory, _ = _serving_session_factory(
        by_id_row=row, citations_rows=[citation], entities=[MagicMock(entity_id=subject_id)]
    )
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(visibility_module, "is_visible", MagicMock(return_value=True))

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    served = await service.get(ctx, row["claim_id"], persona=PERSONA_ARCHITECT)

    assert served is not None
    assert served.citations[0].excerpt == "the platform team owns it"


@pytest.mark.asyncio
async def test_every_served_claim_carries_the_recall_label_regardless_of_persona(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = tenant_context()
    subject_id = uuid.uuid4()
    row = _claim_row(subject_entity_id=subject_id)
    factory, _ = _serving_session_factory(
        by_id_row=row, citations_rows=[_citation_row()], entities=[MagicMock(entity_id=subject_id)]
    )
    monkeypatch.setattr(visibility_module, "fetch_shared_with_tenants_one", AsyncMock(return_value=[]))
    monkeypatch.setattr(visibility_module, "is_visible", MagicMock(return_value=True))

    service = ClaimServingService(factory, clock=FakeClock(_NOW))
    for persona in (PERSONA_L1, PERSONA_L3, PERSONA_ARCHITECT, PERSONA_AGENT):
        served = await service.get(ctx, row["claim_id"], persona=persona)
        assert served is not None
        assert served.label == RECALL_LABEL
        assert served.trust == RECALL_TRUST
