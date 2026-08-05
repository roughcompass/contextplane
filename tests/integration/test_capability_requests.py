"""The loop's return path: what consuming teams need, routed to whoever can act.

Everything before this ran one direction — observe, extract, score, settle, promote,
serve. These tests cover the first path that runs the other way.

The properties worth breaking on purpose: a requester must not be able to decide their
own request, a decline must carry a reason, a declined request must survive as demand
signal rather than being deleted, and the requester must be able to see all of it. The
last one is the whole point — an invisible queue is indistinguishable from being ignored,
and being ignored is what pushes teams back to Slack.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.audit import actions
from registry.exceptions import ConflictError, NotFoundError, ValidationError
from registry.service.memory.capability_requests import (
    ALLOWED_TRANSITIONS,
    REQUEST_CATEGORIES,
    STATUS_ACCEPTED,
    STATUS_ACKNOWLEDGED,
    STATUS_DECLINED,
    STATUS_DUPLICATE,
    STATUS_RAISED,
    STATUS_RESOLVED,
    CapabilityRequestService,
)
from registry.service.memory.source_governance import SourceGovernanceService
from registry.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def ontology(factory: async_sessionmaker[AsyncSession]) -> None:
    """Needed only by the tests that stage a real claim to link a request to."""
    from registry.service.catalog.global_vocabulary import GlobalVocabularyService
    from registry.service.memory.claim_ontology import seed_ontology

    await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))


@pytest.fixture
def requests_svc(factory: async_sessionmaker[AsyncSession]) -> CapabilityRequestService:
    return CapabilityRequestService(factory, clock=FakeClock(_NOW))


@pytest.fixture
def governance(factory: async_sessionmaker[AsyncSession]) -> SourceGovernanceService:
    return SourceGovernanceService(factory, clock=FakeClock(_NOW))


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"req-{tid.hex[:8]}", "now": _NOW},
        )
    return tid


async def _seed_actor(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                "                    actor_kind, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, 'human', :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return aid


async def _seed_entity(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    eid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, "
                "                      is_active, created_at) "
                "VALUES (:eid, :tid, 'capability', :name, 'public', TRUE, :now)"
            ),
            {"eid": eid, "tid": tid, "name": f"cap-{eid.hex[:8]}", "now": _NOW},
        )
    return eid


async def _seed_source(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name, "
                "                          config, is_active, created_at) "
                "VALUES (:sid, :tid, 'openapi', 'src', '{}'::jsonb, TRUE, :now)"
            ),
            {"sid": sid, "tid": tid, "now": _NOW},
        )
    return sid


def _ctx(tid: uuid.UUID, aid: uuid.UUID, *, roles: list[str] | None = None) -> TenantContext:
    return TenantContext(tenant_id=tid, actor_id=aid, roles=roles or ["producer"], oidc_subject="s")


async def _audit_actions(factory: async_sessionmaker[AsyncSession], target: uuid.UUID) -> list[str]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text("SELECT action FROM audit_log WHERE target_id = :t ORDER BY ts"),
                    {"t": target},
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


# --- exit criterion 1: routing ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_routes_to_the_capabilitys_owner_not_the_requester(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    """The owner is resolved from the subject rather than supplied. A requester who
    could address their request would be choosing who decides it."""
    owner = await _seed_tenant(factory)
    consumer = await _seed_tenant(factory)
    consumer_actor = await _seed_actor(factory, consumer)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(consumer, consumer_actor),
        subject_entity_id=subject,
        request_category="interface_change",
        title="needs an idempotency key",
        body="retries double-charge without one",
    )

    assert request.owner_tenant_id == owner
    assert request.requester_tenant_id == consumer
    assert request.status == STATUS_RAISED
    assert actions.REQUEST_RAISED in await _audit_actions(factory, request.request_id)


@pytest.mark.asyncio
async def test_the_requester_cannot_transition_their_own_request(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    """Otherwise the surface is a suggestion box the suggester can also stamp."""
    owner = await _seed_tenant(factory)
    consumer = await _seed_tenant(factory)
    consumer_actor = await _seed_actor(factory, consumer)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(consumer, consumer_actor),
        subject_entity_id=subject,
        request_category="interface_change",
        title="needs a batch variant",
        body="one call per row does not scale",
    )

    with pytest.raises(PermissionError, match="owns the capability"):
        await requests_svc.transition(
            _ctx(consumer, consumer_actor),
            request_id=request.request_id,
            to_status=STATUS_ACCEPTED,
        )


@pytest.mark.asyncio
async def test_the_right_tenant_with_the_wrong_role_is_also_refused(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    """Tenant and role are separate conditions, so satisfying one does not satisfy
    the other by accident."""
    owner = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(owner, owner_actor),
        subject_entity_id=subject,
        request_category="documentation",
        title="examples are wrong",
        body="the curl example omits the tenant header",
    )

    with pytest.raises(PermissionError, match="producer or admin"):
        await requests_svc.transition(
            _ctx(owner, owner_actor, roles=["consumer"]),
            request_id=request.request_id,
            to_status=STATUS_ACKNOWLEDGED,
        )


@pytest.mark.asyncio
async def test_a_request_is_visible_alongside_the_claims_about_the_same_capability(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    owner = await _seed_tenant(factory)
    consumer = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    consumer_actor = await _seed_actor(factory, consumer)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(consumer, consumer_actor),
        subject_entity_id=subject,
        request_category="operational",
        title="publish an SLO",
        body="we cannot set our own without yours",
    )

    for viewer in (_ctx(owner, owner_actor), _ctx(consumer, consumer_actor)):
        found = await requests_svc.for_subject(viewer, subject)
        assert [r.request_id for r in found] == [request.request_id]


@pytest.mark.asyncio
async def test_a_third_tenant_sees_neither_the_request_nor_its_history(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    """What one team asked another for is between them. A bystander who could read it
    learns both that the capability exists and what it is missing."""
    owner = await _seed_tenant(factory)
    consumer = await _seed_tenant(factory)
    bystander = await _seed_tenant(factory)
    consumer_actor = await _seed_actor(factory, consumer)
    bystander_actor = await _seed_actor(factory, bystander)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(consumer, consumer_actor),
        subject_entity_id=subject,
        request_category="defect",
        title="429 has no retry-after",
        body="clients back off blindly",
    )

    viewer = _ctx(bystander, bystander_actor)
    assert await requests_svc.get(viewer, request.request_id) is None
    assert await requests_svc.for_subject(viewer, subject) == ()
    assert await requests_svc.history(viewer, request.request_id) == ()


# --- exit criterion 2: decline with a reason, kept as signal ---------------------


@pytest.mark.asyncio
async def test_acknowledging_then_declining_keeps_the_request_and_its_reason(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    """A declined request is demand signal. Deleting it makes the owner's position
    look unanimous and leaves the requester unable to tell refusal from neglect."""
    owner = await _seed_tenant(factory)
    consumer = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    consumer_actor = await _seed_actor(factory, consumer)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(consumer, consumer_actor),
        subject_entity_id=subject,
        request_category="new_capability",
        title="webhook for status changes",
        body="polling is wasteful at our volume",
    )

    await requests_svc.transition(
        _ctx(owner, owner_actor), request_id=request.request_id, to_status=STATUS_ACKNOWLEDGED
    )
    await requests_svc.transition(
        _ctx(owner, owner_actor),
        request_id=request.request_id,
        to_status=STATUS_DECLINED,
        reason="planned for next quarter; tracked externally",
    )

    # The requester's own view, which is the one that matters here.
    seen = await requests_svc.get(_ctx(consumer, consumer_actor), request.request_id)
    assert seen is not None
    assert seen.status == STATUS_DECLINED
    assert "next quarter" in (seen.decision_reason or "")

    history = await requests_svc.history(_ctx(consumer, consumer_actor), request.request_id)
    assert [(t.from_status, t.to_status) for t in history] == [
        (STATUS_RAISED, STATUS_ACKNOWLEDGED),
        (STATUS_ACKNOWLEDGED, STATUS_DECLINED),
    ]


@pytest.mark.asyncio
async def test_declining_without_a_reason_is_refused(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    """A decline with no reason is indistinguishable from neglect from the other
    side, which is the failure the whole surface exists to prevent."""
    owner = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(owner, owner_actor),
        subject_entity_id=subject,
        request_category="defect",
        title="timeouts under load",
        body="p99 exceeds the documented budget",
    )

    with pytest.raises(ValidationError, match="requires a reason"):
        await requests_svc.transition(
            _ctx(owner, owner_actor), request_id=request.request_id, to_status=STATUS_DECLINED
        )


@pytest.mark.asyncio
async def test_marking_a_duplicate_also_requires_saying_of_what(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    owner = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(owner, owner_actor),
        subject_entity_id=subject,
        request_category="operational",
        title="raise the rate limit",
        body="we hit it every morning",
    )

    with pytest.raises(ValidationError, match="requires a reason"):
        await requests_svc.transition(
            _ctx(owner, owner_actor), request_id=request.request_id, to_status=STATUS_DUPLICATE
        )


@pytest.mark.asyncio
async def test_a_declined_request_cannot_be_reopened(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    """Reopening is raising a new request, which keeps the original refusal on the
    record. A status that could be walked backwards would let a decision be undone
    without a trace of it having been made."""
    owner = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(owner, owner_actor),
        subject_entity_id=subject,
        request_category="defect",
        title="stale cache",
        body="reads lag writes by minutes",
    )
    await requests_svc.transition(
        _ctx(owner, owner_actor),
        request_id=request.request_id,
        to_status=STATUS_DECLINED,
        reason="working as intended",
    )

    with pytest.raises(ConflictError, match="terminal"):
        await requests_svc.transition(
            _ctx(owner, owner_actor), request_id=request.request_id, to_status=STATUS_ACKNOWLEDGED
        )


@pytest.mark.asyncio
async def test_a_raised_request_cannot_skip_straight_to_accepted(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    """Acknowledgement is the step that tells the requester a human has seen it.
    Allowing a jump past it would let the most useful signal in the lifecycle be
    skipped for convenience."""
    owner = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(owner, owner_actor),
        subject_entity_id=subject,
        request_category="interface_change",
        title="add a cursor",
        body="offset paging is unstable",
    )

    with pytest.raises(ConflictError, match="cannot become accepted"):
        await requests_svc.transition(
            _ctx(owner, owner_actor), request_id=request.request_id, to_status=STATUS_ACCEPTED
        )


def test_every_terminal_status_is_terminal_and_every_other_has_a_way_out() -> None:
    """A status with no exits and no reason to be terminal is a dead end somebody
    will hit; one with exits that should be terminal is a decision that can be
    quietly undone."""
    terminal = {STATUS_DECLINED, STATUS_DUPLICATE, STATUS_RESOLVED}
    for status, exits in ALLOWED_TRANSITIONS.items():
        if status in terminal:
            assert exits == frozenset(), f"{status} should be terminal"
        else:
            assert exits, f"{status} has no way out"


# --- exit criterion 3: the loop closes visibly -----------------------------------


@pytest.mark.asyncio
async def test_an_accepted_request_can_point_at_the_change_it_produced(
    factory: async_sessionmaker[AsyncSession],
    requests_svc: CapabilityRequestService,
    ontology: None,
) -> None:
    """ "Accepted" tells a requester somebody agreed. A link to the change tells them
    it happened, which is the part they wanted."""
    owner = await _seed_tenant(factory)
    consumer = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    consumer_actor = await _seed_actor(factory, consumer)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(consumer, consumer_actor),
        subject_entity_id=subject,
        request_category="documentation",
        title="document the retry policy",
        body="we guessed and got it wrong",
    )
    await requests_svc.transition(
        _ctx(owner, owner_actor), request_id=request.request_id, to_status=STATUS_ACKNOWLEDGED
    )
    await requests_svc.transition(_ctx(owner, owner_actor), request_id=request.request_id, to_status=STATUS_ACCEPTED)

    promotion_id = await _seed_promotion(factory, owner, owner_actor, subject)
    await requests_svc.link_to_promotion(
        _ctx(owner, owner_actor), request_id=request.request_id, promotion_id=promotion_id
    )

    seen = await requests_svc.get(_ctx(consumer, consumer_actor), request.request_id)
    assert seen is not None
    assert seen.resulting_promotion_id == promotion_id
    assert actions.REQUEST_LINKED_TO_CHANGE in await _audit_actions(factory, request.request_id)


@pytest.mark.asyncio
async def test_a_declined_request_cannot_point_at_a_change(
    factory: async_sessionmaker[AsyncSession],
    requests_svc: CapabilityRequestService,
    ontology: None,
) -> None:
    """A declined request linked to a change would describe a decision nobody made."""
    owner = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    subject = await _seed_entity(factory, owner)

    request = await requests_svc.raise_request(
        _ctx(owner, owner_actor),
        subject_entity_id=subject,
        request_category="defect",
        title="fix the 500",
        body="empty body returns 500 not 400",
    )
    await requests_svc.transition(
        _ctx(owner, owner_actor),
        request_id=request.request_id,
        to_status=STATUS_DECLINED,
        reason="cannot reproduce",
    )
    promotion_id = await _seed_promotion(factory, owner, owner_actor, subject)

    with pytest.raises(ConflictError, match="cannot point at a change"):
        await requests_svc.link_to_promotion(
            _ctx(owner, owner_actor), request_id=request.request_id, promotion_id=promotion_id
        )


@pytest.mark.asyncio
async def test_a_requester_sees_what_they_asked_for_and_where_it_got_to(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    owner = await _seed_tenant(factory)
    consumer = await _seed_tenant(factory)
    consumer_actor = await _seed_actor(factory, consumer)
    subject = await _seed_entity(factory, owner)

    for index in range(2):
        await requests_svc.raise_request(
            _ctx(consumer, consumer_actor),
            subject_entity_id=subject,
            request_category="operational",
            title=f"request {index}",
            body="body",
        )

    mine = await requests_svc.raised_by(_ctx(consumer, consumer_actor))
    assert len(mine) == 2
    assert all(r.status == STATUS_RAISED for r in mine)


@pytest.mark.asyncio
async def test_an_owner_queue_shows_only_what_is_still_open(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    owner = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    subject = await _seed_entity(factory, owner)

    open_request = await requests_svc.raise_request(
        _ctx(owner, owner_actor),
        subject_entity_id=subject,
        request_category="operational",
        title="still open",
        body="body",
    )
    closed = await requests_svc.raise_request(
        _ctx(owner, owner_actor),
        subject_entity_id=subject,
        request_category="operational",
        title="already declined",
        body="body",
    )
    await requests_svc.transition(
        _ctx(owner, owner_actor),
        request_id=closed.request_id,
        to_status=STATUS_DECLINED,
        reason="not doing this",
    )

    queue = await requests_svc.for_owner(_ctx(owner, owner_actor))
    assert [r.request_id for r in queue] == [open_request.request_id]

    everything = await requests_svc.for_owner(_ctx(owner, owner_actor), open_only=False)
    assert len(everything) == 2


# --- validation ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_about_a_capability_that_does_not_exist_is_refused(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    consumer = await _seed_tenant(factory)
    consumer_actor = await _seed_actor(factory, consumer)

    with pytest.raises(NotFoundError, match="no such capability"):
        await requests_svc.raise_request(
            _ctx(consumer, consumer_actor),
            subject_entity_id=uuid.uuid4(),
            request_category="defect",
            title="t",
            body="b",
        )


@pytest.mark.asyncio
async def test_an_unknown_category_is_refused(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    owner = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    subject = await _seed_entity(factory, owner)

    with pytest.raises(ValidationError, match="request_category"):
        await requests_svc.raise_request(
            _ctx(owner, owner_actor),
            subject_entity_id=subject,
            request_category="whatever",
            title="t",
            body="b",
        )
    assert "defect" in REQUEST_CATEGORIES


@pytest.mark.asyncio
async def test_an_empty_title_or_body_is_refused(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    """A request with no content cannot be acted on, and an owner cannot decline what
    they cannot read without looking arbitrary."""
    owner = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    subject = await _seed_entity(factory, owner)

    for title, body in (("   ", "b"), ("t", "")):
        with pytest.raises(ValidationError, match="must not be empty"):
            await requests_svc.raise_request(
                _ctx(owner, owner_actor),
                subject_entity_id=subject,
                request_category="defect",
                title=title,
                body=body,
            )


# --- exit criteria 6 and 7: source governance ------------------------------------


@pytest.mark.asyncio
async def test_a_source_that_declared_nothing_may_not_write(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    """The enforcement behind "declare authority first". The check is on every
    ingest, not a lint at registration time."""
    tid = await _seed_tenant(factory)
    source_id = await _seed_source(factory, tid)

    admission = await governance.admit(source_id)
    assert not admission.permitted
    assert "has not declared an authority tier" in (admission.reason or "")


@pytest.mark.asyncio
async def test_an_invalid_authority_tier_is_refused(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    """A typo'd tier would otherwise register as something ranking below everything,
    silently making the source's claims worthless rather than failing."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source_id = await _seed_source(factory, tid)

    with pytest.raises(ValidationError, match="authority_tier must be"):
        await governance.declare(_ctx(tid, aid), source_id=source_id, authority_tier="pretty_reliable")


@pytest.mark.asyncio
async def test_a_declared_source_may_write_up_to_its_ceiling(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source_id = await _seed_source(factory, tid)
    await governance.declare(
        _ctx(tid, aid), source_id=source_id, authority_tier="observer_extraction", ingest_ceiling=3
    )

    for _ in range(3):
        assert (await governance.admit(source_id)).permitted
    assert actions.SOURCE_AUTHORITY_DECLARED in await _audit_actions(factory, source_id)


@pytest.mark.asyncio
async def test_exceeding_the_ceiling_opens_the_circuit_and_counts_the_breach(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    """Unbounded ingest is a denial-of-usefulness risk even when every claim is
    valid: a staging store nobody can review is not better than an empty one."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source_id = await _seed_source(factory, tid)
    await governance.declare(
        _ctx(tid, aid), source_id=source_id, authority_tier="observer_extraction", ingest_ceiling=2
    )

    assert (await governance.admit(source_id, count=2)).permitted
    refused = await governance.admit(source_id)
    assert not refused.permitted
    assert "ingest ceiling" in (refused.reason or "")

    policy = await governance.policy_for(source_id)
    assert policy is not None
    assert policy.breach_count == 1
    assert policy.breaker_open_until is not None
    assert actions.SOURCE_BREAKER_OPENED in await _audit_actions(factory, source_id)


@pytest.mark.asyncio
async def test_an_over_ceiling_batch_is_refused_whole_rather_than_partially_admitted(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    """A connector that got half its claims through would leave the store holding an
    arbitrary prefix of a document, which is worse than none of it."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source_id = await _seed_source(factory, tid)
    await governance.declare(
        _ctx(tid, aid), source_id=source_id, authority_tier="observer_extraction", ingest_ceiling=5
    )

    assert not (await governance.admit(source_id, count=6)).permitted
    policy = await governance.policy_for(source_id)
    assert policy is not None and policy.breaker_open_until is not None


@pytest.mark.asyncio
async def test_an_open_breaker_keeps_refusing_until_it_expires(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source_id = await _seed_source(factory, tid)
    await governance.declare(
        _ctx(tid, aid), source_id=source_id, authority_tier="observer_extraction", ingest_ceiling=1
    )

    assert (await governance.admit(source_id)).permitted
    assert not (await governance.admit(source_id)).permitted

    again = await governance.admit(source_id)
    assert not again.permitted
    assert "circuit open until" in (again.reason or "")


@pytest.mark.asyncio
async def test_the_breaker_state_survives_a_new_service_instance(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    """A breaker held in memory reopens on every deploy, which turns a rate limit
    into a rate limit between restarts."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source_id = await _seed_source(factory, tid)
    await governance.declare(
        _ctx(tid, aid), source_id=source_id, authority_tier="observer_extraction", ingest_ceiling=1
    )
    await governance.admit(source_id)
    await governance.admit(source_id)

    restarted = SourceGovernanceService(factory, clock=FakeClock(_NOW))
    assert not (await restarted.admit(source_id)).permitted


@pytest.mark.asyncio
async def test_a_reset_closes_the_breaker_without_restating_the_policy(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    """Restating the tier and ceiling to clear a breaker is how a cleanup silently
    changes a policy."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source_id = await _seed_source(factory, tid)
    await governance.declare(
        _ctx(tid, aid),
        source_id=source_id,
        authority_tier="observer_human",
        ingest_ceiling=1,
        window_seconds=60,
    )
    await governance.admit(source_id)
    await governance.admit(source_id)

    await governance.reset_breaker(_ctx(tid, aid), source_id)

    policy = await governance.policy_for(source_id)
    assert policy is not None
    assert policy.breaker_open_until is None
    assert policy.authority_tier == "observer_human", "the tier was not disturbed"
    assert policy.ingest_ceiling == 1
    assert policy.window_seconds == 60
    assert (await governance.admit(source_id)).permitted


@pytest.mark.asyncio
async def test_the_window_rolls_over_and_the_allowance_returns(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source_id = await _seed_source(factory, tid)
    await governance.declare(
        _ctx(tid, aid),
        source_id=source_id,
        authority_tier="observer_extraction",
        ingest_ceiling=1,
        window_seconds=60,
    )
    assert (await governance.admit(source_id)).permitted

    later = SourceGovernanceService(factory, clock=FakeClock(_NOW + datetime.timedelta(seconds=120)))
    assert (await later.admit(source_id)).permitted


@pytest.mark.asyncio
async def test_only_the_owning_tenant_may_govern_a_source(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    owner = await _seed_tenant(factory)
    stranger = await _seed_tenant(factory)
    stranger_actor = await _seed_actor(factory, stranger)
    source_id = await _seed_source(factory, owner)

    with pytest.raises(PermissionError, match="only the owning tenant"):
        await governance.declare(
            _ctx(stranger, stranger_actor),
            source_id=source_id,
            authority_tier="observer_extraction",
        )


@pytest.mark.asyncio
async def test_declaring_a_source_id_that_does_not_exist_is_refused(
    factory: async_sessionmaker[AsyncSession], governance: SourceGovernanceService
) -> None:
    """Distinct from the wrong-tenant refusal above: nothing named by this id
    exists at all, so there is no owner to check standing against."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)

    with pytest.raises(NotFoundError, match="no such source"):
        await governance.declare(_ctx(tid, aid), source_id=uuid.uuid4(), authority_tier="observer_extraction")


async def _seed_promotion(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    subject: uuid.UUID,
) -> uuid.UUID:
    """A promotion journal row, so the request-to-change link has a real target.

    The claim goes through the write path rather than a hand-rolled insert. Staging a
    claim has invariants -- a staged claim must carry a score, and the score must
    carry the inputs that produced it -- and a test fixture that bypassed them would
    be constructing a row the product cannot produce.

    The proposal and journal rows are inserted directly, because driving the whole
    promotion path here would make these tests fail for reasons that have nothing to
    do with requests.
    """
    from registry.service.memory.claim_authority import Evidence
    from registry.service.memory.claim_writer import ClaimService

    claim = await ClaimService(factory, clock=FakeClock(_NOW)).stage_claim(
        _ctx(tenant_id, actor_id),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    proposal_id, promotion_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO memory_promotion_proposal (proposal_id, claim_id, owner_tenant_id, "
                "  author_tenant_id, subject_entity_id, predicate, target_kind, target_key, "
                "  mapping_version, proposed_value, valid_from, state, decided_by, "
                "  decided_at, created_at) "
                "VALUES (:pid, :cid, :tid, :tid, :sid, 'owned_by_team', 'attribute', "
                "        'owned_by_team', 1, '\"platform\"'::jsonb, :now, 'accepted', "
                "        :aid, :now, :now)"
            ),
            {"pid": proposal_id, "cid": claim.claim_id, "tid": tenant_id, "sid": subject, "aid": actor_id, "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO memory_promotion_journal (promotion_id, proposal_id, claim_id, "
                "  tenant_id, target_kind, created_row_id, promoted_at, promoted_by) "
                "VALUES (:prid, :pid, :cid, :tid, 'attribute', :row, :now, :aid)"
            ),
            {
                "prid": promotion_id,
                "pid": proposal_id,
                "cid": claim.claim_id,
                "tid": tenant_id,
                "row": uuid.uuid4(),
                "now": _NOW,
                "aid": actor_id,
            },
        )
    return promotion_id


# --- exit criteria 4 and 5: the new sources --------------------------------------


@pytest.mark.asyncio
async def test_a_runbook_page_lands_claims_provenanced_to_page_and_revision(
    factory: async_sessionmaker[AsyncSession],
    governance: SourceGovernanceService,
    ontology: None,
) -> None:
    """A runbook says different things in different revisions. Provenance naming only
    the page would point at whatever it says today, not the text the claim came
    from."""
    from registry.service.memory.claim_writer import ClaimService
    from registry.service.memory.source_ingest import SourceIngestService, parse_document

    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    source_id = await _seed_source(factory, tid)
    await governance.declare(_ctx(tid, aid), source_id=source_id, authority_tier="observer_extraction")

    candidates = parse_document(
        subject_reference=str(subject),
        page_id="CONF-8891",
        revision="r7",
        body="Owner: platform\nRunbook: https://runbooks/auth\nRTO: 900 seconds\n",
    )
    assert {c.predicate for c in candidates} == {
        "owned_by_team",
        "runbook_url",
        "recovery_time_objective_seconds",
    }

    ingest = SourceIngestService(claims=ClaimService(factory, clock=FakeClock(_NOW)), governance=governance)
    result = await ingest.ingest(_ctx(tid, aid), source_id=source_id, candidates=candidates)
    assert result.admitted
    assert result.written == 3

    async with factory() as session:
        refs = (
            await session.execute(
                text(
                    "SELECT DISTINCT evidence_kind, evidence_ref FROM memory_claim_provenance p "
                    "  JOIN memory_claims c ON c.claim_id = p.claim_id "
                    " WHERE c.subject_entity_id = :sid"
                ),
                {"sid": subject},
            )
        ).all()
    assert refs == [("document_revision", "CONF-8891@r7")]


@pytest.mark.asyncio
async def test_a_runbook_claim_does_not_get_owner_sync_authority(
    factory: async_sessionmaker[AsyncSession],
    governance: SourceGovernanceService,
    ontology: None,
) -> None:
    """A page is not an API spec. Authority is derived from the evidence rather than
    supplied, so a document-derived claim cannot reach the tier a registered
    deterministic connector earns -- whatever the source declared."""
    from registry.service.memory.claim_writer import ClaimService
    from registry.service.memory.source_ingest import SourceIngestService, parse_document

    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    source_id = await _seed_source(factory, tid)
    # Declared at the strongest tier on purpose: the declaration must not be able to
    # buy authority the evidence does not support.
    await governance.declare(_ctx(tid, aid), source_id=source_id, authority_tier="owner_human")

    ingest = SourceIngestService(claims=ClaimService(factory, clock=FakeClock(_NOW)), governance=governance)
    await ingest.ingest(
        _ctx(tid, aid),
        source_id=source_id,
        candidates=parse_document(
            subject_reference=str(subject),
            page_id="CONF-1",
            revision="r1",
            body="Owner: platform\n",
        ),
    )

    async with factory() as session:
        authority = (
            await session.execute(
                text("SELECT source_authority FROM memory_claims WHERE subject_entity_id = :sid"),
                {"sid": subject},
            )
        ).scalar_one()
    assert authority == "owner_inference", "a page earned a tier its evidence cannot support"


@pytest.mark.asyncio
async def test_an_incident_claim_is_a_historical_fact_not_a_decaying_assertion(
    factory: async_sessionmaker[AsyncSession],
    governance: SourceGovernanceService,
    ontology: None,
) -> None:
    """An incident happened. A service having failed last March is not less true in
    April, so its claims must not drift toward the floor the way an assertion about
    current state does."""
    from registry.service.memory.claim_writer import ClaimService
    from registry.service.memory.confidence_decay import CATEGORY_HALF_LIFE_DAYS
    from registry.service.memory.source_ingest import SourceIngestService, parse_incident

    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    source_id = await _seed_source(factory, tid)
    await governance.declare(_ctx(tid, aid), source_id=source_id, authority_tier="observer_extraction")

    occurred = _NOW - datetime.timedelta(days=120)
    candidates = parse_incident(
        subject_reference=str(subject),
        incident_id="INC-42",
        report_url="https://incidents/42",
        occurred_at=occurred,
        summary="auth service returned 500s for 20 minutes",
    )
    ingest = SourceIngestService(claims=ClaimService(factory, clock=FakeClock(_NOW)), governance=governance)
    assert (await ingest.ingest(_ctx(tid, aid), source_id=source_id, candidates=candidates)).written == 2

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT claim_category, asserted_valid_from FROM memory_claims "
                        " WHERE subject_entity_id = :sid"
                    ),
                    {"sid": subject},
                )
            )
            .mappings()
            .all()
        )

    assert {r["claim_category"] for r in rows} == {"incident_history"}
    assert {r["asserted_valid_from"] for r in rows} == {
        occurred
    }, "an incident holds from when it happened, not from when the connector ran"
    # The category that makes it a historical fact rather than a current-state claim.
    assert CATEGORY_HALF_LIFE_DAYS["incident_history"] >= 3650.0
    assert CATEGORY_HALF_LIFE_DAYS["incident_history"] > CATEGORY_HALF_LIFE_DAYS["interface_contract"]


@pytest.mark.asyncio
async def test_a_work_item_claim_records_in_flight_change_not_a_property(
    factory: async_sessionmaker[AsyncSession],
    governance: SourceGovernanceService,
    ontology: None,
) -> None:
    """The connector does not read the ticket's content. Inferring capability
    properties from a human note would be guessing with a citation attached."""
    from registry.service.memory.source_ingest import parse_work_item

    candidates = parse_work_item(
        subject_reference="cap",
        item_key="PLAT-1234",
        url="https://jira/PLAT-1234",
        summary="add idempotency keys to the charge endpoint",
    )
    assert [c.predicate for c in candidates] == ["work_item_url"]
    assert candidates[0].evidence[0].kind == "work_item"
    assert candidates[0].evidence[0].ref == "PLAT-1234"


@pytest.mark.asyncio
async def test_a_connector_cannot_write_before_its_source_declares(
    factory: async_sessionmaker[AsyncSession],
    governance: SourceGovernanceService,
    ontology: None,
) -> None:
    """The gate is on the write, not on registration. A connector that could write
    first and be governed later would have already put rows in the store."""
    from registry.service.memory.claim_writer import ClaimService
    from registry.service.memory.source_ingest import SourceIngestService, parse_document

    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    source_id = await _seed_source(factory, tid)

    ingest = SourceIngestService(claims=ClaimService(factory, clock=FakeClock(_NOW)), governance=governance)
    result = await ingest.ingest(
        _ctx(tid, aid),
        source_id=source_id,
        candidates=parse_document(subject_reference=str(subject), page_id="P", revision="r1", body="Owner: platform\n"),
    )

    assert not result.admitted
    assert result.written == 0
    assert "has not declared an authority tier" in (result.refused_reason or "")


@pytest.mark.asyncio
async def test_a_batch_over_the_ceiling_writes_nothing_at_all(
    factory: async_sessionmaker[AsyncSession],
    governance: SourceGovernanceService,
    ontology: None,
) -> None:
    """Half a document in the store is harder to reason about than none of it: a
    curator cannot tell a page that said three things from a page that said six and
    was cut off."""
    from registry.service.memory.claim_writer import ClaimService
    from registry.service.memory.source_ingest import SourceIngestService, parse_document

    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    source_id = await _seed_source(factory, tid)
    await governance.declare(
        _ctx(tid, aid), source_id=source_id, authority_tier="observer_extraction", ingest_ceiling=2
    )

    ingest = SourceIngestService(claims=ClaimService(factory, clock=FakeClock(_NOW)), governance=governance)
    result = await ingest.ingest(
        _ctx(tid, aid),
        source_id=source_id,
        candidates=parse_document(
            subject_reference=str(subject),
            page_id="P",
            revision="r1",
            body="Owner: platform\nRunbook: https://r/1\nRTO: 60 seconds\n",
        ),
    )

    assert not result.admitted
    assert result.written == 0
    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM memory_claims WHERE subject_entity_id = :sid"),
                {"sid": subject},
            )
        ).scalar_one()
    assert count == 0, "a refused batch left rows behind"


# --- loop observability: the return arrow's own two counters -----------------


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.asyncio
async def test_raising_increments_the_raised_counter(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    owner = await _seed_tenant(factory)
    consumer = await _seed_tenant(factory)
    consumer_actor = await _seed_actor(factory, consumer)
    subject = await _seed_entity(factory, owner)
    before = _sample("registry_capability_request_raised_total")

    await requests_svc.raise_request(
        _ctx(consumer, consumer_actor),
        subject_entity_id=subject,
        request_category="interface_change",
        title="needs an idempotency key",
        body="retries double-charge without one",
    )

    assert _sample("registry_capability_request_raised_total") == before + 1


@pytest.mark.asyncio
async def test_transitioning_increments_the_decided_counter_by_the_status_reached(
    factory: async_sessionmaker[AsyncSession], requests_svc: CapabilityRequestService
) -> None:
    owner = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    consumer = await _seed_tenant(factory)
    consumer_actor = await _seed_actor(factory, consumer)
    subject = await _seed_entity(factory, owner)
    before = _sample("registry_capability_request_decided_total", to_status=STATUS_ACKNOWLEDGED)

    request = await requests_svc.raise_request(
        _ctx(consumer, consumer_actor),
        subject_entity_id=subject,
        request_category="documentation",
        title="examples are wrong",
        body="the curl example omits the tenant header",
    )
    await requests_svc.transition(
        _ctx(owner, owner_actor), request_id=request.request_id, to_status=STATUS_ACKNOWLEDGED
    )

    assert _sample("registry_capability_request_decided_total", to_status=STATUS_ACKNOWLEDGED) == before + 1
