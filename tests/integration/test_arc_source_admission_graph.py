"""Integration tests for graph-promotion source admission, against a real Postgres.

What the unit suite (`tests/unit/test_arc_source_admission_graph.py`) cannot
prove with monkeypatched queries: that the three SELECTs name columns that
exist and join tables that relate, that migration 0056's widened CHECK
constraints actually accept a `graph_promoted` row with both proof columns
NULL, and -- the point of the whole authority -- that a claim nobody promoted
is unreachable through the real join rather than merely filtered by service
code that a later refactor could drop.

The last of those is asserted twice on purpose: once through the service's
refusal, and once by reinstating the pre-0056 representation constraint and
watching the database itself reject a `graph_promoted` row that carries a
signature. A service-layer check and a schema-layer check fail differently,
and evidence rows outlive whichever service wrote them.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.source_admission import SourceAdmissionRefused, SourceAdmissionService
from contextplane.arc.service.source_admission_graph import (
    GraphPromotionAdmission,
    GraphPromotionAdmissionService,
)
from contextplane.arc.types import ArcRequestContext
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock
from tests.helpers.seeding import seed_shared_entity, seed_tenant_and_actor

_ISSUER = "https://idp.example.test"
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_PROMOTED_AT = datetime.datetime(2025, 12, 1, 9, 30, tzinfo=datetime.UTC)
_REVIEW = datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC)


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"], oidc_subject="operator")
    return ArcRequestContext.from_validated_claims(tenant, {"iss": _ISSUER})


def _service(factory: async_sessionmaker[AsyncSession]) -> GraphPromotionAdmissionService:
    authorization = ArcAuthorizationService(visibility=_AllowAll())
    clock = FakeClock(_NOW)
    return GraphPromotionAdmissionService(
        factory,
        admission=SourceAdmissionService(factory, authorization=authorization, clock=clock),
        authorization=authorization,
        clock=clock,
    )


async def _second_actor(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> uuid.UUID:
    """A promoter who is not the claim's author -- the separation this authority rests on."""
    actor_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'Reviewer', :oidc, :now)"
            ),
            {"aid": actor_id, "tid": tenant_id, "oidc": f"reviewer-{actor_id.hex[:8]}", "now": _NOW},
        )
    return actor_id


async def _seed_promoted_claim(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    author_actor_id: uuid.UUID,
    promoted_by: uuid.UUID,
    entity_id: uuid.UUID,
    evidence_kind: str = "commit",
    reversed_at: datetime.datetime | None = None,
    promote: bool = True,
) -> uuid.UUID:
    """Stage a claim, give it commit provenance, and (optionally) promote it."""
    claim_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO memory_claims ("
                " claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                " subject_reference, predicate, value_type, claim_category, value_jsonb,"
                " asserted_valid_from, status, visibility, source_authority, size_bytes, created_at,"
                " confidence, confidence_scored_at, confidence_inputs, scorer_version,"
                " calibration_version, decay_half_life_days"
                ") VALUES ("
                " :claim, :tenant, :tenant, :author, :entity, :subject, 'governs_deployment', 'prose',"
                " 'operational_lifecycle', CAST(:value AS JSONB), :now, 'staged', 'tenant-shared',"
                " 'owner_human', 64, :now,"
                " 0.900, :now, CAST(:inputs AS JSONB), 'scorer.v1', 'calib.v1', 30"
                ")"
            ),
            {
                "claim": claim_id,
                "tenant": tenant_id,
                "author": author_actor_id,
                "entity": entity_id,
                "subject": f"adr-{claim_id.hex[:6]}",
                "value": json.dumps("production deploys reference an approved change ticket"),
                "inputs": json.dumps({"graph_promotion_test": True}),
                "now": _NOW,
            },
        )
        await session.execute(
            text(
                "INSERT INTO memory_claim_provenance ("
                " claim_id, evidence_kind, evidence_ref, evidence_excerpt, recorded_at, derivation"
                ") VALUES (:claim, :kind, :ref, :excerpt, :now, 'human')"
            ),
            {
                "claim": claim_id,
                "kind": evidence_kind,
                "ref": "bitbucket.org/acme/adr@9f3c1ad",
                "excerpt": "All production deploys must reference an approved change ticket.",
                "now": _NOW,
            },
        )
        if not promote:
            return claim_id

        proposal_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO memory_promotion_proposal ("
                " proposal_id, claim_id, owner_tenant_id, author_tenant_id, subject_entity_id,"
                " predicate, target_kind, target_key, mapping_version, proposed_value, valid_from,"
                " state, decided_by, decided_at, created_at"
                ") VALUES ("
                " :proposal, :claim, :tenant, :tenant, :entity, 'governs_deployment', 'attribute',"
                " 'governs_deployment', 1, CAST(:value AS JSONB), :now, 'accepted', :promoter, :now, :now"
                ")"
            ),
            {
                "proposal": proposal_id,
                "claim": claim_id,
                "tenant": tenant_id,
                "entity": entity_id,
                "value": json.dumps("production deploys reference an approved change ticket"),
                "promoter": promoted_by,
                "now": _NOW,
            },
        )
        await session.execute(
            text(
                "INSERT INTO memory_promotion_journal ("
                " promotion_id, proposal_id, claim_id, tenant_id, target_kind, created_row_id,"
                " promoted_at, promoted_by, reversed_at, reversed_by, reversal_reason"
                ") VALUES ("
                " :promotion, :proposal, :claim, :tenant, 'attribute', :row, :promoted_at, :promoter,"
                " :reversed_at, :reversed_by, :reason"
                ")"
            ),
            {
                "promotion": uuid.uuid4(),
                "proposal": proposal_id,
                "claim": claim_id,
                "tenant": tenant_id,
                "row": uuid.uuid4(),
                "promoted_at": _PROMOTED_AT,
                "promoter": promoted_by,
                "reversed_at": reversed_at,
                "reversed_by": promoted_by if reversed_at else None,
                "reason": "superseded" if reversed_at else None,
            },
        )
    return claim_id


def _request(claim_id: uuid.UUID) -> GraphPromotionAdmission:
    return GraphPromotionAdmission(
        claim_id=claim_id,
        source_system="bitbucket.org/acme/adr",
        review_expires_at=_REVIEW,
        idempotency_key=f"key-{uuid.uuid4().hex[:8]}",
    )


@pytest.mark.asyncio
async def test_promoted_claim_is_admitted_and_the_body_round_trips(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    tenant_id, author_id = await seed_tenant_and_actor(pg_container, slug=f"gp-{uuid.uuid4().hex[:8]}")
    promoter_id = await _second_actor(factory, tenant_id)
    entity_id = await seed_shared_entity(factory, tenant_id)
    claim_id = await _seed_promoted_claim(
        factory,
        tenant_id=tenant_id,
        author_actor_id=author_id,
        promoted_by=promoter_id,
        entity_id=entity_id,
    )

    service = _service(factory)
    ctx = _ctx(tenant_id, author_id)
    evidence = await service.admit_promoted_claim(ctx, _request(claim_id))

    assert evidence.admission_method == "graph_promotion"
    assert evidence.verification_method == "graph_promotion"
    assert evidence.source_revision_locator == "commit:bitbucket.org/acme/adr@9f3c1ad"
    assert evidence.status == "current"

    # Migration 0056's representation rule permits -- and this row proves --
    # a verification method that carries neither proof column.
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT verification_method, admission_method, signature, verifier_attestation,"
                    "       connector_id, policy_id, verifier_id, claim "
                    "FROM arc_source_approval_evidence WHERE source_evidence_id = :sid"
                ),
                {"sid": evidence.source_evidence_id},
            )
        ).one()
    assert row.verification_method == "graph_promoted"
    assert row.admission_method == "graph_promotion"
    assert row.signature is None
    assert row.verifier_attestation is None
    assert row.connector_id is None
    assert row.policy_id is None
    assert row.verifier_id.startswith("promotion:")
    # The approving authority is the promoter, not the admitting caller.
    assert row.claim["approving_authority_subject"] != "operator"

    body, content_type = await service._admission.get_body(ctx, evidence.source_evidence_id)
    assert content_type == "application/vnd.contextplane.graph-promotion+json"
    projection = json.loads(body)
    assert projection["claim"]["predicate"] == "governs_deployment"
    assert projection["evidence"][0]["ref"] == "bitbucket.org/acme/adr@9f3c1ad"


@pytest.mark.asyncio
async def test_an_unpromoted_claim_is_unreachable_through_the_join(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    tenant_id, author_id = await seed_tenant_and_actor(pg_container, slug=f"gp-{uuid.uuid4().hex[:8]}")
    promoter_id = await _second_actor(factory, tenant_id)
    entity_id = await seed_shared_entity(factory, tenant_id)
    claim_id = await _seed_promoted_claim(
        factory,
        tenant_id=tenant_id,
        author_actor_id=author_id,
        promoted_by=promoter_id,
        entity_id=entity_id,
        promote=False,
    )

    service = _service(factory)
    with pytest.raises(SourceAdmissionRefused, match="not a promoted claim"):
        await service.admit_promoted_claim(_ctx(tenant_id, author_id), _request(claim_id))


@pytest.mark.asyncio
async def test_a_reversed_promotion_is_refused(factory: async_sessionmaker[AsyncSession], pg_container: str) -> None:
    tenant_id, author_id = await seed_tenant_and_actor(pg_container, slug=f"gp-{uuid.uuid4().hex[:8]}")
    promoter_id = await _second_actor(factory, tenant_id)
    entity_id = await seed_shared_entity(factory, tenant_id)
    claim_id = await _seed_promoted_claim(
        factory,
        tenant_id=tenant_id,
        author_actor_id=author_id,
        promoted_by=promoter_id,
        entity_id=entity_id,
        reversed_at=datetime.datetime(2025, 12, 20, tzinfo=datetime.UTC),
    )

    service = _service(factory)
    with pytest.raises(SourceAdmissionRefused, match="was reversed"):
        await service.admit_promoted_claim(_ctx(tenant_id, author_id), _request(claim_id))


@pytest.mark.asyncio
async def test_a_self_promotion_is_refused(factory: async_sessionmaker[AsyncSession], pg_container: str) -> None:
    tenant_id, author_id = await seed_tenant_and_actor(pg_container, slug=f"gp-{uuid.uuid4().hex[:8]}")
    entity_id = await seed_shared_entity(factory, tenant_id)
    claim_id = await _seed_promoted_claim(
        factory,
        tenant_id=tenant_id,
        author_actor_id=author_id,
        promoted_by=author_id,
        entity_id=entity_id,
    )

    service = _service(factory)
    with pytest.raises(SourceAdmissionRefused, match="second actor"):
        await service.admit_promoted_claim(_ctx(tenant_id, author_id), _request(claim_id))


@pytest.mark.asyncio
async def test_another_tenants_promoted_claim_is_not_visible(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    owner_tenant, owner_actor = await seed_tenant_and_actor(pg_container, slug=f"gp-{uuid.uuid4().hex[:8]}")
    other_tenant, other_actor = await seed_tenant_and_actor(pg_container, slug=f"gp-{uuid.uuid4().hex[:8]}")
    promoter_id = await _second_actor(factory, owner_tenant)
    entity_id = await seed_shared_entity(factory, owner_tenant)
    claim_id = await _seed_promoted_claim(
        factory,
        tenant_id=owner_tenant,
        author_actor_id=owner_actor,
        promoted_by=promoter_id,
        entity_id=entity_id,
    )

    service = _service(factory)
    with pytest.raises(SourceAdmissionRefused, match="not a promoted claim"):
        await service.admit_promoted_claim(_ctx(other_tenant, other_actor), _request(claim_id))


@pytest.mark.asyncio
async def test_evidence_with_no_revision_locator_is_refused(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    tenant_id, author_id = await seed_tenant_and_actor(pg_container, slug=f"gp-{uuid.uuid4().hex[:8]}")
    promoter_id = await _second_actor(factory, tenant_id)
    entity_id = await seed_shared_entity(factory, tenant_id)
    claim_id = await _seed_promoted_claim(
        factory,
        tenant_id=tenant_id,
        author_actor_id=author_id,
        promoted_by=promoter_id,
        entity_id=entity_id,
        evidence_kind="session_event",
    )

    service = _service(factory)
    with pytest.raises(SourceAdmissionRefused, match="source revision locator"):
        await service.admit_promoted_claim(_ctx(tenant_id, author_id), _request(claim_id))


@pytest.mark.asyncio
async def test_an_exact_retry_returns_the_first_evidence(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    tenant_id, author_id = await seed_tenant_and_actor(pg_container, slug=f"gp-{uuid.uuid4().hex[:8]}")
    promoter_id = await _second_actor(factory, tenant_id)
    entity_id = await seed_shared_entity(factory, tenant_id)
    claim_id = await _seed_promoted_claim(
        factory,
        tenant_id=tenant_id,
        author_actor_id=author_id,
        promoted_by=promoter_id,
        entity_id=entity_id,
    )

    service = _service(factory)
    ctx = _ctx(tenant_id, author_id)
    request = _request(claim_id)

    first = await service.admit_promoted_claim(ctx, request)
    # Byte-stable projection plus an unchanged promotion means the payload
    # digest matches, so this resolves to the first row rather than
    # conflicting -- the property the canonical JSON exists to provide.
    second = await service.admit_promoted_claim(ctx, request)
    assert first.source_evidence_id == second.source_evidence_id


@pytest.mark.asyncio
async def test_the_schema_rejects_a_graph_promotion_carrying_a_signature(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The representation rule is enforced by the database, not only the service.

    Written as a direct UPDATE because no service path can produce this row:
    the point is that the constraint would stop one if a future path tried.
    """
    tenant_id, author_id = await seed_tenant_and_actor(pg_container, slug=f"gp-{uuid.uuid4().hex[:8]}")
    promoter_id = await _second_actor(factory, tenant_id)
    entity_id = await seed_shared_entity(factory, tenant_id)
    claim_id = await _seed_promoted_claim(
        factory,
        tenant_id=tenant_id,
        author_actor_id=author_id,
        promoted_by=promoter_id,
        entity_id=entity_id,
    )
    evidence = await _service(factory).admit_promoted_claim(_ctx(tenant_id, author_id), _request(claim_id))

    with pytest.raises(Exception, match="ck_arc_source_evidence_representation"):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE arc_source_approval_evidence SET signature = 'c2ln' " "WHERE source_evidence_id = :sid"),
                {"sid": evidence.source_evidence_id},
            )
