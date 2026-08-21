"""The advisory stage: nothing is refused, and every refusal is written down.

The stage exists because landing "no envelope, no authority" as specified would
refuse every agent in every deployment on the day it shipped. So the tests that
matter here are the two halves of that bargain -- that an advisory tenant is
never blocked, and that the evidence a graduation scan will need is actually
there afterwards.

The recorded rows are not telemetry. Their shape is decided by the query that
reads them: which principals acted in this window with no envelope at all. So
`no_envelope` staying distinct from the other three refusals is asserted here
rather than left to the enum, because collapsing them would make that scan
unbuildable and nothing else would notice.
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
from contextplane.arc.service.autonomy_enforcement import (
    AutonomyEnforcementService,
    EnforcementStage,
    stage_of,
)
from contextplane.arc.service.autonomy_envelope import AutonomyEnvelopeService, EnvelopeGrant, WorkloadIdentity
from contextplane.arc.types import ActionClass, ArcRequestContext, IntentKind, IntentManifest
from contextplane.audit import actions
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
    return await seed_arc(factory, slug_prefix="arc-enforcement")


class _AllVisible:
    async def visible_entity_ids(self, ctx: object, entity_ids: object) -> list[uuid.UUID]:
        return list(entity_ids)  # type: ignore[arg-type]


def _authz() -> ArcAuthorizationService:
    return ArcAuthorizationService(visibility=_AllVisible(), global_write_allowlist=())


@pytest.fixture
def envelopes(factory: async_sessionmaker[AsyncSession]) -> AutonomyEnvelopeService:
    return AutonomyEnvelopeService(factory, authorization=_authz(), clock=FakeClock(ARC_NOW))


@pytest.fixture
def enforcement(
    factory: async_sessionmaker[AsyncSession], envelopes: AutonomyEnvelopeService
) -> AutonomyEnforcementService:
    decisions = AutonomyDecisionService(factory, envelopes=envelopes, authorization=_authz(), clock=FakeClock(ARC_NOW))
    return AutonomyEnforcementService(factory, decisions=decisions)


def _ctx(seed: ArcSeed) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=["admin"], oidc_subject="operator-1")
    return ArcRequestContext.from_validated_claims(tenant, {"iss": _ISSUER}, host_id="h")


def _manifest(**over: object) -> IntentManifest:
    fields: dict[str, object] = {
        "session_id": "session-1",
        "intent_kind": IntentKind.DEPLOYMENT,
        "requested_action_classes": frozenset({ActionClass.DEPLOY}),
    }
    fields.update(over)
    return IntentManifest(**fields)  # type: ignore[arg-type]


async def _graduate(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE tenants SET envelope_enforcement_stage = 'enforcing' WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )


async def _add_rule(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> uuid.UUID:
    rule_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_applicability_rules "
                "(rule_id, revision_id, tenant_id, scope, effective_from, is_mandatory) "
                "VALUES (:rid, :rev, :tid, 'global', :efrom, TRUE)"
            ),
            {
                "rid": rule_id,
                "rev": seed.revision_id,
                "tid": seed.tenant_id,
                "efrom": ARC_NOW - datetime.timedelta(days=1),
            },
        )
    return rule_id


async def _audit_events(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> list[str]:
    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT event_type FROM arc_audit_outbox WHERE tenant_id = :tid ORDER BY created_at"),
                {"tid": tenant_id},
            )
        ).all()
    return [row[0] for row in rows]


async def _records(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> list[dict[str, object]]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT verdict, principal_issuer, principal_subject, binding_id, revision_id, "
                    "       intent_kind, session_id, decided_at "
                    "FROM arc_envelope_advisory_records WHERE tenant_id = :tid ORDER BY decided_at"
                ),
                {"tid": tenant_id},
            )
        ).mappings()
    return [dict(r) for r in rows]


# --- the bargain -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_advisory_tenant_is_never_blocked(
    enforcement: AutonomyEnforcementService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The half that keeps the rollout from breaking every deployment on day one.

    This principal has no envelope at all, which under enforcement is the
    hardest refusal there is.
    """
    outcome = await enforcement.evaluate(_ctx(seed), _manifest(), principal=_AGENT)

    assert outcome.stage is EnforcementStage.ADVISORY
    assert not outcome.blocked
    assert outcome.decision.verdict is EnvelopeVerdict.NO_ENVELOPE
    assert outcome.would_have_been_blocked, "the caller proceeds and the record says it should not have"
    assert outcome.recorded
    assert await _audit_events(factory, seed.tenant_id) == [actions.ARC_ENVELOPE_AUTHORITY_ADVISORY]


@pytest.mark.asyncio
async def test_the_record_carries_what_the_graduation_scan_asks_for(
    enforcement: AutonomyEnforcementService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Its shape is decided by the query that reads it, not by what is easy to log."""
    await enforcement.evaluate(_ctx(seed), _manifest(session_id="s-42"), principal=_AGENT)

    [row] = await _records(factory, seed.tenant_id)
    assert row["verdict"] == "no_envelope"
    assert row["principal_issuer"] == _AGENT.issuer
    assert row["principal_subject"] == _AGENT.subject
    assert row["intent_kind"] == str(IntentKind.DEPLOYMENT)
    assert row["session_id"] == "s-42"
    assert row["decided_at"] == ARC_NOW
    assert row["binding_id"] is None, "no_envelope means there is no binding to name"


@pytest.mark.asyncio
async def test_an_enforcing_tenant_blocks_and_records_nothing(
    enforcement: AutonomyEnforcementService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Once the refusal is real there is nothing left for a record to answer."""
    await _graduate(factory, seed.tenant_id)

    outcome = await enforcement.evaluate(_ctx(seed), _manifest(), principal=_AGENT)

    assert outcome.stage is EnforcementStage.ENFORCING
    assert outcome.blocked
    assert not outcome.recorded
    assert await _records(factory, seed.tenant_id) == []
    assert await _audit_events(factory, seed.tenant_id) == [
        actions.ARC_ENVELOPE_AUTHORITY_REFUSED
    ], "the trail is written even when the scan row is not -- a refusal that refused is still auditable"


@pytest.mark.asyncio
async def test_a_permitted_act_records_nothing_in_either_stage(
    enforcement: AutonomyEnforcementService,
    envelopes: AutonomyEnvelopeService,
    seed: ArcSeed,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A principal acting inside its envelope is not an offender, so it must not
    leave a row for the scan to count."""
    ctx = _ctx(seed)
    await _add_rule(factory, seed)
    await envelopes.grant(ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant"))

    advisory = await enforcement.evaluate(ctx, _manifest(), principal=_AGENT)
    assert not advisory.blocked
    assert not advisory.recorded
    assert not advisory.would_have_been_blocked

    await _graduate(factory, seed.tenant_id)
    enforcing = await enforcement.evaluate(ctx, _manifest(), principal=_AGENT)
    assert not enforcing.blocked

    assert await _records(factory, seed.tenant_id) == []


# --- the distinctions the scan depends on -------------------------------------------


@pytest.mark.asyncio
async def test_the_four_refusals_are_recorded_apart(
    enforcement: AutonomyEnforcementService,
    envelopes: AutonomyEnvelopeService,
    seed: ArcSeed,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A principal with no envelope is an incomplete rollout; a principal acting
    outside a real one is a governance finding. Only the first blocks graduation,
    so the two cannot be one value."""
    ctx = _ctx(seed)

    # no_envelope
    await enforcement.evaluate(ctx, _manifest(), principal=_AGENT)

    # outside_envelope: bound, in force, and no rule covers the act.
    binding_id = await envelopes.grant(
        ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant")
    )
    await enforcement.evaluate(ctx, _manifest(), principal=_AGENT)

    # envelope_suspended
    await envelopes.suspend(ctx, binding_id, reason="incident")
    await enforcement.evaluate(ctx, _manifest(), principal=_AGENT)

    verdicts = [row["verdict"] for row in await _records(factory, seed.tenant_id)]
    assert set(verdicts) == {"no_envelope", "outside_envelope", "envelope_suspended"}


@pytest.mark.asyncio
async def test_a_bound_refusal_names_the_envelope_that_refused(
    enforcement: AutonomyEnforcementService,
    envelopes: AutonomyEnvelopeService,
    seed: ArcSeed,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An operator reading a refusal wants the envelope more than the fact."""
    ctx = _ctx(seed)
    binding_id = await envelopes.grant(
        ctx, EnvelopeGrant(revision_id=seed.revision_id, principal=_AGENT, reason="grant")
    )

    await enforcement.evaluate(ctx, _manifest(), principal=_AGENT)

    [row] = await _records(factory, seed.tenant_id)
    assert row["verdict"] == "outside_envelope"
    assert row["binding_id"] == binding_id
    assert row["revision_id"] == seed.revision_id


@pytest.mark.asyncio
async def test_the_database_refuses_a_record_that_contradicts_itself(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """`no_envelope` with a binding, or a bound verdict without one, is a row the
    scan would have to guess about. The CHECK is what stops a future writer from
    producing one."""
    from sqlalchemy.exc import IntegrityError

    for verdict, binding_id in (("no_envelope", uuid.uuid4()), ("outside_envelope", None)):
        with pytest.raises(IntegrityError):
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO arc_envelope_advisory_records ("
                        "  record_id, tenant_id, principal_issuer, principal_subject, verdict,"
                        "  binding_id, intent_kind, session_id, decided_at"
                        ") VALUES (:rec, :tid, 'i', 's', :verdict, :bid, 'deployment', 's1', :now)"
                    ),
                    {
                        "rec": uuid.uuid4(),
                        "tid": seed.tenant_id,
                        "verdict": verdict,
                        "bid": binding_id,
                        "now": ARC_NOW,
                    },
                )


@pytest.mark.asyncio
async def test_a_permit_is_not_a_storable_verdict(seed: ArcSeed, factory: async_sessionmaker[AsyncSession]) -> None:
    """The table holds refusals. Admitting `permitted` in the CHECK would describe
    a state it cannot hold and invite a future writer to fill it."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO arc_envelope_advisory_records ("
                    "  record_id, tenant_id, principal_issuer, principal_subject, verdict,"
                    "  binding_id, intent_kind, session_id, decided_at"
                    ") VALUES (:rec, :tid, 'i', 's', 'permitted', :bid, 'deployment', 's1', :now)"
                ),
                {"rec": uuid.uuid4(), "tid": seed.tenant_id, "bid": uuid.uuid4(), "now": ARC_NOW},
            )


# --- the stage itself ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_tenant_starts_advisory(seed: ArcSeed, factory: async_sessionmaker[AsyncSession]) -> None:
    """The one place in this service where the safe default is the permissive one:
    `enforcing` on an unmigrated tenant refuses every agent it runs."""
    async with factory() as session:
        stage = (
            await session.execute(
                text("SELECT envelope_enforcement_stage FROM tenants WHERE tenant_id = :tid"),
                {"tid": seed.tenant_id},
            )
        ).scalar_one()

    assert stage == "advisory"


@pytest.mark.asyncio
async def test_graduating_takes_effect_at_the_next_decision(
    enforcement: AutonomyEnforcementService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The stage is read per decision for the same reason the envelope is: no
    cache, so both graduating and demoting land immediately."""
    ctx = _ctx(seed)
    assert not (await enforcement.evaluate(ctx, _manifest(), principal=_AGENT)).blocked

    await _graduate(factory, seed.tenant_id)

    assert (await enforcement.evaluate(ctx, _manifest(), principal=_AGENT)).blocked


def test_an_absent_stage_reads_as_advisory() -> None:
    """A tenant deleted mid-request must not turn a race into an outage."""
    assert stage_of(None) is EnforcementStage.ADVISORY
    assert stage_of("enforcing") is EnforcementStage.ENFORCING


def test_an_unparseable_stage_raises_rather_than_defaulting() -> None:
    """A CHECK already closes this vocabulary, so an unknown value means the
    constraint and the enum have drifted. Defaulting a tenant into the permissive
    stage on that would be the worst possible reading of a typo."""
    with pytest.raises(ValueError, match="disabled"):
        stage_of("disabled")
