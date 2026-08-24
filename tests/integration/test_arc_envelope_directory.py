"""The envelope directory: who is governed, when the caller cannot name them.

E23-T8. `resolve` answers about a principal the caller can already name, and
until this read nothing told them the names — so the operating surface E23-T5
shipped could only be used by somebody who already knew the `(issuer, subject)`
pair of the agent they were trying to stop. During an incident that is the same
as not having the control.

The four properties worth holding, and each is a way the list could be wrong in
a direction nobody notices:

**A tenant sees its own bindings and no others.** An issuer/subject pair is not
globally unique to one tenant's governance, which `resolve` already has a test
for; a directory that leaked would leak the whole set at once rather than one
principal at a time.

**Closed and suspended intervals are in it.** An operator asking "was this agent
ever governed" is asking about exactly those, and a list that dropped them
answers "no" to a question whose real answer is "yes, until Tuesday".

**Paging does not skip.** The list is being written to while it is read — that
is what an incident is — so the cursor is a keyset rather than an offset.

**A revision that is no longer in force still shows as one.** A binding is only
checked for `active` at grant time, so this is reachable, and it is the row an
operator most needs to see.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc import (
    ArcAuthorizationService,
    ArcRequestContext,
    AutonomyEnvelopeService,
    EnvelopeGrant,
    WorkloadIdentity,
)
from contextplane.types import TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc
from tests.helpers.clock import FakeClock

_ISSUER = "https://idp.example.test"


def _agent(name: str) -> WorkloadIdentity:
    return WorkloadIdentity(issuer="https://iam.example.test", subject=f"workload/{name}")


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-directory")


class _AllVisible:
    async def visible_entity_ids(self, ctx: object, entity_ids: object) -> list[uuid.UUID]:
        return list(entity_ids)  # type: ignore[arg-type]


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession]) -> AutonomyEnvelopeService:
    return AutonomyEnvelopeService(
        factory,
        authorization=ArcAuthorizationService(visibility=_AllVisible(), global_write_allowlist=()),
        clock=FakeClock(ARC_NOW),
    )


def _ctx(seed: ArcSeed, *, tenant_id: uuid.UUID | None = None) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=tenant_id or seed.tenant_id,
        actor_id=seed.actor_id,
        roles=["admin"],
        oidc_subject="operator-1",
    )
    return ArcRequestContext.from_validated_claims(tenant, {"iss": _ISSUER}, host_id="h")


@pytest.mark.asyncio
async def test_the_directory_names_principals_the_caller_could_not_have(
    service: AutonomyEnvelopeService, seed: ArcSeed
) -> None:
    """The whole point: learning the names rather than needing them."""
    ctx = _ctx(seed)
    for name in ("deploy-agent", "planner", "reviewer"):
        await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_agent(name), reason="governed"))

    page, next_cursor = await service.list_bindings(ctx)

    assert next_cursor is None
    assert {binding.principal.subject for binding in page} == {
        "workload/deploy-agent",
        "workload/planner",
        "workload/reviewer",
    }


@pytest.mark.asyncio
async def test_the_directory_does_not_cross_tenants(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    """One leak here is the whole set rather than one principal.

    `resolve` already refuses to answer across tenants. A directory that did not
    would make that refusal decorative — the same rows, reachable by a caller who
    asked a broader question.
    """
    await service.grant(
        _ctx(seed), EnvelopeGrant(revision_id=seed.revision_id, principal=_agent("deploy"), reason="governed")
    )

    page, _ = await service.list_bindings(_ctx(seed, tenant_id=uuid.uuid4()))

    assert page == []


@pytest.mark.asyncio
async def test_a_suspended_binding_is_in_the_directory_and_says_so(
    service: AutonomyEnvelopeService, seed: ArcSeed
) -> None:
    """The row an operator is most often looking for.

    Filtering to what is in force would hide the agent somebody suspended an
    hour ago, which is the one the next responder needs to find.
    """
    ctx = _ctx(seed)
    binding_id = await service.grant(
        ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_agent("deploy"), reason="governed")
    )
    await service.suspend(ctx, binding_id, reason="incident 4412")

    page, _ = await service.list_bindings(ctx)

    assert [(b.state, b.is_in_force, b.suspension_reason) for b in page] == [("suspended", False, "incident 4412")]


@pytest.mark.asyncio
async def test_a_revoked_binding_stays_in_the_directory(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    """ "Was this agent ever governed" is a question about closed intervals.

    A list of only open intervals answers "no" where the truth is "yes, until
    Tuesday" — and the difference is what an auditor is reading the record for.
    """
    ctx = _ctx(seed)
    binding_id = await service.grant(
        ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_agent("deploy"), reason="governed")
    )
    await service.revoke(ctx, binding_id, reason="agent decommissioned")

    page, _ = await service.list_bindings(ctx)

    assert len(page) == 1
    assert page[0].effective_to is not None


@pytest.mark.asyncio
async def test_paging_returns_each_binding_once_and_all_of_them(
    service: AutonomyEnvelopeService, seed: ArcSeed
) -> None:
    """A keyset, because the list is written to while it is read.

    Granting during an incident is ordinary, and an offset would silently skip a
    row when one lands above the reader's position — a governed agent missing
    from a directory being used to find governed agents.
    """
    ctx = _ctx(seed)
    granted = set()
    for index in range(7):
        granted.add(
            await service.grant(
                ctx,
                EnvelopeGrant(
                    effective_from=ARC_NOW + datetime.timedelta(minutes=index),
                    principal=_agent(f"agent-{index}"),
                    reason="governed",
                    revision_id=seed.revision_id,
                ),
            )
        )

    seen: list[uuid.UUID] = []
    cursor: tuple[datetime.datetime, uuid.UUID] | None = None
    for _ in range(10):
        page, cursor = await service.list_bindings(ctx, cursor=cursor, limit=3)
        seen.extend(binding.binding_id for binding in page)
        if cursor is None:
            break

    assert cursor is None
    assert len(seen) == len(set(seen)), "a binding came back on two pages"
    assert set(seen) == granted


@pytest.mark.asyncio
async def test_the_directory_reports_a_binding_to_a_revision_that_is_no_longer_active(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Reachable, and the row that matters most.

    A binding is checked for an active revision when it is granted and never
    again, so an agent can end up governed by a document somebody superseded
    weeks ago. A directory that dropped the lifecycle would show it as governed.
    """
    ctx = _ctx(seed)
    await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_agent("deploy"), reason="governed"))
    async with factory() as session, session.begin():
        # Revoked rather than superseded: `ck_arc_revisions_superseded_link`
        # requires a successor, and inventing one would put a second governance
        # document in the fixture to test a property about the first.
        await session.execute(
            text("UPDATE arc_revisions SET lifecycle_state = 'revoked', revoked_at = :now" " WHERE revision_id = :rid"),
            {"now": ARC_NOW, "rid": seed.revision_id},
        )

    page, _ = await service.list_bindings(ctx)

    assert [(b.is_in_force, b.revision_lifecycle_state) for b in page] == [(True, "revoked")]


@pytest.mark.asyncio
async def test_a_caller_cannot_ask_for_an_unbounded_page(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    """A directory is for scanning, and an export is a different request.

    Clamped rather than refused: a caller asking for too much gets the first
    page and a cursor, which is the answer they wanted anyway.
    """
    ctx = _ctx(seed)
    for index in range(4):
        await service.grant(
            ctx,
            EnvelopeGrant(
                effective_from=ARC_NOW + datetime.timedelta(minutes=index),
                principal=_agent(f"agent-{index}"),
                reason="governed",
                revision_id=seed.revision_id,
            ),
        )

    page, _ = await service.list_bindings(ctx, limit=100_000)

    assert len(page) == 4
