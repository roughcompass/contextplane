"""One specialist hands work to another, and the evidence survives the crossing.

The unit suite proves the binding refuses when something moves. This proves the
thing that only a database can: that the audience predicate the refusal depends
on is the *real* one, resolved in SQL against real grants, and that it answers
the same way through the handoff as it does through the direct read.

That distinction matters because the handoff's authorization is not its own. It
is whatever `get_checkpoint` resolves, and a fake reader satisfies the protocol
whether or not the real query filters on anything at all. So the outsider here is
a real actor with no grant, denied by a real predicate.

The two specialists are separate actor identities on one task -- the coding actor
that wrote the checkpoint, and the security actor that resumes it. Nothing is
copied between them: the second reads the same rows the first did, through the
handle's pointers.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.context.handoff import ContextHandoffService, HandoffRefused
from contextplane.context.receipts import ContextReceiptService
from contextplane.types import TenantContext
from contextplane.workspaces.audience import RECOGNIZED_RESOLVERS
from contextplane.workspaces.checkpoints import IntentCheckpointService

_NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)
_GOAL = "harden the token exchange before the review gate"


class _Clock:
    def now(self) -> datetime.datetime:
        return _NOW


def _ctx(tenant: uuid.UUID, actor: str) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=actor, roles=["member"])


async def _grant(session: AsyncSession, *, tenant: uuid.UUID, task: uuid.UUID, actor: str) -> None:
    await session.execute(
        text(
            "INSERT INTO intent_participant_grants "
            "(tenant_id, intent_id, actor_id, role, granted_by, granted_at, resolver_version) "
            "VALUES (:t, :task, :actor, 'contributor', 'granter', :now, :resolver)"
        ),
        {
            "t": tenant,
            "task": task,
            "actor": actor,
            "now": _NOW - datetime.timedelta(hours=1),
            "resolver": sorted(RECOGNIZED_RESOLVERS)[0],
        },
    )


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    """One task, two granted specialists, one outsider, one checkpoint, one receipt."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant, task, receipt = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Fresh per world, and real UUIDs: the audit row an append writes types
    # `actor_id` as a UUID, and `actors` is keyed on it, so a readable label
    # would fail at the write and a shared constant would collide between tests.
    coding, security, outsider = (str(uuid.uuid4()) for _ in range(3))
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'handoff')"),
                {"t": tenant, "s": f"hnd-{tenant.hex[:10]}"},
            )
            for actor in (coding, security, outsider):
                await session.execute(
                    text(
                        "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                        "VALUES (:a, :t, 'specialist', :sub, :now)"
                    ),
                    {"a": actor, "t": tenant, "sub": f"sub-{actor[:12]}", "now": _NOW},
                )
            # The outsider exists and is *not* granted. An actor that did not
            # exist at all would be denied by a foreign key rather than by the
            # audience predicate, which is not the denial being asserted.
            for actor in (coding, security):
                await _grant(session, tenant=tenant, task=task, actor=actor)
            await session.execute(
                text(
                    "INSERT INTO context_receipts (receipt_id, tenant_id, intent_id, state, cacheable, "
                    "resolved_at, requested_by) VALUES (:r, :t, :task, 'complete', false, :now, :by)"
                ),
                {"r": receipt, "t": tenant, "task": task, "now": _NOW, "by": coding},
            )
            for block, state in (("workspace", "success"), ("canonical", "success")):
                await session.execute(
                    text(
                        "INSERT INTO context_receipt_arms (arm_id, receipt_id, block, state) " "VALUES (:a, :r, :b, :s)"
                    ),
                    {"a": uuid.uuid4(), "r": receipt, "b": block, "s": state},
                )
            await session.execute(
                text(
                    "INSERT INTO context_receipt_exclusions (exclusion_id, receipt_id, block, item_key, reason) "
                    "VALUES (:e, :r, 'workspace', 'cp-withheld', 'classification restricted')"
                ),
                {"e": uuid.uuid4(), "r": receipt},
            )

        checkpoints = IntentCheckpointService(session_factory=factory, clock=_Clock())
        appended = await checkpoints.append_checkpoint(
            _ctx(tenant, coding),
            intent_id=task,
            payload={
                "goal": _GOAL,
                "decisions": ["exchange stays server-side"],
                "assumptions": [],
                "completed_checks": [],
                "open_questions": ["does the review gate need the refresh path"],
                "next_action": "hand to security review",
            },
            idempotency_key=f"seed-{task.hex[:8]}",
        )

        yield {
            "factory": factory,
            "tenant": tenant,
            "task": task,
            "receipt": receipt,
            "coding": coding,
            "security": security,
            "outsider": outsider,
            "checkpoint_id": appended.checkpoint.checkpoint_id,
            "checkpoints": checkpoints,
            "handoff": ContextHandoffService(
                checkpoints=checkpoints,
                receipts=ContextReceiptService(session_factory=factory, clock=_Clock()),
            ),
        }
    finally:
        await engine.dispose()


async def _issue(world: dict[str, Any], actor: str | None = None) -> Any:
    return await world["handoff"].issue(
        _ctx(world["tenant"], actor or world["coding"]),
        checkpoint_id=world["checkpoint_id"],
        receipt_id=world["receipt"],
    )


@pytest.mark.asyncio
async def test_a_second_specialist_resumes_the_first_specialists_exact_evidence(world: dict[str, Any]) -> None:
    """The property the handoff exists for, across two actor identities.

    The security actor never receives the checkpoint's text. It receives
    pointers, reads the rows itself under its own grant, and the digests agree --
    which is what makes this evidence rather than a report about evidence.
    """
    handle = await _issue(world)
    consumed = await world["handoff"].consume(_ctx(world["tenant"], world["security"]), handle)

    assert consumed == handle
    assert consumed.issued_by == world["coding"]
    # What it withheld crosses too. A consumer that cannot see this reads the
    # evidence as complete.
    assert consumed.exclusions == ("workspace/cp-withheld",)
    assert consumed.source_blocks == ("canonical", "workspace")


@pytest.mark.asyncio
async def test_the_handed_over_revision_is_the_one_the_consumer_reads(world: dict[str, Any]) -> None:
    """The digest names a revision, and the revision is fetchable by it.

    This is the control that stops the test above passing on two digests that
    merely match each other: the digest resolves, through the product's own
    by-digest lookup, to the checkpoint whose id the handle carries.
    """
    handle = await _issue(world)
    await world["handoff"].consume(_ctx(world["tenant"], world["security"]), handle)

    found = await world["checkpoints"].get_checkpoint_by_digest(
        _ctx(world["tenant"], world["security"]), digest=handle.checkpoint_digest
    )
    assert found.checkpoint_id == handle.checkpoint_id
    assert found.goal == _GOAL


@pytest.mark.asyncio
async def test_an_outsider_is_denied_the_handoff_and_the_direct_read_alike(world: dict[str, Any]) -> None:
    """One answer through two doors, which is the point of not adding a third.

    The handle is valid and matches the rows exactly; the outsider simply has no
    grant. Denied through the handoff, and denied through the direct read the
    handoff delegates to -- so the handoff has not become a way around the
    audience predicate.
    """
    handle = await _issue(world)
    outsider = _ctx(world["tenant"], world["outsider"])

    with pytest.raises(HandoffRefused):
        await world["handoff"].consume(outsider, handle)

    from contextplane.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await world["checkpoints"].get_checkpoint(outsider, checkpoint_id=world["checkpoint_id"])


@pytest.mark.asyncio
async def test_a_revoked_specialist_stops_being_able_to_consume(world: dict[str, Any]) -> None:
    """A handle is not a capability that outlives the grant it was minted under."""
    handle = await _issue(world)

    async with world["factory"]() as session, session.begin():
        await session.execute(
            text(
                "UPDATE intent_participant_grants SET expires_at = :then "
                "WHERE tenant_id = :t AND intent_id = :task AND actor_id = :actor"
            ),
            {
                "then": _NOW - datetime.timedelta(minutes=1),
                "t": world["tenant"],
                "task": world["task"],
                "actor": world["security"],
            },
        )

    with pytest.raises(HandoffRefused):
        await world["handoff"].consume(_ctx(world["tenant"], world["security"]), handle)


@pytest.mark.asyncio
async def test_appending_the_next_checkpoint_moves_the_revision_and_the_old_handle_still_names_the_old_one(
    world: dict[str, Any],
) -> None:
    """Append continues the chain; it does not rewrite what was handed over.

    The security actor appends its own checkpoint under its own identity, and the
    handle it consumed keeps naming the revision it was issued against. A handle
    that silently followed the head would let a consumer believe it had validated
    work that arrived after it looked.
    """
    handle = await _issue(world)
    await world["handoff"].consume(_ctx(world["tenant"], world["security"]), handle)

    appended = await world["checkpoints"].append_checkpoint(
        _ctx(world["tenant"], world["security"]),
        intent_id=world["task"],
        payload={
            "goal": "reviewed the token exchange",
            "decisions": [],
            "assumptions": [],
            "completed_checks": ["no refresh path reachable without the gate"],
            "open_questions": [],
        },
        idempotency_key=f"review-{world["task"].hex[:8]}",
    )

    assert appended.checkpoint.predecessor_id == handle.checkpoint_id
    assert appended.checkpoint.author == world["security"]
    # Still consumable, and still pointing at the revision it was issued for.
    again = await world["handoff"].consume(_ctx(world["tenant"], world["security"]), handle)
    assert again.checkpoint_id == handle.checkpoint_id
    assert again.checkpoint_digest == handle.checkpoint_digest
