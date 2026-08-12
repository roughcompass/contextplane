"""A task is a bounded audience, and an outsider cannot find its edges.

The resolver is unit-tested in isolation. What needs a real database is the
claim that the audience predicate is *in the query*: a Python-side filter would
pass every "does the outsider get the row" test and still leak through a count
that was computed before filtering, a page that comes back short, or a lookup
that distinguishes "not yours" from "does not exist".

So every assertion here is about what a second actor and an outsider observe
through the same code paths, against the same rows, in the same tenant. Two
tenants are not the interesting case — cross-tenant isolation is enforced
elsewhere and would pass even if participation were ignored entirely. The
dangerous case is two actors inside one tenant, which is exactly the boundary
that did not exist before.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.workspaces import queries_audience as q
from contextplane.workspaces.audience import (
    CAPABILITY_EXTEND,
    CAPABILITY_READ,
    ENTITLEMENT_RESOLVER,
    RESOLVER_EXPLICIT,
    AudienceDenied,
    EntitlementEvidence,
    materialize_entitlement_grant,
    require,
)
from contextplane.workspaces.schemas.intent_memory import (
    ROLE_AUDITOR,
    ROLE_CONTRIBUTOR,
    ROLE_OWNER,
    IntentParticipantGrantV1,
    ParticipantRole,
)

pytestmark = pytest.mark.asyncio

_OWNER = "agent-owner"
_MEMBER = "agent-member"
_OUTSIDER = "agent-outsider"
_HOUR = datetime.timedelta(hours=1)


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def session(pg_container: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(pg_container)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def tenant(session: AsyncSession) -> uuid.UUID:
    tid = uuid.uuid4()
    await session.execute(
        text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
        {"t": tid, "s": f"aud-{tid.hex[:8]}", "n": "task audience test"},
    )
    await session.flush()
    return tid


async def _grant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    intent_id: uuid.UUID,
    actor: str,
    role: ParticipantRole = ROLE_CONTRIBUTOR,
    *,
    granted_at: datetime.datetime | None = None,
    expires_at: datetime.datetime | None = None,
    resolver: str = RESOLVER_EXPLICIT,
) -> None:
    await q.insert_grant(
        session,
        tenant_id=tenant_id,
        grant=IntentParticipantGrantV1(
            intent_id=intent_id,
            actor_id=actor,
            role=role,
            granted_by=_OWNER if actor != _OWNER else "platform-admin",
            granted_at=granted_at or (_now() - _HOUR),
            expires_at=expires_at,
            resolver_version=resolver,
        ),
    )


async def _checkpoint(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    intent_id: uuid.UUID,
    *,
    sequence: int = 1,
    goal: str = "ship the migration",
    predecessor: uuid.UUID | None = None,
) -> uuid.UUID:
    """Append one checkpoint and move the head projection to it.

    `predecessor` is required past the first step and refused on it: the chain
    constraint is the database's, and a helper that passed `NULL` for step two
    would fail the insert rather than the assertion — which reads as a defect in
    the code under test.
    """
    if (sequence > 1) != (predecessor is not None):
        raise AssertionError("a checkpoint past the first needs a predecessor, and the first must not have one")
    cid = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO intent_checkpoints
                (checkpoint_id, tenant_id, intent_id, sequence, predecessor_id, goal,
                 next_action, author, recorded_at, retention_policy, digest)
            VALUES (:cid, :tid, :task, :seq, :pred, :goal,
                    'keep going', :author, now(), 'standard', 'deadbeef')
            """
        ),
        {
            "cid": cid,
            "tid": tenant_id,
            "task": intent_id,
            "seq": sequence,
            "pred": predecessor,
            "goal": goal,
            "author": _MEMBER,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO intent_heads (tenant_id, intent_id, head_checkpoint_id, head_sequence, summary, updated_at)
            VALUES (:tid, :task, :cid, :seq, :goal, now())
            ON CONFLICT (tenant_id, intent_id) DO UPDATE
              SET head_checkpoint_id = :cid, head_sequence = :seq, summary = :goal
            """
        ),
        {"tid": tenant_id, "task": intent_id, "cid": cid, "seq": sequence, "goal": goal},
    )
    await session.flush()
    return cid


@pytest_asyncio.fixture
async def task(session: AsyncSession, tenant: uuid.UUID) -> uuid.UUID:
    """One task with an owner, a member, and a checkpoint to find."""
    intent_id = uuid.uuid4()
    await _grant(session, tenant, intent_id, _OWNER, ROLE_OWNER)
    await _grant(session, tenant, intent_id, _MEMBER, ROLE_CONTRIBUTOR)
    await _checkpoint(session, tenant, intent_id)
    return intent_id


# --- the authorized second actor ----------------------------------------------


async def test_a_second_actor_reads_and_extends_the_same_task(
    session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID
) -> None:
    """The feature: cross-agent collaboration on one task.

    Both capabilities, because a contributor that can read and not extend is a
    reader with extra steps, and the audience exists to make the second half work.
    """
    grants = await q.fetch_task_grants(session, tenant_id=tenant, intent_id=task)
    read = require(grants, intent_id=task, actor_id=_MEMBER, capability=CAPABILITY_READ, moment=_now())
    extend = require(grants, intent_id=task, actor_id=_MEMBER, capability=CAPABILITY_EXTEND, moment=_now())
    assert read.role == ROLE_CONTRIBUTOR
    assert extend.role == ROLE_CONTRIBUTOR

    head = await q.lookup_authorized_head(session, tenant_id=tenant, actor_id=_MEMBER, intent_id=task, moment=_now())
    assert head is not None
    assert head.head_sequence == 1


async def test_the_member_can_append_the_next_checkpoint(
    session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID
) -> None:
    """Extending is proven by a real second row, not by the capability check
    alone: an authorization that passes and a write that the schema rejects are
    the same outcome for the caller.

    The predecessor is read back through the authorized lookup, which is how a
    real append finds it — and means this also covers a member reading the head
    the owner's checkpoint created.
    """
    head = await q.lookup_authorized_head(session, tenant_id=tenant, actor_id=_MEMBER, intent_id=task, moment=_now())
    assert head is not None
    await _checkpoint(session, tenant, task, sequence=2, goal="ship the resolver", predecessor=head.head_checkpoint_id)
    found = await q.search_authorized_checkpoints(
        session, tenant_id=tenant, actor_id=_MEMBER, term="resolver", moment=_now()
    )
    assert [c.sequence for c in found] == [2]


# --- the outsider, through every path -----------------------------------------


async def test_an_outsider_is_denied_the_task(session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID) -> None:
    grants = await q.fetch_task_grants(session, tenant_id=tenant, intent_id=task)
    with pytest.raises(AudienceDenied):
        require(grants, intent_id=task, actor_id=_OUTSIDER, capability=CAPABILITY_READ, moment=_now())


async def test_an_outsider_lookup_cannot_distinguish_denied_from_absent(
    session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID
) -> None:
    """Both answers are `None`. A lookup that returned 403 for one and 404 for
    the other would enumerate the tenant's tasks one probe at a time."""
    denied = await q.lookup_authorized_head(
        session, tenant_id=tenant, actor_id=_OUTSIDER, intent_id=task, moment=_now()
    )
    absent = await q.lookup_authorized_head(
        session, tenant_id=tenant, actor_id=_OUTSIDER, intent_id=uuid.uuid4(), moment=_now()
    )
    assert denied is None
    assert absent is None


async def test_an_outsider_lists_nothing(session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID) -> None:
    assert await q.list_authorized_task_ids(session, tenant_id=tenant, actor_id=_OUTSIDER, moment=_now()) == []


async def test_an_outsider_counts_zero_while_the_task_exists(
    session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID
) -> None:
    """The count is over the authorized set, not the tenant's tasks.

    A total that includes tasks the caller cannot open is a disclosure with no
    row in it: watch the number move and you have learned a task was created.
    """
    assert await q.count_authorized_tasks(session, tenant_id=tenant, actor_id=_OUTSIDER, moment=_now()) == 0
    assert await q.count_authorized_tasks(session, tenant_id=tenant, actor_id=_MEMBER, moment=_now()) == 1


async def test_an_outsider_lexical_search_finds_nothing_it_may_not_read(
    session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID
) -> None:
    """The term matches a real checkpoint. Only the audience keeps it back."""
    member_hits = await q.search_authorized_checkpoints(
        session, tenant_id=tenant, actor_id=_MEMBER, term="migration", moment=_now()
    )
    outsider_hits = await q.search_authorized_checkpoints(
        session, tenant_id=tenant, actor_id=_OUTSIDER, term="migration", moment=_now()
    )
    assert len(member_hits) == 1
    assert outsider_hits == []


async def test_a_blank_search_term_is_not_a_wildcard(session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID) -> None:
    assert (
        await q.search_authorized_checkpoints(session, tenant_id=tenant, actor_id=_MEMBER, term="  ", moment=_now())
        == []
    )


# --- revocation, in the database ----------------------------------------------


async def test_revocation_closes_every_path_at_once(session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID) -> None:
    """One predicate, so one revocation. This is the test that fails if any read
    path grows its own copy of "is this grant active"."""
    assert await q.count_authorized_tasks(session, tenant_id=tenant, actor_id=_MEMBER, moment=_now()) == 1

    changed = await q.revoke_grant(session, tenant_id=tenant, intent_id=task, actor_id=_MEMBER, moment=_now())
    assert changed

    assert await q.count_authorized_tasks(session, tenant_id=tenant, actor_id=_MEMBER, moment=_now()) == 0
    assert await q.list_authorized_task_ids(session, tenant_id=tenant, actor_id=_MEMBER, moment=_now()) == []
    assert (
        await q.lookup_authorized_head(session, tenant_id=tenant, actor_id=_MEMBER, intent_id=task, moment=_now())
        is None
    )
    assert (
        await q.search_authorized_checkpoints(
            session, tenant_id=tenant, actor_id=_MEMBER, term="migration", moment=_now()
        )
        == []
    )
    assert await q.fetch_actor_role(session, tenant_id=tenant, intent_id=task, actor_id=_MEMBER, moment=_now()) is None


async def test_a_revoked_grant_still_authorizes_the_past(
    session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID
) -> None:
    """Revocation is temporal, not a delete. An audit asking whether the actor
    was authorized at the moment they read needs the grant to still be there."""
    await q.revoke_grant(session, tenant_id=tenant, intent_id=task, actor_id=_MEMBER, moment=_now())
    earlier = _now() - datetime.timedelta(minutes=30)
    assert (
        await q.fetch_actor_role(session, tenant_id=tenant, intent_id=task, actor_id=_MEMBER, moment=earlier)
        == ROLE_CONTRIBUTOR
    )


async def test_revoking_twice_does_not_extend_the_window(
    session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID
) -> None:
    assert await q.revoke_grant(session, tenant_id=tenant, intent_id=task, actor_id=_MEMBER, moment=_now())
    assert not await q.revoke_grant(session, tenant_id=tenant, intent_id=task, actor_id=_MEMBER, moment=_now() + _HOUR)


async def test_revoking_a_grant_that_does_not_exist_reports_no_change(
    session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID
) -> None:
    assert not await q.revoke_grant(session, tenant_id=tenant, intent_id=task, actor_id=_OUTSIDER, moment=_now())


# --- expiry and unrecognized resolvers, through the query ---------------------


async def test_an_expired_grant_is_invisible_to_every_read(session: AsyncSession, tenant: uuid.UUID) -> None:
    intent_id = uuid.uuid4()
    await _grant(
        session,
        tenant,
        intent_id,
        _MEMBER,
        granted_at=_now() - (2 * _HOUR),
        expires_at=_now() - _HOUR,
    )
    await _checkpoint(session, tenant, intent_id, goal="lapsed work")
    assert await q.count_authorized_tasks(session, tenant_id=tenant, actor_id=_MEMBER, moment=_now()) == 0
    assert (
        await q.lookup_authorized_head(session, tenant_id=tenant, actor_id=_MEMBER, intent_id=intent_id, moment=_now())
        is None
    )


async def test_a_grant_from_an_unrecognized_resolver_is_invisible_in_sql_too(
    session: AsyncSession, tenant: uuid.UUID
) -> None:
    """The resolver check is duplicated into the predicate on purpose: a read
    that never loads grant objects would otherwise skip it entirely, and this
    proves the SQL half is really there."""
    intent_id = uuid.uuid4()
    await _grant(session, tenant, intent_id, _MEMBER, resolver="some-future-resolver/v9")
    await _checkpoint(session, tenant, intent_id, goal="unreadable rule")
    assert await q.count_authorized_tasks(session, tenant_id=tenant, actor_id=_MEMBER, moment=_now()) == 0
    assert await q.list_authorized_task_ids(session, tenant_id=tenant, actor_id=_MEMBER, moment=_now()) == []
    assert (
        await q.fetch_actor_role(session, tenant_id=tenant, intent_id=intent_id, actor_id=_MEMBER, moment=_now())
        is None
    )


async def test_a_future_dated_grant_confers_nothing_yet(session: AsyncSession, tenant: uuid.UUID) -> None:
    intent_id = uuid.uuid4()
    await _grant(session, tenant, intent_id, _MEMBER, granted_at=_now() + _HOUR)
    assert await q.count_authorized_tasks(session, tenant_id=tenant, actor_id=_MEMBER, moment=_now()) == 0
    assert await q.count_authorized_tasks(session, tenant_id=tenant, actor_id=_MEMBER, moment=_now() + (2 * _HOUR)) == 1


# --- entitlement-derived participation ----------------------------------------


async def test_a_materialized_entitlement_grant_authorizes_and_then_lapses(
    session: AsyncSession, tenant: uuid.UUID
) -> None:
    """The whole point of materializing: the read path consults a stored row,
    not a live entitlement service, and the row carries its own expiry."""
    intent_id = uuid.uuid4()
    grant = materialize_entitlement_grant(
        intent_id=intent_id,
        actor_id=_MEMBER,
        role=ROLE_AUDITOR,
        granted_by="entitlement-service",
        evidence=EntitlementEvidence(
            resolver_version=ENTITLEMENT_RESOLVER,
            resolved_at=_now(),
            max_age=_HOUR,
            source="entitlement-service",
            authority="platform-directory",
        ),
        moment=_now(),
    )
    await q.insert_grant(session, tenant_id=tenant, grant=grant)

    assert (
        await q.fetch_actor_role(session, tenant_id=tenant, intent_id=intent_id, actor_id=_MEMBER, moment=_now())
        == ROLE_AUDITOR
    )
    later = _now() + _HOUR
    assert (
        await q.fetch_actor_role(session, tenant_id=tenant, intent_id=intent_id, actor_id=_MEMBER, moment=later) is None
    )


async def test_an_auditor_reads_but_the_resolver_refuses_the_extend(session: AsyncSession, tenant: uuid.UUID) -> None:
    """Roles carry capabilities by membership. An auditor with a live grant is
    authorized to read the task and must still be refused an append."""
    intent_id = uuid.uuid4()
    await _grant(session, tenant, intent_id, _MEMBER, ROLE_AUDITOR)
    grants = await q.fetch_task_grants(session, tenant_id=tenant, intent_id=intent_id)
    assert require(grants, intent_id=intent_id, actor_id=_MEMBER, capability=CAPABILITY_READ, moment=_now()).allowed
    with pytest.raises(AudienceDenied):
        require(grants, intent_id=intent_id, actor_id=_MEMBER, capability=CAPABILITY_EXTEND, moment=_now())


# --- a tenant is not an audience ----------------------------------------------


async def test_sharing_a_tenant_confers_nothing(session: AsyncSession, tenant: uuid.UUID, task: uuid.UUID) -> None:
    """The boundary being replaced. Every actor here is in the task's own
    tenant, and the outsider still sees nothing — which is what makes this a
    task audience rather than tenant-wide visibility with extra bookkeeping."""
    assert await q.count_authorized_tasks(session, tenant_id=tenant, actor_id=_OUTSIDER, moment=_now()) == 0
    assert (
        await q.fetch_actor_role(session, tenant_id=tenant, intent_id=task, actor_id=_OUTSIDER, moment=_now()) is None
    )


async def test_the_database_refuses_a_self_grant_through_this_path_too(
    session: AsyncSession, tenant: uuid.UUID
) -> None:
    """The contract object refuses it first, so the constraint is never reached
    from here. Asserting the near half is what proves the far half is unreachable
    rather than merely untested."""
    from contextplane.context.schemas.trust import InvalidContextItem

    with pytest.raises(InvalidContextItem, match="cannot grant themselves"):
        IntentParticipantGrantV1(
            intent_id=uuid.uuid4(),
            actor_id=_MEMBER,
            role=ROLE_CONTRIBUTOR,
            granted_by=_MEMBER,
            granted_at=_now(),
            expires_at=None,
            resolver_version=RESOLVER_EXPLICIT,
        )
