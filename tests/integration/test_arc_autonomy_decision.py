"""The authority decision against a real database: nothing is cached.

`tests/unit/test_arc_autonomy_decision.py` covers the verdict logic as a pure
function. This file covers the property that logic depends on and that only a
database can demonstrate: **both reads happen on every decision**, so a
suspension takes effect at the next one with no invalidation, no sweeper and no
staleness window.

That is the whole of the suspension mechanism, and it is stated as a bound on
operations rather than on wall-clock: a suspended envelope authorises no
operation that begins after the flip commits. A wall-clock figure would be a
claim about how long an in-flight operation may run, which this service does not
bound, and it would be unobservable anyway -- the latency histogram's buckets
top out at ten seconds.

The tests here flip a row through the service and then decide again through the
same service instance -- no restart, no new object, nothing given a chance to
notice.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.autonomy_decision import AutonomyDecisionService, EnvelopeVerdict
from contextplane.arc.service.autonomy_envelope import AutonomyEnvelopeService, EnvelopeGrant, WorkloadIdentity
from contextplane.arc.types import ActionClass, ArcRequestContext, IntentKind, IntentManifest
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
    return await seed_arc(factory, slug_prefix="arc-decision")


class _AllVisible:
    async def visible_entity_ids(self, ctx: object, entity_ids: object) -> list[uuid.UUID]:
        return list(entity_ids)  # type: ignore[arg-type]


@pytest.fixture
def envelopes(factory: async_sessionmaker[AsyncSession]) -> AutonomyEnvelopeService:
    return AutonomyEnvelopeService(
        factory,
        authorization=ArcAuthorizationService(visibility=_AllVisible(), global_write_allowlist=()),
        clock=FakeClock(ARC_NOW),
    )


@pytest.fixture
def decisions(factory: async_sessionmaker[AsyncSession], envelopes: AutonomyEnvelopeService) -> AutonomyDecisionService:
    return AutonomyDecisionService(
        factory,
        envelopes=envelopes,
        authorization=ArcAuthorizationService(visibility=_AllVisible(), global_write_allowlist=()),
        clock=FakeClock(ARC_NOW),
    )


def _ctx(seed: ArcSeed) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=["admin"], oidc_subject="operator-1")
    return ArcRequestContext.from_validated_claims(tenant, {"iss": _ISSUER}, host_id="h")


def _manifest(**over: object) -> IntentManifest:
    fields: dict[str, object] = {
        "session_id": "s1",
        "intent_kind": IntentKind.DEPLOYMENT,
        "requested_action_classes": frozenset({ActionClass.DEPLOY}),
    }
    fields.update(over)
    return IntentManifest(**fields)  # type: ignore[arg-type]


async def _add_rule(factory: async_sessionmaker[AsyncSession], seed: ArcSeed, **selectors: object) -> uuid.UUID:
    """One applicability rule on the seeded revision, global-scoped by default."""
    rule_id = uuid.uuid4()
    columns = ["rule_id", "revision_id", "tenant_id", "scope", "effective_from", "is_mandatory"]
    values = [":rule_id", ":revision_id", ":tenant_id", "'global'", ":effective_from", "TRUE"]
    params: dict[str, object] = {
        "rule_id": rule_id,
        "revision_id": seed.revision_id,
        "tenant_id": seed.tenant_id,
        "effective_from": ARC_NOW - datetime.timedelta(days=1),
    }
    for name, value in selectors.items():
        columns.append(name)
        values.append(f":{name}")
        params[name] = value

    async with factory() as session, session.begin():
        await session.execute(
            text(f"INSERT INTO arc_applicability_rules ({', '.join(columns)}) VALUES ({', '.join(values)})"),
            params,
        )
    return rule_id


# --- the property the whole design rests on --------------------------------------


@pytest.mark.asyncio
async def test_a_suspension_refuses_the_very_next_decision(
    decisions: AutonomyDecisionService,
    envelopes: AutonomyEnvelopeService,
    seed: ArcSeed,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The suspension mechanism, in one test.

    The same service instance decides before and after the flip. Nothing is
    restarted, nothing is told, and no time passes -- the clock is fixed. If any
    part of the verdict were cached, the second decision would still permit,
    and there is no invalidation step anywhere for it to have missed.
    """
    ctx = _ctx(seed)
    await _add_rule(factory, seed)
    binding_id = await envelopes.grant(
        ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant")
    )

    before = await decisions.decide(ctx, _manifest(), principal=_AGENT)
    assert before.is_permitted

    await envelopes.suspend(ctx, binding_id, reason="incident 4471")

    after = await decisions.decide(ctx, _manifest(), principal=_AGENT)
    assert after.verdict is EnvelopeVerdict.ENVELOPE_SUSPENDED
    assert not after.is_permitted


@pytest.mark.asyncio
async def test_reinstating_permits_again_with_no_invalidation_step(
    decisions: AutonomyDecisionService,
    envelopes: AutonomyEnvelopeService,
    seed: ArcSeed,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The other direction, which a cache would also get wrong -- and would get
    wrong more quietly, because a stale refusal looks like working governance."""
    ctx = _ctx(seed)
    await _add_rule(factory, seed)
    binding_id = await envelopes.grant(
        ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant")
    )
    await envelopes.suspend(ctx, binding_id, reason="incident")
    assert not (await decisions.decide(ctx, _manifest(), principal=_AGENT)).is_permitted

    await envelopes.reinstate(ctx, binding_id, reason="resolved")

    assert (await decisions.decide(ctx, _manifest(), principal=_AGENT)).is_permitted


@pytest.mark.asyncio
async def test_a_rule_added_after_the_first_refusal_is_seen(
    decisions: AutonomyDecisionService,
    envelopes: AutonomyEnvelopeService,
    seed: ArcSeed,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The rules are the other read, and it is fresh too.

    Worth its own test: the binding could be re-read every time while the matrix
    behind it was cached, and every suspension test would still pass.
    """
    ctx = _ctx(seed)
    await envelopes.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))
    assert (await decisions.decide(ctx, _manifest(), principal=_AGENT)).verdict is EnvelopeVerdict.OUTSIDE_ENVELOPE

    rule_id = await _add_rule(factory, seed)

    decision = await decisions.decide(ctx, _manifest(), principal=_AGENT)
    assert decision.is_permitted
    assert decision.matched_rule_id == rule_id


# --- the verdicts, end to end ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ungoverned_principal_gets_no_envelope(decisions: AutonomyDecisionService, seed: ArcSeed) -> None:
    decision = await decisions.decide(_ctx(seed), _manifest(), principal=_AGENT)

    assert decision.verdict is EnvelopeVerdict.NO_ENVELOPE
    assert decision.binding_id is None


@pytest.mark.asyncio
async def test_revoking_the_bound_revision_withdraws_authority(
    decisions: AutonomyDecisionService,
    envelopes: AutonomyEnvelopeService,
    seed: ArcSeed,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The binding stands and the document behind it does not.

    Ending bindings as a side effect of revoking a revision would be a decision
    taken silently, so the binding is left alone -- which makes this the check
    that stops a withdrawn governance document from still authorising.
    """
    ctx = _ctx(seed)
    await _add_rule(factory, seed)
    await envelopes.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))
    assert (await decisions.decide(ctx, _manifest(), principal=_AGENT)).is_permitted

    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET lifecycle_state = 'revoked', revoked_at = :now " "WHERE revision_id = :rid"),
            {"rid": seed.revision_id, "now": ARC_NOW},
        )

    decision = await decisions.decide(ctx, _manifest(), principal=_AGENT)
    assert decision.verdict is EnvelopeVerdict.ENVELOPE_WITHDRAWN
    assert decision.binding_id is not None, "the binding still exists and is still named"


@pytest.mark.asyncio
async def test_a_narrow_rule_refuses_an_act_outside_it(
    decisions: AutonomyDecisionService,
    envelopes: AutonomyEnvelopeService,
    seed: ArcSeed,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deny by default, with the matrix doing the deciding."""
    ctx = _ctx(seed)
    await _add_rule(factory, seed, intent_kinds=["read_only"])
    await envelopes.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))

    assert (
        await decisions.decide(ctx, _manifest(intent_kind=IntentKind.DEPLOYMENT), principal=_AGENT)
    ).verdict is EnvelopeVerdict.OUTSIDE_ENVELOPE
    assert (await decisions.decide(ctx, _manifest(intent_kind=IntentKind.READ_ONLY), principal=_AGENT)).is_permitted


@pytest.mark.asyncio
async def test_the_requester_is_the_default_principal(
    decisions: AutonomyDecisionService,
    envelopes: AutonomyEnvelopeService,
    seed: ArcSeed,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An agent asking whether *it* may act is the case that matters, so it takes
    no argument. Naming another principal is the operator-preview path."""
    ctx = _ctx(seed)
    await _add_rule(factory, seed)
    requester = WorkloadIdentity.of_requester(ctx)
    await envelopes.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=requester, reason="grant"))

    decision = await decisions.decide(ctx, _manifest())

    assert decision.is_permitted
    assert decision.principal == requester


@pytest.mark.asyncio
async def test_another_tenants_envelope_does_not_decide_this_tenants_request(
    decisions: AutonomyDecisionService,
    envelopes: AutonomyEnvelopeService,
    seed: ArcSeed,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same workload identity in another tenant is another principal."""
    ctx = _ctx(seed)
    await _add_rule(factory, seed)
    await envelopes.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))

    other = await seed_arc(factory, slug_prefix="arc-decision-other")
    assert (await decisions.decide(_ctx(other), _manifest(), principal=_AGENT)).verdict is (EnvelopeVerdict.NO_ENVELOPE)
