"""A tenant is governed by exactly one binding, and it moves by declared steps.

The binding is what decides which profile a tenant's writes are validated against,
so two active bindings and none are equally bad: the first makes the answer
ambiguous and the second makes it absent. Both writes therefore happen in one
transaction, and these tests check the result in the database rather than the value
the service returned.

The state machine is a table in one place -- `planned -> validating -> active`,
`active -> rollback_pending -> rolled_back`, with `retired` reachable from the three
live states. Anything else is refused rather than tolerated, because a binding that
could skip validation is a binding that governs writes nobody checked.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.profile import bindings as profile_bindings
from contextplane.profile import service as profile_service
from contextplane.profile.schemas.entity import EntityTypeDefinition
from contextplane.profile.schemas.relationship import RelationshipTypeDefinition

_NAMESPACE = "northwind"
_START = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)


class _MovableClock:
    """Advances only when a test asks it to.

    A real clock would make `effective_from` orderings incidental — two activations
    in the same millisecond would order arbitrarily, and the test would pass or fail
    on how fast the machine is.
    """

    def __init__(self) -> None:
        self._now = _START

    def now(self) -> datetime.datetime:
        return self._now

    def advance(self, *, minutes: int) -> None:
        self._now = self._now + datetime.timedelta(minutes=minutes)


def _entity(type_name: str) -> EntityTypeDefinition:
    return EntityTypeDefinition(namespace=_NAMESPACE, type_name=type_name)


def _relationship(type_name: str) -> RelationshipTypeDefinition:
    return RelationshipTypeDefinition(
        namespace=_NAMESPACE,
        type_name=type_name,
        source_type=f"{_NAMESPACE}:warehouse",
        destination_type=f"{_NAMESPACE}:depot",
        direction="directed",
        cardinality_scope="per_source",
        authority="observed",
        cross_org_policy="deny",
    )


@pytest_asyncio.fixture
async def bound(pg_container: str) -> AsyncIterator[dict[str, object]]:
    """A tenant, two published revisions to bind to, and a BindingService."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    clock = _MovableClock()
    try:
        async with factory() as session, session.begin():
            for identifier, slug in ((tenant_id, "pb"), (other_tenant_id, "pb-other")):
                await session.execute(
                    text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'bindings')"),
                    {"t": identifier, "s": f"{slug}-{identifier.hex[:10]}"},
                )
        publisher = profile_service.ProfileService(factory, clock=clock)
        revisions = []
        for index, extra in enumerate(((), (_entity("annex"),))):
            revisions.append(
                await publisher.publish_revision(
                    profile_family="platform",
                    profile_name=f"bind-{tenant_id.hex[:12]}",
                    semantic_version=f"1.{index}.0",
                    entities=(_entity("warehouse"), _entity("depot"), *extra),
                    relationships=(_relationship("stocks"),),
                    interfaces=(),
                    compatibility="backward_compatible",
                    published_by="platform@example.test",
                )
            )
        yield {
            "factory": factory,
            "tenant": tenant_id,
            "other_tenant": other_tenant_id,
            "clock": clock,
            "service": profile_bindings.BindingService(factory, clock=clock),
            "revisions": revisions,
        }
    finally:
        await engine.dispose()


async def _plan(
    fixture: dict[str, object],
    *,
    revision_index: int = 0,
    extensions: Sequence[uuid.UUID] = (),
) -> profile_bindings.Binding:
    service: profile_bindings.BindingService = fixture["service"]  # type: ignore[assignment]
    revisions: list[profile_service.PublishedRevision] = fixture["revisions"]  # type: ignore[assignment]
    clock: _MovableClock = fixture["clock"]  # type: ignore[assignment]
    return await service.plan_binding(
        tenant_id=fixture["tenant"],  # type: ignore[arg-type]
        profile_revision_id=revisions[revision_index].profile_revision_id,
        extension_revision_ids=tuple(extensions),
        effective_from=clock.now(),
        actor="operator@example.test",
        reason="scheduled migration",
        audit_reference="CHG-1",
    )


async def _activate(fixture: dict[str, object], binding: profile_bindings.Binding) -> profile_bindings.Binding:
    service: profile_bindings.BindingService = fixture["service"]  # type: ignore[assignment]
    await service.start_validation(
        tenant_id=fixture["tenant"],  # type: ignore[arg-type]
        binding_id=binding.binding_id,
        actor="operator@example.test",
        reason="validating",
    )
    return await service.activate(
        tenant_id=fixture["tenant"],  # type: ignore[arg-type]
        binding_id=binding.binding_id,
        actor="operator@example.test",
        reason="cutover",
        audit_reference="CHG-1",
    )


# ---------------------------------------------------------------------------
# The extension-set digest, which decides whether governance actually changed


def test_the_extension_set_digest_ignores_the_order_a_caller_listed_them_in() -> None:
    """Two bindings naming the same extensions must agree, or the value is useless."""
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    forwards = profile_bindings.extension_set_digest([first, second, third])
    backwards = profile_bindings.extension_set_digest([third, second, first])

    assert forwards == backwards


def test_the_extension_set_digest_is_over_the_set_not_the_list() -> None:
    """A repeated extension does not make a different configuration."""
    identifier = uuid.uuid4()
    assert profile_bindings.extension_set_digest([identifier]) == profile_bindings.extension_set_digest(
        [identifier, identifier]
    )


def test_the_empty_extension_set_still_has_a_digest() -> None:
    """ "Bound to core with no extensions" is a real configuration, not a null.

    Given a value so the column is never read as three-valued — a NULL there would
    make every comparison against it neither true nor false.
    """
    empty = profile_bindings.extension_set_digest([])
    assert empty
    assert empty != profile_bindings.extension_set_digest([uuid.uuid4()])


def test_a_different_extension_set_produces_a_different_digest() -> None:
    """The counterpart, so the two tests above are not satisfied by a constant."""
    assert profile_bindings.extension_set_digest([uuid.uuid4()]) != profile_bindings.extension_set_digest(
        [uuid.uuid4()]
    )


# ---------------------------------------------------------------------------
# Planning governs nothing


async def test_a_planned_binding_governs_nothing_yet(bound: dict[str, object]) -> None:
    """Planned is a draft: it records intent and leaves the tenant where it was."""
    planned = await _plan(bound)
    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]

    assert planned.state == "planned"
    assert planned.actor == "operator@example.test"
    assert planned.reason == "scheduled migration"
    assert planned.audit_reference == "CHG-1"
    assert await service.active_binding(tenant_id=bound["tenant"]) is None  # type: ignore[arg-type]


async def test_several_planned_bindings_may_exist_at_once(bound: dict[str, object]) -> None:
    """Drafting alternatives is normal; only promotion has to be exclusive.

    The exclusion constraint applies to `active` deliberately, so this is a property
    to assert rather than an accident to leave untested.
    """
    first = await _plan(bound, revision_index=0)
    second = await _plan(bound, revision_index=1)

    assert first.binding_id != second.binding_id
    assert (first.state, second.state) == ("planned", "planned")


# ---------------------------------------------------------------------------
# The declared path, and the refusal of every other one


async def test_a_binding_reaches_active_through_validation(bound: dict[str, object]) -> None:
    planned = await _plan(bound)
    active = await _activate(bound, planned)
    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]

    assert active.state == "active"
    assert active.effective_to is None
    resolved = await service.active_binding(tenant_id=bound["tenant"])  # type: ignore[arg-type]
    assert resolved is not None
    assert resolved.binding_id == planned.binding_id


async def test_a_planned_binding_cannot_skip_validation(bound: dict[str, object]) -> None:
    """The refusal that matters: activating unvalidated governs unchecked writes."""
    planned = await _plan(bound)
    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]

    with pytest.raises(profile_bindings.InvalidTransition):
        await service.activate(
            tenant_id=bound["tenant"],  # type: ignore[arg-type]
            binding_id=planned.binding_id,
            actor="operator@example.test",
            reason="skipping",
        )


async def test_an_active_binding_cannot_be_validated_again(bound: dict[str, object]) -> None:
    """Backwards is not a transition. The table has no edge and neither does this."""
    planned = await _plan(bound)
    await _activate(bound, planned)
    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]

    with pytest.raises(profile_bindings.InvalidTransition):
        await service.start_validation(
            tenant_id=bound["tenant"],  # type: ignore[arg-type]
            binding_id=planned.binding_id,
            actor="operator@example.test",
            reason="again",
        )


async def test_a_binding_that_does_not_exist_is_reported_as_such(bound: dict[str, object]) -> None:
    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]

    with pytest.raises(profile_bindings.BindingNotFound):
        await service.start_validation(
            tenant_id=bound["tenant"],  # type: ignore[arg-type]
            binding_id=uuid.uuid4(),
            actor="operator@example.test",
            reason="unknown",
        )


# ---------------------------------------------------------------------------
# One active binding, enforced by closing the outgoing one in the same transaction


async def test_activating_a_second_binding_closes_the_first(bound: dict[str, object]) -> None:
    """Read back from the table, because the invariant is about rows, not returns.

    A service that returned a plausible object while leaving two rows `active` would
    satisfy any assertion made on its return value alone.
    """
    first = await _plan(bound, revision_index=0)
    await _activate(bound, first)

    clock: _MovableClock = bound["clock"]  # type: ignore[assignment]
    clock.advance(minutes=30)
    second = await _plan(bound, revision_index=1)
    await _activate(bound, second)

    factory: async_sessionmaker[AsyncSession] = bound["factory"]  # type: ignore[assignment]
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT binding_id, state, effective_to FROM profile_bindings "
                    "WHERE tenant_id = :t ORDER BY recorded_at"
                ),
                {"t": bound["tenant"]},
            )
        ).all()

    states = {row[0]: (row[1], row[2]) for row in rows}
    assert states[first.binding_id][0] == "retired", "the outgoing binding is still live"
    assert states[first.binding_id][1] is not None, "a closed binding needs an end to its interval"
    assert states[second.binding_id][0] == "active"
    assert states[second.binding_id][1] is None


async def test_exactly_one_binding_is_active_after_a_handover(bound: dict[str, object]) -> None:
    """Stated as a count, because "none" is the failure the interval close can cause."""
    first = await _plan(bound, revision_index=0)
    await _activate(bound, first)
    clock: _MovableClock = bound["clock"]  # type: ignore[assignment]
    clock.advance(minutes=30)
    second = await _plan(bound, revision_index=1)
    await _activate(bound, second)

    factory: async_sessionmaker[AsyncSession] = bound["factory"]  # type: ignore[assignment]
    async with factory() as session:
        active = (
            await session.execute(
                text("SELECT count(*) FROM profile_bindings WHERE tenant_id = :t AND state = 'active'"),
                {"t": bound["tenant"]},
            )
        ).scalar_one()

    assert active == 1


# ---------------------------------------------------------------------------
# Rollback restores governance, and only governance


async def test_a_tenants_first_binding_has_nothing_to_roll_back_to(bound: dict[str, object]) -> None:
    """Deliberate, and the reason `rollback_ready` exists as a column.

    There is no prior behaviour to restore, so a rollback would leave the tenant
    governed by nothing — which is worse than the state it was asked to leave.
    """
    first = await _plan(bound)
    activated = await _activate(bound, first)

    assert activated.rollback_target_binding_id is None
    assert activated.rollback_ready is False

    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]
    with pytest.raises(profile_bindings.RollbackNotReady):
        await service.begin_rollback(
            tenant_id=bound["tenant"],  # type: ignore[arg-type]
            binding_id=first.binding_id,
            actor="operator@example.test",
            reason="no target",
        )


async def test_a_second_binding_records_the_one_it_displaced(bound: dict[str, object]) -> None:
    first = await _plan(bound, revision_index=0)
    await _activate(bound, first)
    clock: _MovableClock = bound["clock"]  # type: ignore[assignment]
    clock.advance(minutes=30)
    second = await _plan(bound, revision_index=1)
    activated = await _activate(bound, second)

    assert activated.rollback_target_binding_id == first.binding_id
    assert activated.rollback_ready is True


async def test_completing_a_rollback_makes_the_target_govern_again(bound: dict[str, object]) -> None:
    """The whole point: after rollback the tenant is validated against the old profile."""
    first = await _plan(bound, revision_index=0)
    await _activate(bound, first)
    clock: _MovableClock = bound["clock"]  # type: ignore[assignment]
    clock.advance(minutes=30)
    second = await _plan(bound, revision_index=1)
    await _activate(bound, second)

    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]
    clock.advance(minutes=30)
    pending = await service.begin_rollback(
        tenant_id=bound["tenant"],  # type: ignore[arg-type]
        binding_id=second.binding_id,
        actor="operator@example.test",
        reason="regression found",
    )
    assert pending.state == "rollback_pending"

    rolled_back = await service.complete_rollback(
        tenant_id=bound["tenant"],  # type: ignore[arg-type]
        binding_id=second.binding_id,
        actor="operator@example.test",
        reason="restored",
    )
    assert rolled_back.state == "rolled_back"

    resolved = await service.active_binding(tenant_id=bound["tenant"])  # type: ignore[arg-type]
    assert resolved is not None
    assert resolved.binding_id == first.binding_id, "the rollback target does not govern again"


async def test_a_rolled_back_binding_is_terminal(bound: dict[str, object]) -> None:
    """No edge leaves `rolled_back`. Re-activating it would resurrect a withdrawn profile."""
    first = await _plan(bound, revision_index=0)
    await _activate(bound, first)
    clock: _MovableClock = bound["clock"]  # type: ignore[assignment]
    clock.advance(minutes=30)
    second = await _plan(bound, revision_index=1)
    await _activate(bound, second)

    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]
    # Time has to move before the rollback closes this interval:
    # `ck_profile_bindings_interval` requires effective_to > effective_from, so a
    # binding rolled back at the instant it activated is refused by the database.
    clock.advance(minutes=30)
    await service.begin_rollback(
        tenant_id=bound["tenant"],  # type: ignore[arg-type]
        binding_id=second.binding_id,
        actor="operator@example.test",
        reason="regression",
    )
    await service.complete_rollback(
        tenant_id=bound["tenant"],  # type: ignore[arg-type]
        binding_id=second.binding_id,
        actor="operator@example.test",
        reason="restored",
    )

    with pytest.raises(profile_bindings.InvalidTransition):
        await service.start_validation(
            tenant_id=bound["tenant"],  # type: ignore[arg-type]
            binding_id=second.binding_id,
            actor="operator@example.test",
            reason="again",
        )


# ---------------------------------------------------------------------------
# The tenant comes from the caller's credential, so another tenant's id is not a key


async def test_another_tenant_cannot_read_this_tenants_binding(bound: dict[str, object]) -> None:
    """A binding id is not a capability. Every read is scoped by tenant as well."""
    planned = await _plan(bound)
    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]

    assert (
        await service.get_binding(
            tenant_id=bound["other_tenant"],  # type: ignore[arg-type]
            binding_id=planned.binding_id,
        )
        is None
    )


async def test_another_tenant_cannot_transition_this_tenants_binding(bound: dict[str, object]) -> None:
    """The write half of the same property, which is the half that matters.

    Reading someone else's binding leaks a configuration; transitioning it changes
    which profile their writes are validated against.
    """
    planned = await _plan(bound)
    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]

    with pytest.raises(profile_bindings.BindingNotFound):
        await service.start_validation(
            tenant_id=bound["other_tenant"],  # type: ignore[arg-type]
            binding_id=planned.binding_id,
            actor="intruder@example.test",
            reason="not mine",
        )

    # And it is still where its owner left it.
    unchanged = await service.get_binding(
        tenant_id=bound["tenant"],  # type: ignore[arg-type]
        binding_id=planned.binding_id,
    )
    assert unchanged is not None
    assert unchanged.state == "planned"


async def test_one_tenants_activation_does_not_close_anothers(bound: dict[str, object]) -> None:
    """The interval close is scoped, or a busy deployment would ungovern its tenants.

    `_current_active_id` looks up the outgoing binding by tenant; if it did not, the
    second tenant to activate anything would retire the first tenant's binding.
    """
    mine = await _plan(bound)
    await _activate(bound, mine)

    other_service = profile_bindings.BindingService(
        bound["factory"],  # type: ignore[arg-type]
        clock=bound["clock"],  # type: ignore[arg-type]
    )
    revisions: list[profile_service.PublishedRevision] = bound["revisions"]  # type: ignore[assignment]
    clock: _MovableClock = bound["clock"]  # type: ignore[assignment]
    clock.advance(minutes=10)
    theirs = await other_service.plan_binding(
        tenant_id=bound["other_tenant"],  # type: ignore[arg-type]
        profile_revision_id=revisions[1].profile_revision_id,
        effective_from=clock.now(),
        actor="them@example.test",
        reason="their migration",
    )
    await other_service.start_validation(
        tenant_id=bound["other_tenant"],  # type: ignore[arg-type]
        binding_id=theirs.binding_id,
        actor="them@example.test",
        reason="validating",
    )
    await other_service.activate(
        tenant_id=bound["other_tenant"],  # type: ignore[arg-type]
        binding_id=theirs.binding_id,
        actor="them@example.test",
        reason="cutover",
    )

    service: profile_bindings.BindingService = bound["service"]  # type: ignore[assignment]
    still_mine = await service.active_binding(tenant_id=bound["tenant"])  # type: ignore[arg-type]
    assert still_mine is not None
    assert still_mine.binding_id == mine.binding_id, "another tenant's activation retired this one"


async def test_a_binding_cannot_be_closed_at_the_instant_it_opened(bound: dict[str, object]) -> None:
    """A governed interval with no duration governs nothing, and is refused.

    Found by writing a rollback test that did not advance the clock:
    `ck_profile_bindings_interval` requires `effective_to > effective_from`, so
    closing a binding in the same instant it activated fails. Worth pinning rather
    than working around, because the alternative — a zero-length active interval —
    would be a row that claims to have governed a period during which no write
    could have been validated against it.
    """
    first = await _plan(bound, revision_index=0)
    await _activate(bound, first)
    second = await _plan(bound, revision_index=1)

    # No clock advance: activating `second` would close `first` at its own start.
    with pytest.raises(Exception, match="ck_profile_bindings_interval"):
        await _activate(bound, second)
