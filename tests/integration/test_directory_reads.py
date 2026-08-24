"""The three listings E23-T1 adds, against a real database.

Each one narrows itself from something the caller already holds, and each
narrowing is the whole authorization argument:

- **intents** from the caller's participation grants;
- **tenants** from the caller's credential, never from the tenants table;
- **receipts** to what the detail read would serve the same caller.

A fake would agree with whatever the code did about all three, which is why they
are here.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.context.receipts import ContextReceiptService
from contextplane.exceptions import ValidationError
from contextplane.service.governance.tenants import TenantDirectoryService
from contextplane.types import TenantContext, TenantMembership
from contextplane.workspaces.directory import IntentDirectoryService
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, other_tenant_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        for tid, name in ((tenant_id, "Northstar"), (other_tenant_id, "Field Labs")):
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :slug, :name)"),
                {"name": name, "slug": f"t-{tid.hex[:8]}", "t": tid},
            )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind, "
                "                    declared_at, declared_by, created_at) "
                "VALUES (:a, :t, :sub, 'Reader', 'human', :now, :a, :now)"
            ),
            {"a": actor_id, "now": _NOW, "sub": f"s-{actor_id.hex[:8]}", "t": tenant_id},
        )
    try:
        yield {
            "actor_id": actor_id,
            "ctx": TenantContext(actor_id=actor_id, roles=["producer"], tenant_id=tenant_id),
            "factory": factory,
            "other_tenant_id": other_tenant_id,
            "tenant_id": tenant_id,
        }
    finally:
        await engine.dispose()


async def _grant(
    world: dict[str, Any],
    task_id: uuid.UUID,
    *,
    actor: str | None = None,
    expires: Any = None,
    granted_at: datetime.datetime | None = None,
) -> None:
    """One participation grant.

    `granted_at` is settable because the schema refuses an expiry that predates
    the grant — an expired grant has to have been granted before it lapsed, which
    is the constraint being right rather than in the way.
    """
    async with world["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO intent_participant_grants "
                "(tenant_id, intent_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version) "
                "VALUES (:t, :intent, :actor, 'contributor', 'granter', :now, :expires, 'v1')"
            ),
            {
                "actor": actor or str(world["actor_id"]),
                "expires": expires,
                "now": granted_at or _NOW,
                "t": world["tenant_id"],
                "intent": task_id,
            },
        )


async def _checkpoint(world: dict[str, Any], task_id: uuid.UUID, *, goal: str, minutes: int = 1) -> None:
    """The first checkpoint on an intent.

    Always sequence 1: the schema allows a missing predecessor only there, which
    is the chain being right rather than in the way. What the ordering keys on is
    `recorded_at`, so that is what varies.
    """
    async with world["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO intent_checkpoints "
                "(checkpoint_id, tenant_id, intent_id, sequence, predecessor_id, goal, author, "
                " recorded_at, retention_policy, digest) "
                "VALUES (:cid, :t, :intent, :seq, NULL, :goal, 'author', :now, 'standard', :digest)"
            ),
            {
                "cid": uuid.uuid4(),
                "digest": f"sha256:{uuid.uuid4().hex}",
                "goal": goal,
                "now": _NOW + datetime.timedelta(minutes=minutes),
                "seq": 1,
                "t": world["tenant_id"],
                "intent": task_id,
            },
        )


# --- intents ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_task_is_listed_by_its_latest_goal_rather_than_its_uuid(world: dict[str, Any]) -> None:
    """A goal is restated as a task moves, so the oldest statement is the one
    least likely to describe what it is now."""
    task = uuid.uuid4()
    await _grant(world, task)
    await _checkpoint(world, task, goal="ship the migration")

    service = IntentDirectoryService(clock=FakeClock(_NOW), session_factory=world["factory"])
    (found,) = await service.list_intents(world["ctx"])

    assert found.intent_id == task
    assert found.goal == "ship the migration"
    assert found.checkpoint_count == 1
    assert found.role == "contributor"


@pytest.mark.asyncio
async def test_a_task_with_no_checkpoint_is_listed_with_no_goal(world: dict[str, Any]) -> None:
    """A grant is written before the first checkpoint, so this is a real state.
    Dropping it would hide a task the caller may work on; rendering the UUID
    would show the value the directory exists to stop showing."""
    task = uuid.uuid4()
    await _grant(world, task)

    service = IntentDirectoryService(clock=FakeClock(_NOW), session_factory=world["factory"])
    (found,) = await service.list_intents(world["ctx"])

    assert found.intent_id == task
    assert found.goal is None
    assert found.checkpoint_count == 0


@pytest.mark.asyncio
async def test_a_task_this_caller_is_not_on_is_not_listed(world: dict[str, Any]) -> None:
    """Participation is already the rule for reading a task's material, so a
    directory that offered one the caller cannot open would be offering a
    refusal."""
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _grant(world, mine)
    await _grant(world, theirs, actor=str(uuid.uuid4()))

    service = IntentDirectoryService(clock=FakeClock(_NOW), session_factory=world["factory"])
    found = await service.list_intents(world["ctx"])

    assert [entry.intent_id for entry in found] == [mine]


@pytest.mark.asyncio
async def test_an_expired_grant_is_not_offered(world: dict[str, Any]) -> None:
    """`list_grants` includes expired grants because it audits who was on a task.
    This picks what the caller may work on now, and the two are different
    questions."""
    live, lapsed = uuid.uuid4(), uuid.uuid4()
    await _grant(world, live)
    await _grant(
        world,
        lapsed,
        expires=_NOW - datetime.timedelta(days=1),
        granted_at=_NOW - datetime.timedelta(days=7),
    )

    service = IntentDirectoryService(clock=FakeClock(_NOW), session_factory=world["factory"])
    found = await service.list_intents(world["ctx"])

    assert [entry.intent_id for entry in found] == [live]


@pytest.mark.asyncio
async def test_the_most_recently_touched_task_comes_first(world: dict[str, Any]) -> None:
    older, newer = uuid.uuid4(), uuid.uuid4()
    await _grant(world, older)
    await _grant(world, newer)
    await _checkpoint(world, older, goal="older", minutes=1)
    await _checkpoint(world, newer, goal="newer", minutes=5)

    service = IntentDirectoryService(clock=FakeClock(_NOW), session_factory=world["factory"])
    found = await service.list_intents(world["ctx"])

    assert [entry.goal for entry in found] == ["newer", "older"]


@pytest.mark.asyncio
async def test_an_oversized_page_is_refused(world: dict[str, Any]) -> None:
    service = IntentDirectoryService(clock=FakeClock(_NOW), session_factory=world["factory"])

    with pytest.raises(ValidationError, match="page_size"):
        await service.list_intents(world["ctx"], page_size=1000)


# --- tenants ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tenant_list_comes_from_the_credential_and_not_the_table(
    world: dict[str, Any],
) -> None:
    """The only cross-tenant read in the product, and the reason it is safe.

    Both tenants exist in the table. The caller's credential names one. A version
    that selected from `tenants` and filtered afterwards would be one refactor
    from returning both — and the failure would be a disclosure rather than an
    error.
    """
    ctx = TenantContext(
        actor_id=world["actor_id"],
        roles=["producer"],
        tenant_id=world["tenant_id"],
        tenant_memberships=[
            TenantMembership(roles=frozenset({"producer"}), tenant_id=world["tenant_id"], tenant_slug="northstar")
        ],
    )

    found = await TenantDirectoryService(world["factory"]).reachable(ctx)

    assert [entry.tenant_id for entry in found] == [world["tenant_id"]]
    assert found[0].display_name == "Northstar"
    assert found[0].is_current is True


@pytest.mark.asyncio
async def test_a_membership_this_deployment_has_never_seen_is_reported_not_dropped(
    world: dict[str, Any],
) -> None:
    """A credential can name a tenant whose row has not been materialised.
    Dropping it would report no access to a tenant the caller does have."""
    unseen = uuid.uuid4()
    ctx = TenantContext(
        actor_id=world["actor_id"],
        roles=["producer"],
        tenant_id=world["tenant_id"],
        tenant_memberships=[
            TenantMembership(roles=frozenset({"producer"}), tenant_id=world["tenant_id"], tenant_slug="a-northstar"),
            TenantMembership(roles=frozenset({"auditor"}), tenant_id=unseen, tenant_slug="z-unseen"),
        ],
    )

    found = await TenantDirectoryService(world["factory"]).reachable(ctx)

    by_id = {entry.tenant_id: entry for entry in found}
    assert by_id[unseen].is_provisioned is False
    assert by_id[unseen].display_name is None
    assert by_id[unseen].tenant_slug == "z-unseen"
    assert by_id[world["tenant_id"]].is_provisioned is True


@pytest.mark.asyncio
async def test_the_current_tenant_is_first(world: dict[str, Any]) -> None:
    """A picker's first row should be where the reader already is, and a list
    ordered by whatever the entitlement service returned would reshuffle between
    requests."""
    ctx = TenantContext(
        actor_id=world["actor_id"],
        roles=["producer"],
        tenant_id=world["other_tenant_id"],
        tenant_memberships=[
            TenantMembership(roles=frozenset(), tenant_id=world["tenant_id"], tenant_slug="a-first-alphabetically"),
            TenantMembership(roles=frozenset(), tenant_id=world["other_tenant_id"], tenant_slug="z-current"),
        ],
    )

    found = await TenantDirectoryService(world["factory"]).reachable(ctx)

    assert found[0].tenant_id == world["other_tenant_id"]
    assert found[0].is_current is True


@pytest.mark.asyncio
async def test_a_context_with_no_memberships_reports_the_one_it_is_acting_as(
    world: dict[str, Any],
) -> None:
    """A single-tenant deployment populates no memberships. Reporting nothing
    would make a picker over this render zero rows on a deployment that has
    exactly one answer."""
    found = await TenantDirectoryService(world["factory"]).reachable(world["ctx"])

    assert [entry.tenant_id for entry in found] == [world["tenant_id"]]
    assert found[0].display_name == "Northstar"


# --- receipts -----------------------------------------------------------------


async def _receipt(
    world: dict[str, Any],
    *,
    minutes: int,
    hydration: str = "complete",
    withheld: bool = False,
) -> uuid.UUID:
    receipt_id = uuid.uuid4()
    quarantine_id: uuid.UUID | None = None
    if withheld:
        # A receipt is withheld *by a quarantine*, not by an actor — the foreign
        # key says so, and it is a better fact than "somebody hid this": an
        # operator asking why can open the incident rather than the person.
        quarantine_id = uuid.uuid4()
        async with world["factory"]() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO claim_quarantines "
                    "(quarantine_id, tenant_id, predicate, matched_count, reason, applied_by, applied_at) "
                    "VALUES (:qid, :t, CAST('{}' AS JSONB), 1, 'incident', :actor, :now)"
                ),
                {"actor": world["actor_id"], "now": _NOW, "qid": quarantine_id, "t": world["tenant_id"]},
            )
    async with world["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO context_receipts "
                "(receipt_id, tenant_id, state, cacheable, hydration_state, withheld_at, withheld_by, "
                " item_count, exclusion_count, resolved_at, requested_by) "
                "VALUES (:rid, :t, 'complete', TRUE, :hydration, :withheld, :withheld_by, 1, 0, :at, :actor)"
            ),
            {
                "actor": str(world["actor_id"]),
                "at": _NOW + datetime.timedelta(minutes=minutes),
                "hydration": hydration,
                "rid": receipt_id,
                "t": world["tenant_id"],
                # Both or neither: the schema refuses a withholding nobody
                # is accountable for, which is the same rule the instruction
                # channel's contradiction note follows.
                "withheld": _NOW if withheld else None,
                "withheld_by": quarantine_id,
            },
        )
    return receipt_id


@pytest.mark.asyncio
async def test_recent_receipts_come_back_newest_first(world: dict[str, Any]) -> None:
    older = await _receipt(world, minutes=1)
    newer = await _receipt(world, minutes=5)

    service = ContextReceiptService(clock=FakeClock(_NOW), session_factory=world["factory"])
    found = await service.recent(world["ctx"])

    assert [entry.receipt_id for entry in found] == [newer, older]


@pytest.mark.asyncio
async def test_a_withheld_receipt_is_absent_rather_than_empty(world: dict[str, Any]) -> None:
    """A row with nothing in it still discloses that the resolution happened,
    which is most of what withholding protects."""
    servable = await _receipt(world, minutes=1)
    await _receipt(world, minutes=2, withheld=True)

    service = ContextReceiptService(clock=FakeClock(_NOW), session_factory=world["factory"])
    found = await service.recent(world["ctx"])

    assert [entry.receipt_id for entry in found] == [servable]


@pytest.mark.asyncio
async def test_an_unhydrated_receipt_is_absent(world: dict[str, Any]) -> None:
    """Its exclusions are not recorded yet, so an `exclusion_count` of zero on it
    would mean "nothing was withheld" when it means "nothing has been written
    down"."""
    servable = await _receipt(world, minutes=1)
    await _receipt(world, minutes=2, hydration="pending")

    service = ContextReceiptService(clock=FakeClock(_NOW), session_factory=world["factory"])
    found = await service.recent(world["ctx"])

    assert [entry.receipt_id for entry in found] == [servable]


@pytest.mark.asyncio
async def test_another_tenants_receipt_is_not_listed(world: dict[str, Any]) -> None:
    mine = await _receipt(world, minutes=1)
    async with world["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO context_receipts "
                "(receipt_id, tenant_id, state, cacheable, hydration_state, withheld_at, "
                " item_count, exclusion_count, resolved_at, requested_by) "
                "VALUES (:rid, :t, 'complete', TRUE, 'complete', NULL, 1, 0, :at, 'x')"
            ),
            {"at": _NOW, "rid": uuid.uuid4(), "t": world["other_tenant_id"]},
        )

    service = ContextReceiptService(clock=FakeClock(_NOW), session_factory=world["factory"])
    found = await service.recent(world["ctx"])

    assert [entry.receipt_id for entry in found] == [mine]


@pytest.mark.asyncio
async def test_paging_by_timestamp_does_not_skip_a_receipt_written_between_pages(
    world: dict[str, Any],
) -> None:
    """The reason this is keyset rather than offset. A receipt written between
    two pages shifts an offset window and hides a row — in a busy tenant, the
    rows most worth seeing."""
    newest = await _receipt(world, minutes=10)
    middle = await _receipt(world, minutes=5)
    oldest = await _receipt(world, minutes=1)

    service = ContextReceiptService(clock=FakeClock(_NOW), session_factory=world["factory"])
    first_page = await service.recent(world["ctx"], limit=1)
    assert [entry.receipt_id for entry in first_page] == [newest]

    # A resolution lands after the first page was read.
    await _receipt(world, minutes=20)

    second_page = await service.recent(world["ctx"], limit=2, before=first_page[-1].resolved_at)
    assert [entry.receipt_id for entry in second_page] == [middle, oldest]


@pytest.mark.asyncio
async def test_an_oversized_limit_is_refused(world: dict[str, Any]) -> None:
    service = ContextReceiptService(clock=FakeClock(_NOW), session_factory=world["factory"])

    with pytest.raises(ValidationError, match="limit"):
        await service.recent(world["ctx"], limit=5000)
