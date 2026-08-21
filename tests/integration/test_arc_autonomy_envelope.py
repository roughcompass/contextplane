"""Autonomy-envelope bindings: which `policy` revision governs which principal.

An envelope decides what an agent may do, so every test here is about a way that
decision could be made by the wrong person, about the wrong document, or about a
principal who already has one.

The three that matter most:

**A tenant admin cannot displace a deployment operator's global envelope.**
`test_a_tenant_admin_cannot_displace_a_global_envelope` is a regression test for
a real escalation this module shipped in an intermediate form, where suspend and
revoke were both authorized at tenant scope and a suspension freed the
principal's exclusion slot. Every step was individually authorized and the
sequence was not.

**A principal cannot end up with two envelopes over one window.** Not because
the service checks -- a check followed by an insert is two statements a
concurrent grant interleaves between -- but because an exclusion constraint makes
the state unconstructible. The concurrency test is what distinguishes those two
claims.

**An envelope is an active `policy` revision and nothing else.** A `runbook`'s
applicability rules were written to select corpus, and binding a principal to
one would silently turn them into that principal's authority; a `draft` has been
through no approval at all. Both the service and a composite foreign key refuse
the wrong kind, and the direct-insert test is what proves the second, because a
service-only guard is one refactor from gone.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.authorization import ArcAuthorizationError, ArcAuthorizationService
from contextplane.arc.service.autonomy_envelope import (
    AutonomyEnvelopeService,
    EnvelopeAlreadyBound,
    EnvelopeGrant,
    NotAnEnvelope,
    WorkloadIdentity,
)
from contextplane.arc.types import ArcRequestContext
from contextplane.audit import actions
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.types import TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc
from tests.helpers.clock import FakeClock

_ISSUER = "https://idp.example.test"
_AGENT = WorkloadIdentity(issuer="https://iam.example.test", subject="workload/deploy-agent")


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-envelope")


class _AllVisible:
    async def visible_entity_ids(self, ctx: object, entity_ids: object) -> list[uuid.UUID]:
        return list(entity_ids)  # type: ignore[arg-type]


def _service(
    factory: async_sessionmaker[AsyncSession],
    *,
    allowlist: tuple[tuple[str, str], ...] = (),
    clock: FakeClock | None = None,
) -> AutonomyEnvelopeService:
    return AutonomyEnvelopeService(
        factory,
        authorization=ArcAuthorizationService(visibility=_AllVisible(), global_write_allowlist=allowlist),
        clock=clock or FakeClock(ARC_NOW),
    )


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession]) -> AutonomyEnvelopeService:
    return _service(factory)


def _ctx(seed: ArcSeed, *, tenant_id: uuid.UUID | None = None, roles: list[str] | None = None) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=tenant_id or seed.tenant_id,
        actor_id=seed.actor_id,
        roles=roles if roles is not None else ["admin"],
        oidc_subject="operator-1",
    )
    return ArcRequestContext.from_validated_claims(tenant, {"iss": _ISSUER}, host_id="h")


async def _seed_revision_of_kind(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, kind: str, *, lifecycle_state: str = "active"
) -> tuple[uuid.UUID, uuid.UUID]:
    """A second artifact of some other kind, and one revision of it."""
    artifact_id, revision_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_artifacts ("
                "  artifact_id, tenant_id, slug, kind, title, created_at, created_by_issuer, created_by_subject"
                ") VALUES (:aid, :tid, :slug, :kind, :title, :now, :iss, :sub)"
            ),
            {
                "aid": artifact_id,
                "tid": seed.tenant_id,
                "slug": f"k-{artifact_id.hex[:8]}",
                "kind": kind,
                "title": f"A {kind}",
                "now": ARC_NOW,
                "iss": _ISSUER,
                "sub": "seed",
            },
        )
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, source_body_plaintext, created_at"
                ") VALUES (:rid, :aid, :tid, 'test-system', :loc, :rloc, :digest, :lifecycle, :efrom,"
                "  :review, 'all_matched_actors', 'revision_pinned_only', 'internal',"
                "  :retention, 'none', 'body', :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "tid": seed.tenant_id,
                "lifecycle": lifecycle_state,
                "loc": f"loc://{revision_id.hex[:8]}",
                "rloc": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": ARC_NOW - datetime.timedelta(days=1),
                "review": ARC_NOW + datetime.timedelta(days=365),
                "retention": ARC_NOW + datetime.timedelta(days=730),
                "now": ARC_NOW,
            },
        )
    return artifact_id, revision_id


async def _audit_events(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> list[str]:
    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT event_type FROM arc_audit_outbox WHERE tenant_id = :tid ORDER BY created_at, event_type"),
                {"tid": tenant_id},
            )
        ).all()
    return [row[0] for row in rows]


# -- granting ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_granted_envelope_resolves_for_its_principal(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    ctx = _ctx(seed)
    binding_id = await service.grant(
        ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="initial grant")
    )

    bound = await service.resolve(ctx, _AGENT)

    assert bound is not None
    assert bound.binding_id == binding_id
    assert bound.revision_id == seed.revision_id
    assert bound.artifact_id == seed.artifact_id
    assert bound.principal == _AGENT
    assert bound.is_in_force
    assert actions.ARC_ENVELOPE_BOUND in await _audit_events(factory, seed.tenant_id)


@pytest.mark.asyncio
async def test_an_ungoverned_principal_resolves_to_nothing(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    """`None` is a real answer, distinct from a suspended envelope.

    The decision path branches on the difference: one is a principal nobody has
    governed, the other is a posture an operator chose.
    """
    assert await service.resolve(_ctx(seed), WorkloadIdentity(issuer="i", subject="never-granted")) is None


@pytest.mark.asyncio
async def test_an_envelope_does_not_resolve_across_tenants(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The same workload identity in another tenant is another principal.

    An issuer/subject pair is not globally unique to one tenant's governance,
    and a resolve that ignored the tenant would hand one tenant's agent another
    tenant's authority.
    """
    await service.grant(_ctx(seed), EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))

    other = await seed_arc(factory, slug_prefix="arc-envelope-other")
    assert await service.resolve(_ctx(other), _AGENT) is None


@pytest.mark.asyncio
async def test_a_second_envelope_for_one_principal_is_refused(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    ctx = _ctx(seed)
    await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="first"))

    with pytest.raises(EnvelopeAlreadyBound):
        await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="second"))


@pytest.mark.asyncio
async def test_two_concurrent_grants_cannot_both_win(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The point of the exclusion constraint rather than a read-then-write.

    Both grants read an empty table before either writes. A service-side check
    would let both through; the database is what makes the second impossible.
    """
    ctx = _ctx(seed)
    results = await asyncio.gather(
        service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="a")),
        service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="b")),
        return_exceptions=True,
    )

    granted = [r for r in results if isinstance(r, uuid.UUID)]
    refused = [r for r in results if isinstance(r, Exception)]
    assert len(granted) == 1, results
    assert len(refused) == 1
    assert isinstance(refused[0], EnvelopeAlreadyBound), refused[0]

    async with factory() as session:
        live = (
            await session.execute(
                text(
                    "SELECT count(*) FROM arc_autonomy_envelope_bindings " "WHERE tenant_id = :tid AND state = 'active'"
                ),
                {"tid": seed.tenant_id},
            )
        ).scalar_one()
    assert live == 1


@pytest.mark.asyncio
async def test_two_principals_may_share_one_envelope_revision(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    """The constraint is per principal, not per revision.

    One template envelope governing a fleet is the ordinary case, and a
    constraint that keyed on the revision would forbid it.
    """
    ctx = _ctx(seed)
    other = WorkloadIdentity(issuer=_AGENT.issuer, subject="workload/build-agent")
    await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="a"))
    await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=other, reason="b"))

    assert (await service.resolve(ctx, _AGENT)) is not None
    assert (await service.resolve(ctx, other)) is not None


# -- what may be bound -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_runbook_revision_is_not_an_envelope(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    _, revision_id = await _seed_revision_of_kind(factory, seed, "runbook")

    with pytest.raises(NotAnEnvelope) as caught:
        await service.grant(_ctx(seed), EnvelopeGrant(revision_id=revision_id, principal=_AGENT, reason="r"))

    assert "runbook" in str(caught.value), "the refusal names what the caller actually pointed at"


@pytest.mark.asyncio
async def test_the_database_refuses_a_non_policy_binding_written_directly(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The composite foreign key, not the service, is what holds this.

    A service-only guard is one refactor away from gone, and the consequence of
    losing it is a `runbook`'s corpus-selection rules quietly becoming an
    agent's authority. So the write is attempted around the service.
    """
    artifact_id, revision_id = await _seed_revision_of_kind(factory, seed, "standard")

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO arc_autonomy_envelope_bindings ("
                    "  binding_id, tenant_id, revision_id, artifact_id, artifact_kind,"
                    "  principal_issuer, principal_subject, state, effective_from,"
                    "  actor, reason, recorded_at"
                    ") VALUES (:bid, :tid, :rid, :aid, 'policy', 'i', 's', 'active', :now, 'a', 'r', :now)"
                ),
                {
                    "bid": uuid.uuid4(),
                    "tid": seed.tenant_id,
                    "rid": revision_id,
                    "aid": artifact_id,
                    "now": ARC_NOW,
                },
            )


@pytest.mark.asyncio
async def test_binding_an_unknown_revision_is_not_found(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    with pytest.raises(NotFoundError):
        await service.grant(_ctx(seed), EnvelopeGrant(revision_id=uuid.uuid4(), principal=_AGENT, reason="r"))


@pytest.mark.asyncio
async def test_a_draft_revision_may_not_be_bound(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A draft has been through no approval and no actor separation.

    Binding a principal to one would let whoever can write a draft decide what
    an agent may do, skipping the pipeline that exists to stop exactly that.
    """
    _, draft = await _seed_revision_of_kind(factory, seed, "policy", lifecycle_state="draft")

    with pytest.raises(NotAnEnvelope, match="draft"):
        await service.grant(_ctx(seed), EnvelopeGrant(revision_id=draft, principal=_AGENT, reason="r"))


@pytest.mark.asyncio
async def test_an_operator_cannot_bind_another_tenants_envelope(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A tenant-scoped envelope governs only its own tenant's principals.

    A tenant admin is stopped by the artifact write gate. A deployment operator
    bypasses that gate -- that is what break-glass means -- so this is checked
    separately, or the trust root could bind one tenant's agent to another
    tenant's governance by naming the wrong revision id.
    """
    other = await seed_arc(factory, slug_prefix="arc-envelope-foreign")
    operator = _service(factory, allowlist=((_ISSUER, "operator-1"),))

    with pytest.raises(NotAnEnvelope, match="another tenant"):
        await operator.grant(_ctx(seed), EnvelopeGrant(revision_id=other.revision_id, principal=_AGENT, reason="r"))


@pytest.mark.asyncio
async def test_resolve_reports_the_bound_revisions_lifecycle_state(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A revision revoked after binding leaves the binding alone, and says so.

    Ending bindings as a side effect of revoking a revision would be a decision
    taken silently. Hiding the revocation from the decision path would be worse.
    So the read carries it and the decision path chooses.
    """
    ctx = _ctx(seed)
    await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))
    assert (await service.resolve(ctx, _AGENT)).revision_lifecycle_state == "active"  # type: ignore[union-attr]

    async with factory() as session, session.begin():
        await session.execute(
            # `ck_arc_revisions_revoked_at` requires the timestamp alongside the
            # state -- a revocation that does not say when is not a revocation.
            text("UPDATE arc_revisions SET lifecycle_state = 'revoked', revoked_at = :now " "WHERE revision_id = :rid"),
            {"rid": seed.revision_id, "now": ARC_NOW},
        )

    bound = await service.resolve(ctx, _AGENT)
    assert bound is not None
    assert bound.is_in_force, "the binding is still switched on; only the document was withdrawn"
    assert bound.revision_lifecycle_state == "revoked"


def test_a_workload_identity_is_stored_stripped() -> None:
    """Resolution matches exactly, so an unstripped subject binds and resolves
    for nobody -- a row that reads as governed and governs nothing."""
    assert WorkloadIdentity(issuer=" https://iam ", subject=" agent-1\n") == WorkloadIdentity(
        issuer="https://iam", subject="agent-1"
    )


# -- who may grant -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plain_reader_cannot_grant_an_envelope(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    """Granting an envelope is a governance write, not a read-scoped action.

    Nothing about holding a revision id should let a reader decide what an agent
    may do.
    """
    with pytest.raises(ArcAuthorizationError):
        await service.grant(
            _ctx(seed, roles=["reader"]),
            EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="r"),
        )


@pytest.mark.asyncio
async def test_a_tenant_admin_cannot_grant_a_global_envelope(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A global envelope is deployment-wide authority, which no tenant role reaches.

    The scope is read off the artifact rather than taken from the caller,
    precisely so a tenant admin cannot claim tenant scope for a global document.
    """
    global_revision = await _seed_global_policy_revision(factory)

    with pytest.raises(ArcAuthorizationError):
        await service.grant(_ctx(seed), EnvelopeGrant(revision_id=global_revision, principal=_AGENT, reason="r"))


@pytest.mark.asyncio
async def test_a_deployment_operator_may_grant_a_global_envelope(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    global_revision = await _seed_global_policy_revision(factory)
    allowlisted = _service(factory, allowlist=((_ISSUER, "operator-1"),))

    binding_id = await allowlisted.grant(
        _ctx(seed, roles=["reader"]),
        EnvelopeGrant(revision_id=global_revision, principal=_AGENT, reason="fleet template"),
    )

    assert binding_id is not None


@pytest.mark.asyncio
async def test_a_tenant_admin_may_suspend_a_global_envelope_binding(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Turning authority *off* is authorized at the binding, not the envelope.

    This is the whole point of instant suspend. An earlier version of this
    module authorized every status change at the envelope's scope, which meant a
    tenant admin whose agent was misbehaving under a global envelope had to find
    a deployment operator before they could switch it off -- exactly the
    situation the fast path exists for. Narrowing grants nothing the actor did
    not already hold, so tenant admin is the right bar.
    """
    global_revision = await _seed_global_policy_revision(factory)
    operator = _service(factory, allowlist=((_ISSUER, "operator-1"),))
    binding_id = await operator.grant(
        _ctx(seed), EnvelopeGrant(revision_id=global_revision, principal=_AGENT, reason="fleet template")
    )

    # A plain tenant admin, with no place on the deployment allowlist.
    tenant_admin = _service(factory)
    await tenant_admin.suspend(_ctx(seed), binding_id, reason="incident 4471")

    bound = await tenant_admin.resolve(_ctx(seed), _AGENT)
    assert bound is not None
    assert not bound.is_in_force


@pytest.mark.asyncio
async def test_a_tenant_admin_cannot_reinstate_a_global_envelope_binding(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Turning authority back *on* is authorized like a grant.

    The mirror image of the test above, and the reason the asymmetry is not
    simply "status changes are cheap": if reinstating took tenant admin, a
    tenant could undo a deployment operator's suspension of a global envelope
    and the operator's switch would not be a switch.
    """
    global_revision = await _seed_global_policy_revision(factory)
    operator = _service(factory, allowlist=((_ISSUER, "operator-1"),))
    binding_id = await operator.grant(
        _ctx(seed), EnvelopeGrant(revision_id=global_revision, principal=_AGENT, reason="fleet template")
    )
    await operator.suspend(_ctx(seed), binding_id, reason="misbehaving")

    tenant_admin = _service(factory)
    with pytest.raises(ArcAuthorizationError):
        await tenant_admin.reinstate(_ctx(seed), binding_id, reason="looks fine to me")

    await operator.reinstate(_ctx(seed), binding_id, reason="root cause fixed")
    bound = await operator.resolve(_ctx(seed), _AGENT)
    assert bound is not None
    assert bound.is_in_force


@pytest.mark.asyncio
async def test_a_tenant_admin_cannot_revoke_a_global_envelope_binding(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Revoking frees the principal's slot, so it is authorized like a grant.

    Revoking only narrows, which is why an earlier version put it with suspend.
    But closing the interval is the first half of a substitution, and the second
    half is a grant the tenant admin *can* make -- of an envelope they wrote.
    See the escalation regression test below.
    """
    global_revision = await _seed_global_policy_revision(factory)
    operator = _service(factory, allowlist=((_ISSUER, "operator-1"),))
    binding_id = await operator.grant(
        _ctx(seed), EnvelopeGrant(revision_id=global_revision, principal=_AGENT, reason="fleet template")
    )

    tenant_admin = _service(factory)
    with pytest.raises(ArcAuthorizationError):
        await tenant_admin.revoke(_ctx(seed), binding_id, reason="agent decommissioned")

    await operator.revoke(_ctx(seed), binding_id, reason="agent decommissioned")
    assert await operator.resolve(_ctx(seed), _AGENT) is None


@pytest.mark.asyncio
async def test_a_tenant_admin_cannot_displace_a_global_envelope(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The escalation this module's authorization rules exist to prevent.

    An intermediate version authorized suspend *and* revoke at tenant scope on
    the reasoning that both only narrow, and restricted the exclusion constraint
    to `active` rows so a suspension freed the principal's slot. Each step below
    then passed its own check while the sequence replaced deployment-mandated
    governance with governance the tenant admin wrote:

        suspend the operator's binding -> slot freed
        author a tenant `policy` (tenant admin may) -> grant it to the same
        principal -> `resolve` prefers the active row and returns it

    Reasoning one operation at a time said nothing about the trace. Both halves
    are now closed -- the constraint reserves the slot regardless of state, and
    revoke is authorized at the envelope's scope -- and this test fails if
    either regresses.
    """
    global_revision = await _seed_global_policy_revision(factory)
    operator = _service(factory, allowlist=((_ISSUER, "operator-1"),))
    binding_g = await operator.grant(
        _ctx(seed), EnvelopeGrant(revision_id=global_revision, principal=_AGENT, reason="deployment mandate")
    )

    tenant_admin = _service(factory)
    await tenant_admin.suspend(_ctx(seed), binding_g, reason="incident")

    # Their own tenant-scoped policy, which they are perfectly entitled to write.
    _, mine = await _seed_revision_of_kind(factory, seed, "policy")

    # Route one: the suspension did not free the slot.
    with pytest.raises(EnvelopeAlreadyBound):
        await tenant_admin.grant(_ctx(seed), EnvelopeGrant(revision_id=mine, principal=_AGENT, reason="mine"))

    # Route two: freeing it requires authority over the envelope being displaced.
    with pytest.raises(ArcAuthorizationError):
        await tenant_admin.revoke(_ctx(seed), binding_g, reason="clearing the way")

    still = await tenant_admin.resolve(_ctx(seed), _AGENT)
    assert still is not None
    assert still.revision_id == global_revision, "the operator's envelope is still the one bound"


async def _seed_global_policy_revision(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A `policy` artifact with no owning tenant, and one revision of it."""
    artifact_id, revision_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_artifacts ("
                "  artifact_id, tenant_id, slug, kind, title, created_at, created_by_issuer, created_by_subject"
                ") VALUES (:aid, NULL, :slug, 'policy', 'Global envelope', :now, :iss, 'seed')"
            ),
            {"aid": artifact_id, "slug": f"g-{artifact_id.hex[:8]}", "now": ARC_NOW, "iss": _ISSUER},
        )
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, created_at"
                ") VALUES (:rid, :aid, NULL, 'test-system', :loc, :rloc, :digest, 'active', :efrom,"
                "  :review, 'all_matched_actors', 'revision_pinned_only', 'internal',"
                # No `source_body_plaintext`: `ck_arc_revisions_no_global_plaintext`
                # forbids a global revision from carrying one, which is the point
                # of a global document -- its body lives where every tenant may
                # read it, not in a column scoped to one.
                "  :retention, 'none', :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "loc": f"loc://{revision_id.hex[:8]}",
                "rloc": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": ARC_NOW - datetime.timedelta(days=1),
                "review": ARC_NOW + datetime.timedelta(days=365),
                "retention": ARC_NOW + datetime.timedelta(days=730),
                "now": ARC_NOW,
            },
        )
    return revision_id


# -- suspend, reinstate, revoke ----------------------------------------------


@pytest.mark.asyncio
async def test_suspending_leaves_the_binding_readable_and_off(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A suspension is a posture, not a deletion.

    The row keeps saying who it governs and gains a record of why it is off, so
    an operator reads a suspension rather than a gap.
    """
    ctx = _ctx(seed)
    binding_id = await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))

    await service.suspend(ctx, binding_id, reason="incident 4471")

    bound = await service.resolve(ctx, _AGENT)
    assert bound is not None
    assert bound.state == "suspended"
    assert not bound.is_in_force
    assert bound.suspension_reason == "incident 4471"
    assert bound.suspended_at == ARC_NOW
    assert actions.ARC_ENVELOPE_SUSPENDED in await _audit_events(factory, seed.tenant_id)


@pytest.mark.asyncio
async def test_a_suspension_does_not_free_the_slot(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """An open interval reserves the principal whatever the state.

    The first version of the exclusion constraint carried `WHERE state =
    'active'` so a suspension would free the slot for a widen. That turned
    suspension -- the one operation authorized at tenant scope -- into the first
    half of a substitution. Now suspension reserves nothing less than an active
    binding does, so it is purely a narrowing and the widen path is revoke first.
    """
    ctx = _ctx(seed)
    first = await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="v1"))
    await service.suspend(ctx, first, reason="incident")

    _, wider = await _seed_policy_revision(factory, seed)
    with pytest.raises(EnvelopeAlreadyBound):
        await service.grant(ctx, EnvelopeGrant(revision_id=wider, principal=_AGENT, reason="v2"))


@pytest.mark.asyncio
async def test_the_widen_path_is_revoke_then_grant(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Ending the old binding, not suspending it, is what makes room.

    Also the fix for a zombie the suspend-then-grant path left behind: the
    superseded binding kept an open interval forever, so once its replacement
    was revoked `resolve` returned the *old suspended* row and reported a
    principal as suspended for a reason recorded during a widen months earlier.
    A revoked binding's interval is closed, so it can never come back.
    """
    ctx = _ctx(seed)
    first = await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="v1"))
    await service.revoke(ctx, first, reason="widening")

    _, wider = await _seed_policy_revision(factory, seed)
    second = await service.grant(ctx, EnvelopeGrant(revision_id=wider, principal=_AGENT, reason="v2"))

    bound = await service.resolve(ctx, _AGENT)
    assert bound is not None
    assert bound.binding_id == second
    assert bound.revision_id == wider

    # And once the replacement ends, the principal is ungoverned -- not
    # governed by the resurrected first binding.
    await service.revoke(ctx, second, reason="decommissioned")
    assert await service.resolve(ctx, _AGENT) is None


@pytest.mark.asyncio
async def test_a_revoked_binding_can_be_neither_suspended_nor_reinstated(
    service: AutonomyEnvelopeService, seed: ArcSeed
) -> None:
    """`state` and the interval are two halves of one lifecycle.

    `revoke` closes the interval without touching `state`, so a revoked binding
    reads `state = 'active'` forever. With the flips guarded only on `state`, a
    revoked binding could be suspended (emitting an audit event for a binding
    that ended) and then reinstated (leaving `state = 'active'` with
    `effective_to` in the past, so a `resolve(at=...)` inside the old window
    reported it as active for a period it was actually suspended -- retroactively
    rewriting governance history). Both statements now also require an open
    interval.
    """
    ctx = _ctx(seed)
    binding_id = await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="v1"))
    await service.revoke(ctx, binding_id, reason="ended")

    with pytest.raises(NotFoundError):
        await service.suspend(ctx, binding_id, reason="too late")
    with pytest.raises(NotFoundError):
        await service.reinstate(ctx, binding_id, reason="too late")


@pytest.mark.asyncio
async def test_reinstating_turns_an_envelope_back_on(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    ctx = _ctx(seed)
    binding_id = await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))
    await service.suspend(ctx, binding_id, reason="incident")

    await service.reinstate(ctx, binding_id, reason="incident closed")

    bound = await service.resolve(ctx, _AGENT)
    assert bound is not None
    assert bound.is_in_force
    assert bound.suspended_at is None
    assert bound.suspension_reason is None
    assert actions.ARC_ENVELOPE_REINSTATED in await _audit_events(factory, seed.tenant_id)


@pytest.mark.asyncio
async def test_revoking_ends_the_binding_rather_than_flipping_it(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    ctx = _ctx(seed)
    binding_id = await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))

    await service.revoke(ctx, binding_id, reason="agent decommissioned")

    assert await service.resolve(ctx, _AGENT) is None
    assert actions.ARC_ENVELOPE_REVOKED in await _audit_events(factory, seed.tenant_id)


@pytest.mark.asyncio
async def test_revoking_a_binding_that_never_took_effect_closes_it_empty(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Granted and withdrawn in the same instant is an empty interval, not an
    inverted one.

    "In force for no time" is the true record. Deleting the row instead would
    erase an authority decision somebody made, and an inverted interval is a
    state the CHECK refuses -- which is what this used to hit. An empty
    `tstzrange` also overlaps nothing, so the principal's slot is free with no
    special case in the exclusion constraint; the regrant below is what proves
    that rather than assuming it.
    """
    ctx = _ctx(seed)
    binding_id = await service.grant(
        ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="granted in error")
    )

    await service.revoke(ctx, binding_id, reason="withdrawn immediately")

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT effective_from, effective_to FROM arc_autonomy_envelope_bindings " "WHERE binding_id = :bid"
                ),
                {"bid": binding_id},
            )
        ).one()
    assert row.effective_to == row.effective_from

    regranted = await service.grant(
        ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="the real one")
    )
    bound = await service.resolve(ctx, _AGENT)
    assert bound is not None
    assert bound.binding_id == regranted


@pytest.mark.asyncio
async def test_suspending_a_binding_in_another_tenant_is_not_found(
    service: AutonomyEnvelopeService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Holding a binding id from another tenant confers nothing."""
    binding_id = await service.grant(
        _ctx(seed), EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant")
    )
    other = await seed_arc(factory, slug_prefix="arc-envelope-third")

    with pytest.raises(NotFoundError):
        await service.suspend(_ctx(other), binding_id, reason="not mine")


@pytest.mark.asyncio
async def test_suspending_an_already_suspended_binding_is_not_found(
    service: AutonomyEnvelopeService, seed: ArcSeed
) -> None:
    """A no-op update must not report success, or the audit trail gains an
    event for something that did not happen."""
    ctx = _ctx(seed)
    binding_id = await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))
    await service.suspend(ctx, binding_id, reason="first")

    with pytest.raises(NotFoundError):
        await service.suspend(ctx, binding_id, reason="second")


@pytest.mark.asyncio
async def test_a_plain_reader_cannot_suspend_an_envelope(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    binding_id = await service.grant(
        _ctx(seed), EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant")
    )

    with pytest.raises(ArcAuthorizationError):
        await service.suspend(_ctx(seed, roles=["reader"]), binding_id, reason="r")


# -- intervals and input -----------------------------------------------------


@pytest.mark.asyncio
async def test_an_expired_binding_does_not_resolve(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """Time is advanced rather than the binding backdated.

    Backdating is refused, so a binding that has already expired can only be
    reached by granting one that expires and then letting the clock move --
    which is also how it happens in production.
    """
    clock = FakeClock(ARC_NOW)
    service = _service(factory, clock=clock)
    ctx = _ctx(seed)
    await service.grant(
        ctx,
        EnvelopeGrant(
            revision_id=seed.revision_id,
            principal=_AGENT,
            reason="temporary",
            effective_to=ARC_NOW + datetime.timedelta(days=1),
        ),
    )
    assert await service.resolve(ctx, _AGENT) is not None

    clock.tick(datetime.timedelta(days=2))

    assert await service.resolve(ctx, _AGENT) is None
    assert await service.resolve(ctx, _AGENT, at=ARC_NOW) is not None


@pytest.mark.asyncio
async def test_a_binding_may_not_be_backdated(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    """`resolve(at=...)` is what a later audit reads to ask whether an action was
    within envelope. A binding that may start in the past lets the party being
    audited write that answer afterwards."""
    with pytest.raises(ValidationError, match="not backdatable"):
        await service.grant(
            _ctx(seed),
            EnvelopeGrant(
                revision_id=seed.revision_id,
                principal=_AGENT,
                reason="r",
                effective_from=ARC_NOW - datetime.timedelta(days=1),
            ),
        )


@pytest.mark.asyncio
async def test_two_non_overlapping_bindings_may_both_exist(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The constraint is about overlap, not about history.

    A principal's succession of envelopes is exactly what an auditor reads, so
    it must be recordable -- and, since backdating is refused, it accumulates by
    the clock moving rather than by writing the past.
    """
    clock = FakeClock(ARC_NOW)
    service = _service(factory, clock=clock)
    ctx = _ctx(seed)
    await service.grant(
        ctx,
        EnvelopeGrant(
            revision_id=seed.revision_id,
            principal=_AGENT,
            reason="this year",
            effective_to=ARC_NOW + datetime.timedelta(days=200),
        ),
    )

    clock.tick(datetime.timedelta(days=200))
    _, later = await _seed_policy_revision(factory, seed)
    await service.grant(ctx, EnvelopeGrant(revision_id=later, principal=_AGENT, reason="from now on"))

    bound = await service.resolve(ctx, _AGENT)
    assert bound is not None
    assert bound.revision_id == later
    # The earlier one is still readable at the instant it governed.
    earlier = await service.resolve(ctx, _AGENT, at=ARC_NOW)
    assert earlier is not None
    assert earlier.revision_id == seed.revision_id


@pytest.mark.asyncio
async def test_an_inverted_interval_is_refused(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    with pytest.raises(ValidationError):
        await service.grant(
            _ctx(seed),
            EnvelopeGrant(
                revision_id=seed.revision_id,
                principal=_AGENT,
                reason="r",
                effective_from=ARC_NOW,
                effective_to=ARC_NOW - datetime.timedelta(days=1),
            ),
        )


@pytest.mark.asyncio
async def test_every_change_requires_a_reason(service: AutonomyEnvelopeService, seed: ArcSeed) -> None:
    """A blank string satisfies NOT NULL, which is how audit trails become
    "somebody changed this"."""
    ctx = _ctx(seed)
    with pytest.raises(ValidationError):
        await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="   "))

    binding_id = await service.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))
    with pytest.raises(ValidationError):
        await service.suspend(ctx, binding_id, reason="")


def test_a_workload_identity_needs_both_halves() -> None:
    """A half-empty identity would resolve against a column of empty strings."""
    with pytest.raises(ValueError, match="issuer and a subject"):
        WorkloadIdentity(issuer="", subject="s")
    with pytest.raises(ValueError, match="issuer and a subject"):
        WorkloadIdentity(issuer="i", subject="  ")


def test_the_requesting_principal_is_the_allowlist_pair(seed: ArcSeed) -> None:
    """`of_requester` must read the same pair the operator allowlist matches on,
    or an agent acting for itself would resolve a different envelope than the
    one authorization thinks it has."""
    ctx = _ctx(seed)
    assert WorkloadIdentity.of_requester(ctx) == WorkloadIdentity(issuer=_ISSUER, subject="operator-1")
    assert (ctx.operator_identity) == (_ISSUER, "operator-1")


async def _seed_policy_revision(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> tuple[uuid.UUID, uuid.UUID]:
    return await _seed_revision_of_kind(factory, seed, "policy")
