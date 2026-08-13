"""Ownership assignments record accountability, and every move records why.

Two properties carry this file. An assignment starts as a proposal rather than as
fact — creating one already in force would let any caller establish accountability
for anything by asserting it — and every transition appends a row saying who moved
it, when, and why. A state column with no history answers "who owns this" and
never "why did that change", which is the question an accountability record exists
for.

The `owns` and `owned_by` views are asserted against the *same* assignment,
because they are one row read from two ends. A test that checked them separately
would pass for an implementation that stored them twice and let them drift.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.ownership import queries
from contextplane.ownership.service import (
    AssignmentNotFound,
    IllegalTransition,
    OwnershipError,
    OwnershipService,
    SubjectMismatch,
)

_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)


class _FixedClock:
    def now(self) -> datetime.datetime:
        return _NOW


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _fixture(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A tenant, a published revision to attribute against, and an owned entity."""
    tenant_id, revision_id, entity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'own')"),
            {"t": tenant_id, "s": f"ow-{tenant_id.hex[:10]}"},
        )
        await session.execute(
            text(
                "INSERT INTO profile_revisions ("
                "  profile_revision_id, profile_family, profile_name, semantic_version,"
                "  canonical_document, document_digest, compatibility, published_by, published_at"
                ") VALUES (:rid, 'platform', :name, '1.0.0', CAST('{}' AS JSONB), :digest,"
                "          'backward_compatible', 'test', :now)"
            ),
            {"rid": revision_id, "name": f"ow-{revision_id.hex[:12]}", "digest": revision_id.hex, "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, is_active, created_at)"
                " VALUES (:eid, :tid, 'core:capability', :name, TRUE, :now)"
            ),
            {"eid": entity_id, "tid": tenant_id, "name": f"e-{entity_id.hex[:8]}", "now": _NOW},
        )
    return tenant_id, revision_id, entity_id


def _service(factory: async_sessionmaker[AsyncSession]) -> OwnershipService:
    return OwnershipService(session_factory=factory, clock=_FixedClock())


async def _assign(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    revision_id: uuid.UUID,
    entity_id: uuid.UUID,
    **overrides: object,
) -> queries.OwnershipAssignment:
    kwargs: dict[str, object] = {
        "tenant_id": tenant_id,
        "owner_principal": "team-alpha",
        "owned_target_kind": "entity",
        "owned_target_id": entity_id,
        "role": "steward",
        "scope": "service",
        "source": "declared",
        "recorded_by": "operator",
        "profile_revision_id": revision_id,
    }
    kwargs.update(overrides)
    return await _service(factory).assign(**kwargs)  # type: ignore[arg-type]


# --- assignment -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_assignment_starts_as_a_proposal_not_as_fact(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Creating one already in force would let anyone establish accountability."""
    tenant_id, revision_id, entity_id = await _fixture(factory)

    assignment = await _assign(factory, tenant_id, revision_id, entity_id)

    assert assignment.validation_state == queries.DRAFT
    assert assignment.is_pending
    assert not assignment.is_in_force


@pytest.mark.asyncio
async def test_an_assignment_carries_its_own_provenance(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The row is `NOT NULL` on provenance, and the value must be a real record."""
    tenant_id, revision_id, entity_id = await _fixture(factory)

    assignment = await _assign(factory, tenant_id, revision_id, entity_id)

    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT authority, validating_profile_revision_id FROM assertion_provenance"
                        " WHERE provenance_id = :p"
                    ),
                    {"p": assignment.provenance_id},
                )
            )
            .mappings()
            .one()
        )

    assert row["authority"] == "canonical_owner"
    assert row["validating_profile_revision_id"] == revision_id


@pytest.mark.asyncio
async def test_an_inferred_assignment_records_its_method_and_confidence(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A derived assignment is a guess, and must be readable as one."""
    tenant_id, revision_id, entity_id = await _fixture(factory)

    assignment = await _assign(
        factory, tenant_id, revision_id, entity_id, derivation_method="commit-history", confidence=0.6
    )

    assert assignment.derivation_method == "commit-history"
    assert assignment.confidence == 0.6


@pytest.mark.asyncio
async def test_an_assignment_naming_a_missing_target_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An assignment against a target that does not exist is unresolvable forever."""
    tenant_id, revision_id, _ = await _fixture(factory)

    with pytest.raises(SubjectMismatch):
        await _assign(factory, tenant_id, revision_id, uuid.uuid4())


@pytest.mark.asyncio
async def test_an_assignment_against_another_tenants_target_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, revision_id, _ = await _fixture(factory)
    _, _, foreign_entity = await _fixture(factory)

    with pytest.raises(SubjectMismatch):
        await _assign(factory, tenant_id, revision_id, foreign_entity)


# --- transitions ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validating_puts_an_assignment_in_force(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, revision_id, entity_id = await _fixture(factory)
    assignment = await _assign(factory, tenant_id, revision_id, entity_id)

    proposed = await _service(factory).transition(
        tenant_id=tenant_id,
        assignment_id=assignment.ownership_assignment_id,
        to_state=queries.PROPOSED,
        reason="submitted for review",
        recorded_by="operator",
    )
    validated = await _service(factory).transition(
        tenant_id=tenant_id,
        assignment_id=assignment.ownership_assignment_id,
        to_state=queries.VALIDATED,
        reason="confirmed by the owning team",
        recorded_by="reviewer",
    )

    assert proposed.is_pending
    assert validated.is_in_force
    assert not validated.is_pending


@pytest.mark.asyncio
async def test_every_transition_records_actor_time_and_reason(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A state column with no history says who owns it and never why it moved."""
    tenant_id, revision_id, entity_id = await _fixture(factory)
    assignment = await _assign(factory, tenant_id, revision_id, entity_id)

    await _service(factory).transition(
        tenant_id=tenant_id,
        assignment_id=assignment.ownership_assignment_id,
        to_state=queries.PROPOSED,
        reason="submitted for review",
        recorded_by="operator",
    )
    await _service(factory).transition(
        tenant_id=tenant_id,
        assignment_id=assignment.ownership_assignment_id,
        to_state=queries.VALIDATED,
        reason="confirmed by the owning team",
        recorded_by="reviewer",
    )

    async with factory() as session:
        history = await queries.transitions_of(session, assignment_id=assignment.ownership_assignment_id)

    assert [(h["sequence"], h["from_state"], h["to_state"]) for h in history] == [
        (1, queries.DRAFT, queries.PROPOSED),
        (2, queries.PROPOSED, queries.VALIDATED),
    ]
    assert history[1]["recorded_by"] == "reviewer"
    assert history[1]["reason"] == "confirmed by the owning team"


@pytest.mark.asyncio
async def test_a_transition_with_no_reason_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, revision_id, entity_id = await _fixture(factory)
    assignment = await _assign(factory, tenant_id, revision_id, entity_id)

    with pytest.raises(OwnershipError, match="reason"):
        await _service(factory).transition(
            tenant_id=tenant_id,
            assignment_id=assignment.ownership_assignment_id,
            to_state=queries.PROPOSED,
            reason="   ",
            recorded_by="operator",
        )


@pytest.mark.asyncio
async def test_an_undeclared_move_is_refused_by_name(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`draft` does not reach `validated` directly; skipping review is not a shortcut."""
    tenant_id, revision_id, entity_id = await _fixture(factory)
    assignment = await _assign(factory, tenant_id, revision_id, entity_id)

    with pytest.raises(IllegalTransition, match="validated"):
        await _service(factory).transition(
            tenant_id=tenant_id,
            assignment_id=assignment.ownership_assignment_id,
            to_state=queries.VALIDATED,
            reason="skipping review",
            recorded_by="operator",
        )


@pytest.mark.asyncio
async def test_a_terminal_assignment_moves_nowhere(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, revision_id, entity_id = await _fixture(factory)
    assignment = await _assign(factory, tenant_id, revision_id, entity_id)
    await _service(factory).transition(
        tenant_id=tenant_id,
        assignment_id=assignment.ownership_assignment_id,
        to_state=queries.REVOKED,
        reason="raised in error",
        recorded_by="operator",
    )

    with pytest.raises(IllegalTransition):
        await _service(factory).transition(
            tenant_id=tenant_id,
            assignment_id=assignment.ownership_assignment_id,
            to_state=queries.PROPOSED,
            reason="reopening",
            recorded_by="operator",
        )


@pytest.mark.asyncio
async def test_revoking_records_the_reason_on_the_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The schema requires it, and a revocation nobody explained is unauditable."""
    tenant_id, revision_id, entity_id = await _fixture(factory)
    assignment = await _assign(factory, tenant_id, revision_id, entity_id)

    revoked = await _service(factory).transition(
        tenant_id=tenant_id,
        assignment_id=assignment.ownership_assignment_id,
        to_state=queries.REVOKED,
        reason="team disbanded",
        recorded_by="operator",
    )

    assert revoked.revocation_reason == "team disbanded"
    assert not revoked.is_in_force


@pytest.mark.asyncio
async def test_superseding_names_its_replacement(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without the link the history cannot be walked forward from the old row."""
    tenant_id, revision_id, entity_id = await _fixture(factory)
    original = await _assign(factory, tenant_id, revision_id, entity_id)
    replacement = await _assign(factory, tenant_id, revision_id, entity_id, owner_principal="team-beta")

    service = _service(factory)
    for state, reason in ((queries.PROPOSED, "submitted"), (queries.VALIDATED, "confirmed")):
        await service.transition(
            tenant_id=tenant_id,
            assignment_id=original.ownership_assignment_id,
            to_state=state,
            reason=reason,
            recorded_by="operator",
        )

    superseded = await service.transition(
        tenant_id=tenant_id,
        assignment_id=original.ownership_assignment_id,
        to_state=queries.SUPERSEDED,
        reason="ownership handed over",
        recorded_by="operator",
        replaced_by_assignment_id=replacement.ownership_assignment_id,
    )

    assert superseded.replaced_by_assignment_id == replacement.ownership_assignment_id


@pytest.mark.asyncio
async def test_superseding_without_a_replacement_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, revision_id, entity_id = await _fixture(factory)
    assignment = await _assign(factory, tenant_id, revision_id, entity_id)
    service = _service(factory)
    for state, reason in ((queries.PROPOSED, "submitted"), (queries.VALIDATED, "confirmed")):
        await service.transition(
            tenant_id=tenant_id,
            assignment_id=assignment.ownership_assignment_id,
            to_state=state,
            reason=reason,
            recorded_by="operator",
        )

    with pytest.raises(OwnershipError, match="replaces"):
        await service.transition(
            tenant_id=tenant_id,
            assignment_id=assignment.ownership_assignment_id,
            to_state=queries.SUPERSEDED,
            reason="handed over to nobody",
            recorded_by="operator",
        )


@pytest.mark.asyncio
async def test_transitioning_an_assignment_of_another_tenant_is_not_found(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, revision_id, entity_id = await _fixture(factory)
    other_tenant, _, _ = await _fixture(factory)
    assignment = await _assign(factory, tenant_id, revision_id, entity_id)

    with pytest.raises(AssignmentNotFound):
        await _service(factory).transition(
            tenant_id=other_tenant,
            assignment_id=assignment.ownership_assignment_id,
            to_state=queries.PROPOSED,
            reason="reaching across a tenant boundary",
            recorded_by="operator",
        )


# --- the two derived views ------------------------------------------------------------


@pytest.mark.asyncio
async def test_owns_and_owned_by_are_one_row_read_from_two_ends(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Asserted against the same assignment, so a stored-twice implementation fails.

    Checked separately, the two views would pass for an implementation that kept
    them in different tables and let them drift.
    """
    tenant_id, revision_id, entity_id = await _fixture(factory)
    assignment = await _assign(factory, tenant_id, revision_id, entity_id)
    service = _service(factory)
    for state, reason in ((queries.PROPOSED, "submitted"), (queries.VALIDATED, "confirmed")):
        await service.transition(
            tenant_id=tenant_id,
            assignment_id=assignment.ownership_assignment_id,
            to_state=state,
            reason=reason,
            recorded_by="operator",
        )

    async with factory() as session:
        owns = await queries.owned_by(session, tenant_id=tenant_id, owner_principal="team-alpha", at=_NOW)
        owners = await queries.owners_of(
            session, tenant_id=tenant_id, owned_target_kind="entity", owned_target_id=entity_id, at=_NOW
        )

    assert [o.ownership_assignment_id for o in owns] == [assignment.ownership_assignment_id]
    assert [o.ownership_assignment_id for o in owners] == [assignment.ownership_assignment_id]


@pytest.mark.asyncio
async def test_a_pending_assignment_is_excluded_from_the_views_unless_asked_for(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reporting a proposal as ownership answers "who owns this" with somebody who does not."""
    tenant_id, revision_id, entity_id = await _fixture(factory)
    await _assign(factory, tenant_id, revision_id, entity_id)

    async with factory() as session:
        default = await queries.owned_by(session, tenant_id=tenant_id, owner_principal="team-alpha", at=_NOW)
        including = await queries.owned_by(
            session, tenant_id=tenant_id, owner_principal="team-alpha", at=_NOW, include_pending=True
        )

    assert default == ()
    assert len(including) == 1
    assert including[0].is_pending


@pytest.mark.asyncio
async def test_a_revoked_assignment_leaves_the_views(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, revision_id, entity_id = await _fixture(factory)
    assignment = await _assign(factory, tenant_id, revision_id, entity_id)
    service = _service(factory)
    for state, reason in ((queries.PROPOSED, "submitted"), (queries.VALIDATED, "confirmed")):
        await service.transition(
            tenant_id=tenant_id,
            assignment_id=assignment.ownership_assignment_id,
            to_state=state,
            reason=reason,
            recorded_by="operator",
        )
    await service.transition(
        tenant_id=tenant_id,
        assignment_id=assignment.ownership_assignment_id,
        to_state=queries.REVOKED,
        reason="withdrawn",
        recorded_by="operator",
    )

    async with factory() as session:
        owners = await queries.owners_of(
            session, tenant_id=tenant_id, owned_target_kind="entity", owned_target_id=entity_id, at=_NOW
        )
        still_readable = await queries.get(
            session, tenant_id=tenant_id, assignment_id=assignment.ownership_assignment_id
        )

    assert owners == ()
    assert still_readable is not None, "a revoked assignment is a different answer from no assignment"
    assert still_readable.validation_state == queries.REVOKED
