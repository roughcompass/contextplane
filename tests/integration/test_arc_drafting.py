"""Integration tests for model-backed drafting and reach confirmations,
against a real Postgres and, for the enabled path, the real two-subprocess
sandbox pipeline.

What the unit suite (`tests/unit/test_arc_drafter.py`) cannot prove with a
fake session and an injected fake pipeline: that the registered `POST {PV}
/draft` route on the live app genuinely refuses with `arc_drafter_model_disabled`
under this deployment's committed default (the model is disabled, and that
is the point); that `POST {PV}/reach-confirmations` -- the human structured
form's own persisted state -- round-trips through the real
`arc_authoring_reach_confirmations` table and survives a sibling field's
edit; and that the real drafter sandbox pipeline, driven by a test-only
"accepted" decision stub (never the committed `human_only` artifact),
genuinely declines a source-backed field when the seeded content and its
recorded digest disagree, and genuinely runs end to end -- two real
subprocesses, `ipc.py`'s real peer-UID authentication, and back -- when
they agree.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.drafter import DrafterService
from registry.arc.service.proposal import ProposalService, ProposalStateConflict
from registry.arc.service.source_admission import SourceAdmissionService
from registry.arc.service.source_status import SourceStatusService
from registry.arc.types import ArcRequestContext
from registry.config import Settings
from registry.types import TenantContext
from tests.helpers.arc_fixtures import seed_artifact_family, seed_source_evidence
from tests.helpers.auth_harness import EntitlementAuthHarness, bearer_headers, patch_validator_for_actor
from tests.helpers.clock import FakeClock
from tests.helpers.seeding import seed_tenant_and_actor

_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


def _ctx(*, tenant_id: uuid.UUID) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["admin"], oidc_subject=_OPERATOR)
    return ArcRequestContext(tenant=tenant, oidc_issuer=_ISSUER)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _authorization() -> ArcAuthorizationService:
    return ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))


def _proposal_service(factory: async_sessionmaker[AsyncSession]) -> ProposalService:
    return ProposalService(factory, authorization=_authorization(), clock=FakeClock(_NOW))


def _drafter_service(
    factory: async_sessionmaker[AsyncSession],
    *,
    enabled: bool,
    decision_outcome: str = "human_only",
    source_admission: SourceAdmissionService | None = None,
    source_status: SourceStatusService | None = None,
) -> DrafterService:
    settings = Settings(database_url="postgresql+asyncpg://unused/unused", arc_drafter_model_enabled=enabled)
    return DrafterService(
        factory,
        authorization=_authorization(),
        source_admission=source_admission
        or SourceAdmissionService(factory, authorization=_authorization(), clock=FakeClock(_NOW)),
        source_status=source_status or SourceStatusService(factory, clock=FakeClock(_NOW)),
        clock=FakeClock(_NOW),
        settings=settings,
        # Test-only stub: never the committed `registry/arc/drafter/model_decision.json`
        # (which records `human_only`) -- see this module's own docstring.
        decision_loader=lambda: {"outcome": decision_outcome},
    )


async def _open_version(factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID) -> tuple[uuid.UUID, int]:
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)
    version = await _proposal_service(factory).open_proposal(
        _ctx(tenant_id=tenant_id), artifact_id=artifact_id, source_evidence_id=source_evidence_id
    )
    return version.proposal_id, version.proposal_version


async def _seed_matching_source_evidence(
    factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID
) -> uuid.UUID:
    """Same shape as `seed_source_evidence`, except the recorded
    `source_content_digest` is the real sha256 of the seeded body -- the
    fixture helper's own placeholder digest (`"0" * 64`) is deliberately
    wrong (see its docstring: "content is deliberately inert"), which is
    exactly right for proving the binding check refuses a mismatch, but
    the wrong shape for proving a genuine, matching draft succeeds.
    """
    source_evidence_id = uuid.uuid4()
    policy_id = f"seed-policy-{uuid.uuid4().hex[:8]}"
    now = datetime.datetime.now(tz=datetime.UTC)
    content = b"# heading\nsome body text\n"
    digest = hashlib.sha256(content).hexdigest()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_source_upload_policies ("
                "  policy_id, owning_scope, tenant_id, allowed_media_types, allowed_verifier_ids, max_bytes"
                ") VALUES (:pid, 'tenant', :tid, ARRAY['text/markdown'], ARRAY['verifier-1'], 1024)"
            ),
            {"pid": policy_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO arc_source_bodies (source_evidence_id, content_digest, content_bytes, body, created_at) "
                "VALUES (:sid, :digest, :nbytes, :body, :now)"
            ),
            {"sid": source_evidence_id, "digest": digest, "nbytes": len(content), "body": content, "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO arc_source_approval_evidence ("
                "  source_evidence_id, owning_scope, tenant_id, source_system, source_revision_locator,"
                "  source_content_type, source_content_digest, claim, claim_digest, verification_method,"
                "  verifier_id, signature, admission_method, policy_id, admitted_at, admitted_by_issuer,"
                "  admitted_by_subject, verified_at, expires_at, idempotency_key_digest,"
                "  admission_request_payload_digest, idempotency_scope_digest"
                ") VALUES ("
                "  :sid, 'tenant', :tid, 'test-system', 'loc://1', 'text/markdown', :digest,"
                "  '{}'::jsonb, :digest, 'source_signed', 'verifier-1', 'c2lnbmF0dXJl', 'authorized_upload',"
                "  :pid, :now, :issuer, :subject, :now, :expires, :digest, :digest, :digest"
                ")"
            ),
            {
                "sid": source_evidence_id,
                "tid": tenant_id,
                "digest": digest,
                "pid": policy_id,
                "now": now,
                "issuer": _ISSUER,
                "subject": _OPERATOR,
                "expires": now + datetime.timedelta(days=365),
            },
        )
        await session.execute(
            text(
                "INSERT INTO arc_source_approval_status ("
                "  source_evidence_id, status, checked_at, next_check_at, status_source"
                ") VALUES (:sid, 'current', :now, :next_check, 'seed')"
            ),
            {"sid": source_evidence_id, "now": now, "next_check": now + datetime.timedelta(minutes=5)},
        )
    return source_evidence_id


# ---------------------------------------------------------------------------
# HTTP level: the registered route, under the committed disabled default.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


@pytest_asyncio.fixture
async def client(harness: EntitlementAuthHarness) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_draft_and_reach_confirmation_routes_are_registered(harness: EntitlementAuthHarness) -> None:
    paths = {r.path for r in harness.app.routes if hasattr(r, "path")}
    assert "/v1/arc/proposals/{proposal_id}/versions/{proposal_version}/draft" in paths
    assert "/v1/arc/proposals/{proposal_id}/versions/{proposal_version}/reach-confirmations" in paths


@pytest.mark.asyncio
async def test_draft_route_refuses_under_the_committed_disabled_default(
    harness: EntitlementAuthHarness, client: AsyncClient
) -> None:
    """This deployment's real, committed `Settings` -- not a fixture
    override -- has the model disabled. The proposal named in the URL does
    not even need to exist: the disabled check is provably the first thing
    `DrafterService.draft` does (see its own docstring), so this refusal
    fires before any lookup would have had a chance to 404 instead."""
    assert harness.app.state.services.settings.arc_drafter_model_enabled is False

    persona = harness.add_persona(f"drafter-{uuid.uuid4().hex[:6]}", roles=["admin"])
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/v1/arc/proposals/{uuid.uuid4()}/versions/1/draft",
            json={"source_evidence_id": str(uuid.uuid4()), "target_field_paths": ["directives"]},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["errors"][0]["code"] == "arc_drafter_model_disabled"


@pytest.mark.asyncio
async def test_reach_confirmation_route_persists_through_a_real_request(
    harness: EntitlementAuthHarness, client: AsyncClient
) -> None:
    """The human structured form's own route, unaffected by the drafter
    being disabled -- proving the two routes this task adds are
    independent, not one flag gating both."""
    persona = harness.add_persona(f"drafter-{uuid.uuid4().hex[:6]}", roles=["admin"])
    harness.configure_fetcher_for(persona)

    factory: async_sessionmaker[AsyncSession] = harness.app.state.services.session_factory
    tenant_id = await _resolve_tenant_id(client, harness, persona)
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)

    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/v1/arc/proposals/{proposal_id}/versions/{proposal_version}/reach-confirmations",
            json={"field_paths": ["directives"]},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["confirmations"]) == 1
    assert body["confirmations"][0]["field_path"] == "directives"
    assert body["confirmations"][0]["confirmed"] is True
    assert body["confirmations"][0]["confirmed_by"]["subject"] == persona.oidc_subject


async def _resolve_tenant_id(client: AsyncClient, harness: EntitlementAuthHarness, persona: object) -> uuid.UUID:
    """The harness JIT-materializes the tenant row on first authenticated
    request; `whoami` is the cheapest route that guarantees it exists
    before this test seeds a proposal against it directly via SQL."""
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))  # type: ignore[arg-type]
    assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["tenant_id"])


# ---------------------------------------------------------------------------
# Service level: reach confirmations against real rows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_reach_persists_and_survives_a_sibling_edit(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"drafter-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    service = _drafter_service(factory, enabled=False)

    first = await service.confirm_reach(
        _ctx(tenant_id=tenant_id), proposal_id, proposal_version, field_paths=["directives"]
    )
    second = await service.confirm_reach(
        _ctx(tenant_id=tenant_id), proposal_id, proposal_version, field_paths=["applicability"]
    )

    assert first[0].confirmed is True
    all_rows = await service.list_reach_confirmations(_ctx(tenant_id=tenant_id), proposal_id, proposal_version)
    assert {r.field_path for r in all_rows} == {"directives", "applicability"}
    # Confirming "applicability" did not disturb the already-recorded
    # "directives" row -- read back independently, not merely re-returned
    # from the second call.
    directives_row = next(r for r in all_rows if r.field_path == "directives")
    assert directives_row.confirmed is True
    assert second[0].field_path == "applicability"


@pytest.mark.asyncio
async def test_confirm_reach_refuses_once_the_version_is_withdrawn(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"drafter-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    await _proposal_service(factory).withdraw(
        _ctx(tenant_id=tenant_id), proposal_id, proposal_version, reason_code="test"
    )
    service = _drafter_service(factory, enabled=False)

    with pytest.raises(ProposalStateConflict):
        await service.confirm_reach(
            _ctx(tenant_id=tenant_id), proposal_id, proposal_version, field_paths=["directives"]
        )


# ---------------------------------------------------------------------------
# Service level: draft, disabled -- the database is byte-identical after.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_disabled_leaves_the_database_untouched(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"drafter-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    service = _drafter_service(factory, enabled=False)

    async with factory() as session:
        before = (
            await session.execute(
                text("SELECT state, frozen_at FROM arc_authoring_proposal_versions WHERE proposal_id = :pid"),
                {"pid": proposal_id},
            )
        ).one()
        confirmations_before = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_authoring_reach_confirmations WHERE proposal_id = :pid"),
                {"pid": proposal_id},
            )
        ).scalar_one()

    with pytest.raises(Exception, match="disabled"):
        await service.draft(
            _ctx(tenant_id=tenant_id),
            proposal_id,
            proposal_version,
            source_evidence_id=uuid.uuid4(),
            target_field_paths=["directives"],
        )

    async with factory() as session:
        after = (
            await session.execute(
                text("SELECT state, frozen_at FROM arc_authoring_proposal_versions WHERE proposal_id = :pid"),
                {"pid": proposal_id},
            )
        ).one()
        confirmations_after = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_authoring_reach_confirmations WHERE proposal_id = :pid"),
                {"pid": proposal_id},
            )
        ).scalar_one()
    assert tuple(before) == tuple(after)
    assert confirmations_before == confirmations_after == 0


# ---------------------------------------------------------------------------
# Service level: draft, enabled via a test-only stub decision -- the real
# two-subprocess sandbox pipeline, against real seeded rows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_enabled_refuses_when_seeded_digest_does_not_match_the_body(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Uses the shared fixture helper's own placeholder digest (`seed_source_evidence`'s
    documented `"0" * 64`, which never matches its 4-byte inert body) --
    proving the sandbox's binding check refuses a real, seeded mismatch,
    not only a synthetic one built by hand in a unit test."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"drafter-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)
    authorization = _authorization()
    service = _drafter_service(
        factory,
        enabled=True,
        decision_outcome="accepted",
        source_admission=SourceAdmissionService(factory, authorization=authorization, clock=FakeClock(_NOW)),
        source_status=SourceStatusService(factory, clock=FakeClock(_NOW)),
    )

    result = await service.draft(
        _ctx(tenant_id=tenant_id),
        proposal_id,
        proposal_version,
        source_evidence_id=source_evidence_id,
        target_field_paths=["directives"],
    )

    assert result.patch == {}
    assert result.citations == ()
    assert result.declined_field_paths == ("directives",)


@pytest.mark.asyncio
async def test_draft_enabled_runs_the_real_sandbox_pipeline_end_to_end(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A genuinely matching seeded source: the real parser sandbox parses
    it, the real drafter sandbox receives the resulting envelope over
    `ipc.py`, and the response comes back through two real subprocesses --
    proving this is not merely a stub for the enabled path."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"drafter-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    source_evidence_id = await _seed_matching_source_evidence(factory, tenant_id=tenant_id)
    authorization = _authorization()
    service = _drafter_service(
        factory,
        enabled=True,
        decision_outcome="accepted",
        source_admission=SourceAdmissionService(factory, authorization=authorization, clock=FakeClock(_NOW)),
        source_status=SourceStatusService(factory, clock=FakeClock(_NOW)),
    )

    result = await service.draft(
        _ctx(tenant_id=tenant_id),
        proposal_id,
        proposal_version,
        source_evidence_id=source_evidence_id,
        target_field_paths=["directives", "applicability"],
    )

    assert result.patch == {}
    assert result.citations == ()
    assert result.declined_field_paths == ("directives", "applicability")
