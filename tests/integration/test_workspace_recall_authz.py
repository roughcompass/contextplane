"""Workspace recall reaches only into tasks the caller participates in.

The unit suite covers labelling and bounds. What needs a real database is the
claim that the audience predicate is *in the query*: an outsider must get
nothing back, and must also not be able to infer that anything was held back —
no exclusion, no short page, no count that moved.

Every actor here shares one tenant. Two tenants would be the easy case, and it
would pass even if participation were ignored entirely; the boundary under test
is between two actors inside the same tenant.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.workspaces import queries_audience as audience_q
from contextplane.workspaces.audience import RESOLVER_EXPLICIT
from contextplane.workspaces.recall import WorkspaceRecall
from contextplane.workspaces.schemas.task_memory import ROLE_CONTRIBUTOR, TaskParticipantGrantV1

pytestmark = pytest.mark.asyncio

_MEMBER = "agent-member"
_OUTSIDER = "agent-outsider"
_HOUR = datetime.timedelta(hours=1)
_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)

_REFERENCE = {
    "source_system": "github",
    "source_namespace": "acme/platform",
    "kind": "commit",
    "external_id": "abc123",
}


@pytest_asyncio.fixture
async def engine_and_factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def factory(engine_and_factory: async_sessionmaker[AsyncSession]) -> async_sessionmaker[AsyncSession]:
    return engine_and_factory


@pytest_asyncio.fixture
async def tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
            {"t": tid, "s": f"rec-{tid.hex[:8]}", "n": "workspace recall test"},
        )
    return tid


async def _task_with_checkpoint(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    participants: tuple[str, ...],
    goal: str,
    classification: str | None = None,
    bind_reference: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    """One task, its grants, one checkpoint, and optionally a reference binding."""
    task_id = uuid.uuid4()
    checkpoint_id = uuid.uuid4()
    evidence = "[]"
    if classification is not None:
        evidence = (
            '[{"source_system": "github", "source_namespace": "acme/platform", '
            f'"kind": "commit", "external_id": "abc123", "classification": "{classification}"}}]'
        )
    async with factory() as session, session.begin():
        for actor in participants:
            await audience_q.insert_grant(
                session,
                tenant_id=tenant_id,
                grant=TaskParticipantGrantV1(
                    task_id=task_id,
                    actor_id=actor,
                    role=ROLE_CONTRIBUTOR,
                    granted_by="agent-owner",
                    granted_at=_NOW - _HOUR,
                    expires_at=None,
                    resolver_version=RESOLVER_EXPLICIT,
                ),
            )
        await session.execute(
            text(
                """
                INSERT INTO task_checkpoints
                    (checkpoint_id, tenant_id, task_id, sequence, predecessor_id, goal, evidence,
                     next_action, author, recorded_at, retention_policy, digest)
                VALUES (:cid, :tid, :task, 1, NULL, :goal, CAST(:ev AS jsonb),
                        'keep going', :author, :rec, 'standard', :digest)
                """
            ),
            {
                "cid": checkpoint_id,
                "tid": tenant_id,
                "task": task_id,
                "goal": goal,
                "ev": evidence,
                "author": _MEMBER,
                "rec": _NOW,
                "digest": checkpoint_id.hex[:16],
            },
        )
        if bind_reference:
            reference_id = uuid.uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO context_external_references
                        (reference_id, tenant_id, source_system, source_namespace, kind, external_id,
                         classification, external_authority, collision_key, created_at)
                    VALUES (:rid, :tid, :sys, :ns, :kind, :ext, 'internal', 'github', :ck, :now)
                    """
                ),
                {
                    "rid": reference_id,
                    "tid": tenant_id,
                    "sys": _REFERENCE["source_system"],
                    "ns": _REFERENCE["source_namespace"],
                    "kind": _REFERENCE["kind"],
                    "ext": _REFERENCE["external_id"],
                    "ck": f"{tenant_id}:{uuid.uuid4()}",
                    "now": _NOW,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO context_reference_bindings
                        (binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at)
                    VALUES (:bid, :tid, :rid, 'task_checkpoint', :cid, :now)
                    """
                ),
                {
                    "bid": uuid.uuid4(),
                    "tid": tenant_id,
                    "rid": reference_id,
                    "cid": checkpoint_id,
                    "now": _NOW,
                },
            )
    return task_id, checkpoint_id


# --- lexical recall ------------------------------------------------------------


async def test_a_participant_recalls_their_own_checkpoint(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    _, checkpoint_id = await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="ship the migration")
    recall = WorkspaceRecall(session_factory=factory)
    outcome = await recall.lexical_arm(tenant_id=tenant, actor_id=_MEMBER, term="migration", moment=_NOW)()
    assert [i.receipt_item_id.item_key for i in outcome.items] == [str(checkpoint_id)]


async def test_an_outsider_recalls_nothing_and_learns_nothing(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """The heart of it. The term matches a real checkpoint in the outsider's own
    tenant, and the answer is indistinguishable from there being no such content:
    no items, and critically no exclusion — an exclusion here would report that
    a task exists but is not theirs, which is the discovery the boundary is for.
    """
    await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="ship the migration")
    recall = WorkspaceRecall(session_factory=factory)
    outcome = await recall.lexical_arm(tenant_id=tenant, actor_id=_OUTSIDER, term="migration", moment=_NOW)()
    assert outcome.items == ()
    assert outcome.exclusions == ()
    assert outcome.truncated is False


async def test_recall_spans_only_the_callers_own_tasks(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Two tasks, one term, one participant in each. Each caller sees exactly
    their own — so the predicate is per-task, not per-tenant."""
    _, mine = await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="shared word here")
    _, theirs = await _task_with_checkpoint(factory, tenant, participants=(_OUTSIDER,), goal="shared word here")
    recall = WorkspaceRecall(session_factory=factory)
    for actor, expected in ((_MEMBER, mine), (_OUTSIDER, theirs)):
        outcome = await recall.lexical_arm(tenant_id=tenant, actor_id=actor, term="shared word", moment=_NOW)()
        assert [i.receipt_item_id.item_key for i in outcome.items] == [str(expected)]


async def test_a_revoked_participant_stops_recalling(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Revocation reaches recall through the same predicate as every other read,
    which is what stops this arm from being the one path that keeps answering."""
    task_id, _ = await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="ship the migration")
    recall = WorkspaceRecall(session_factory=factory)
    assert (await recall.lexical_arm(tenant_id=tenant, actor_id=_MEMBER, term="migration", moment=_NOW)()).items

    async with factory() as session, session.begin():
        await audience_q.revoke_grant(session, tenant_id=tenant, task_id=task_id, actor_id=_MEMBER, moment=_NOW)

    after = await recall.lexical_arm(tenant_id=tenant, actor_id=_MEMBER, term="migration", moment=_NOW)()
    assert after.items == ()
    assert after.exclusions == ()


async def test_a_blank_term_recalls_nothing_rather_than_everything(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="ship the migration")
    recall = WorkspaceRecall(session_factory=factory)
    outcome = await recall.lexical_arm(tenant_id=tenant, actor_id=_MEMBER, term="   ", moment=_NOW)()
    assert outcome.items == ()


async def test_the_bound_truncates_and_says_so(factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID) -> None:
    """Truncation is a fact the assembler needs: a page silently cut short reads
    as "that was all there was"."""
    for _ in range(4):
        await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="bounded work")
    recall = WorkspaceRecall(session_factory=factory)
    outcome = await recall.lexical_arm(tenant_id=tenant, actor_id=_MEMBER, term="bounded", moment=_NOW, limit=2)()
    assert len(outcome.items) == 2
    assert outcome.truncated is True


# --- reference recall ----------------------------------------------------------


async def test_a_participant_recalls_by_the_reference_a_checkpoint_cited(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    _, checkpoint_id = await _task_with_checkpoint(
        factory, tenant, participants=(_MEMBER,), goal="fix the drain", bind_reference=True
    )
    recall = WorkspaceRecall(session_factory=factory)
    outcome = await recall.reference_arm(tenant_id=tenant, actor_id=_MEMBER, moment=_NOW, **_REFERENCE)()
    assert [i.receipt_item_id.item_key for i in outcome.items] == [str(checkpoint_id)]


async def test_a_binding_does_not_authorize_an_outsider(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """The case reference recall could plausibly get wrong: the binding is found
    by a query the outsider is entitled to run, and it points at a checkpoint
    they are not. Finding the reference is not the same as being in the task."""
    await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="fix the drain", bind_reference=True)
    recall = WorkspaceRecall(session_factory=factory)
    outcome = await recall.reference_arm(tenant_id=tenant, actor_id=_OUTSIDER, moment=_NOW, **_REFERENCE)()
    assert outcome.items == ()
    assert outcome.exclusions == ()


async def test_an_unknown_reference_recalls_nothing(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="fix the drain", bind_reference=True)
    recall = WorkspaceRecall(session_factory=factory)
    outcome = await recall.reference_arm(
        tenant_id=tenant,
        actor_id=_MEMBER,
        moment=_NOW,
        source_system="github",
        source_namespace="acme/platform",
        kind="commit",
        external_id="never-committed",
    )()
    assert outcome.items == ()


async def test_a_checkpoint_with_no_binding_is_not_recalled_by_reference(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Reference recall is about what a checkpoint cited, not what a search would
    match. A task with the same words and no binding must not appear."""
    await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="fix the drain")
    recall = WorkspaceRecall(session_factory=factory)
    outcome = await recall.reference_arm(tenant_id=tenant, actor_id=_MEMBER, moment=_NOW, **_REFERENCE)()
    assert outcome.items == ()


# --- classification, end to end ------------------------------------------------


async def test_restricted_evidence_withholds_the_item_from_a_participant(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Inside the caller's own task, so an exclusion is the right answer — and
    the contrast with the outsider cases above is the whole point: same empty
    item list, entirely different report."""
    _, checkpoint_id = await _task_with_checkpoint(
        factory, tenant, participants=(_MEMBER,), goal="handle the secret", classification="restricted"
    )
    recall = WorkspaceRecall(session_factory=factory)
    outcome = await recall.lexical_arm(tenant_id=tenant, actor_id=_MEMBER, term="secret", moment=_NOW)()
    assert outcome.items == ()
    assert [e.item_key for e in outcome.exclusions] == [str(checkpoint_id)]


async def test_evidence_classification_reaches_the_recalled_item(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    await _task_with_checkpoint(
        factory, tenant, participants=(_MEMBER,), goal="handle the finances", classification="confidential"
    )
    recall = WorkspaceRecall(session_factory=factory)
    outcome = await recall.lexical_arm(tenant_id=tenant, actor_id=_MEMBER, term="finances", moment=_NOW)()
    assert len(outcome.items) == 1
    trust = outcome.items[0].trust
    assert trust is not None
    assert trust.classification == "confidential"
