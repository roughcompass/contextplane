"""Integration tests for source-status storage and its refresh worker,
against a real Postgres.

What the unit suite (`tests/unit/test_arc_source_status.py`) cannot prove
with an in-memory fake: that the compare-and-swap guard on
`update_status_refresh` genuinely serializes two concurrent refresh passes
into one applied write, that a row this service reads was really produced
by `SourceAdmissionService`'s own admission transaction, and that the
worker is reachable through the same scheduler registration a deployment
actually runs -- not only through a `SourceStatusRefreshWorker` a test
constructed directly.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.operational_chain import (
    EVENT_INITIALIZED,
    SYSTEM_ACTOR,
    OperationalChainService,
    build_event_payload,
)
from contextplane.arc.service.queries import source_admission as queries
from contextplane.arc.service.source_admission import (
    ApprovalProof,
    SourceAdmissionService,
    UploadAdmission,
    UploadPolicyRegistration,
)
from contextplane.arc.service.source_status import (
    FRESHNESS_WINDOW,
    STATUS_CURRENT,
    STATUS_REVOKED,
    SourceOperationalIntegrityPending,
    SourceStatusService,
    SourceStatusUnavailable,
)
from contextplane.arc.types import ArcRequestContext
from contextplane.arc.workers.source_status_refresh import RemoteStatusCheck, SourceStatusRefreshWorker
from contextplane.main import create_app
from contextplane.types import TenantContext
from tests.helpers.auth_harness import default_settings
from tests.helpers.clock import FakeClock

_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


class _RevokedProvider:
    """Reports one named source as revoked; every other id is untouched."""

    def __init__(self, revoked_id: uuid.UUID) -> None:
        self._revoked_id = revoked_id

    async def check(
        self, *, source_evidence_id: uuid.UUID, verifier_id: str, connector_id: str | None, policy_id: str | None
    ) -> RemoteStatusCheck:
        return RemoteStatusCheck(revoked=source_evidence_id == self._revoked_id, reason_code="test_revoked")


def _ctx() -> ArcRequestContext:
    tenant = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["admin"], oidc_subject=_OPERATOR)
    return ArcRequestContext.from_validated_claims(tenant, {"iss": _ISSUER})


def _digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _claim(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "profile": "arc_source_approval_claim_v1",
        "source_system": "confluence",
        "source_revision_locator": "conf://space/page@3",
        "source_content_digest_algorithm": "sha256",
        "source_content_digest": "0" * 64,
        "source_content_type": "text/markdown",
        "approval_locator": "https://confluence.example/approvals/1",
        "approving_authority_issuer": _ISSUER,
        "approving_authority_subject": "owner",
        "approval_scope": "space:eng",
        "approved_at": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
    }
    body.update(overrides)
    return body


def _proof() -> ApprovalProof:
    return ApprovalProof(
        verification_method="detached_signature", signature_algorithm="Ed25519", signature_base64="c2lnbmF0dXJl"
    )


async def _bytes_iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _admit_source(factory: async_sessionmaker[AsyncSession], *, clock: FakeClock, expires_at: str) -> uuid.UUID:
    """Admit one real source through `SourceAdmissionService`, so the row
    this test drives is exactly what admission would have produced --
    including `next_check_at` capped at *expires_at* per its own admission
    transaction, which is the mechanism `check_status`'s deadline tests
    below depend on."""
    authorization = ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))
    admission = SourceAdmissionService(factory, authorization=authorization, clock=clock)
    policy_id = f"policy-{uuid.uuid4().hex[:8]}"
    await admission.register_upload_policy(
        _ctx(),
        UploadPolicyRegistration(
            policy_id=policy_id,
            owning_scope="global",
            tenant_id=None,
            allowed_media_types=("text/markdown",),
            allowed_verifier_ids=("verifier-1",),
            max_bytes=1024,
        ),
    )
    data = b"a real document body"
    claim = _claim(source_content_digest=_digest_of(data), expires_at=expires_at)
    evidence = await admission.admit_upload(
        _ctx(),
        UploadAdmission(
            policy_id=policy_id,
            source_system="confluence",
            source_revision_locator="conf://space/page@3",
            source_content_type="text/markdown",
            claim=claim,
            verifier_id="verifier-1",
            proof=_proof(),
            idempotency_key=f"key-{uuid.uuid4().hex[:8]}",
        ),
        _bytes_iter([data]),
    )
    return evidence.source_evidence_id


async def _status_row(factory: async_sessionmaker[AsyncSession], source_evidence_id: uuid.UUID) -> object:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT status, checked_at, next_check_at FROM arc_source_approval_status "
                    "WHERE source_evidence_id = :id"
                ),
                {"id": source_evidence_id},
            )
        ).one()


def _fields(row: object) -> tuple[object, object, object]:
    """`(status, checked_at, next_check_at)`, the three columns a "row is
    byte-for-byte unchanged" assertion below compares -- named so those
    comparisons read as one claim rather than three separate attribute
    accesses each time."""
    return (row.status, row.checked_at, row.next_check_at)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# SourceStatusService.check_status against a real admitted row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_status_succeeds_right_after_admission(factory: async_sessionmaker[AsyncSession]) -> None:
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")
    service = SourceStatusService(factory, clock=clock)

    view = await service.check_status(source_evidence_id)

    assert view.status == STATUS_CURRENT


@pytest.mark.asyncio
async def test_overdue_fails_closed_against_a_real_admitted_row(factory: async_sessionmaker[AsyncSession]) -> None:
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")
    clock.tick(FRESHNESS_WINDOW)  # exactly at the freshness boundary: already overdue
    service = SourceStatusService(factory, clock=clock)

    with pytest.raises(SourceStatusUnavailable, match="overdue"):
        await service.check_status(source_evidence_id)


@pytest.mark.asyncio
async def test_expiry_deadline_fails_closed_before_any_refresh_pass_runs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Proves the deadline is enforced by the read path itself, independent
    of the worker ever having run: admission caps `next_check_at` at the
    claim's own `expires_at` when that is sooner than the freshness window,
    so `check_status` starts refusing exactly there with no worker pass in
    between."""
    clock = FakeClock(_NOW)
    expires_at = _NOW + datetime.timedelta(seconds=120)
    expires_at_wire = expires_at.isoformat().replace("+00:00", "Z")
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at=expires_at_wire)

    clock.set(expires_at - datetime.timedelta(seconds=1))
    service = SourceStatusService(factory, clock=clock)
    await service.check_status(source_evidence_id)  # one second before: still current

    clock.set(expires_at)
    with pytest.raises(SourceStatusUnavailable, match="overdue"):
        await service.check_status(source_evidence_id)


@pytest.mark.asyncio
async def test_record_revocation_refuses_and_leaves_a_real_row_untouched(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")
    before = await _status_row(factory, source_evidence_id)
    service = SourceStatusService(factory, clock=clock)

    with pytest.raises(SourceOperationalIntegrityPending):
        await service.record_revocation(source_evidence_id, reason_code="upstream_revoked")

    after = await _status_row(factory, source_evidence_id)
    assert _fields(after) == _fields(before)


# ---------------------------------------------------------------------------
# record_revocation / record_expiry -- the real four-part write, once an
# operational-chain appender is injected. The tests above prove refusal
# leaves rows unchanged while no appender exists; this is the enabled
# transaction those refusal tests anticipated once an appender becomes
# available.
# ---------------------------------------------------------------------------


async def _seed_dependent_active_revision(
    factory: async_sessionmaker[AsyncSession], *, source_evidence_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """An `active` revision materialized from a proposal version that
    named *source_evidence_id* -- the exact join
    `find_active_revisions_by_source` reads. Raw SQL, not
    `ArtifactMaterialisationService.submit` (still unreachable on every
    deployment today -- see that service's own module docstring): this
    builds the row shape submission will eventually produce, directly.
    """
    artifact_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_artifacts ("
                "  artifact_id, tenant_id, slug, kind, title, created_at, created_by_issuer, created_by_subject"
                ") VALUES (:aid, NULL, :slug, 'policy', :title, :now, :issuer, :subject)"
            ),
            {
                "aid": artifact_id,
                "slug": f"dep-{artifact_id.hex[:8]}",
                "title": f"Dependent {artifact_id.hex[:8]}",
                "now": now,
                "issuer": _ISSUER,
                "subject": _OPERATOR,
            },
        )
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, created_at"
                ") VALUES ("
                "  :rid, :aid, NULL, 'test-system', :locator, :revision_locator, :digest, 'active', :efrom,"
                "  :review, 'all_matched_actors', 'revision_pinned_only', 'internal', :retention, 'none', :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "locator": f"loc://{revision_id.hex[:8]}",
                "revision_locator": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": _NOW - datetime.timedelta(days=1),
                "review": _NOW + datetime.timedelta(days=365),
                "retention": _NOW + datetime.timedelta(days=730),
                "now": now,
            },
        )
        await session.execute(
            text(
                "INSERT INTO arc_authoring_proposals (proposal_id, artifact_id, created_at) VALUES (:pid, :aid, :now)"
            ),
            {"pid": proposal_id, "aid": artifact_id, "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO arc_authoring_proposal_versions ("
                "  proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id, revision_id,"
                "  opened_by_issuer, opened_by_subject, created_at"
                ") VALUES (:pid, 1, :aid, NULL, 'activated', :sid, :rid, :issuer, :subject, :now)"
            ),
            {
                "pid": proposal_id,
                "aid": artifact_id,
                "sid": source_evidence_id,
                "rid": revision_id,
                "issuer": _ISSUER,
                "subject": _OPERATOR,
                "now": now,
            },
        )
    return artifact_id, revision_id


@pytest.mark.asyncio
async def test_record_revocation_commits_all_four_parts_together(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The real transaction, against real foreign keys: the source status
    flip, the dependent revision's lifecycle cascade, the operational
    event that makes it provable, and the audit row -- all committing
    together, once a real `OperationalChainService` is injected.
    """
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")
    artifact_id, revision_id = await _seed_dependent_active_revision(factory, source_evidence_id=source_evidence_id)

    chain = OperationalChainService(clock=clock, deployment_id="cascade-test")
    # Seed genesis directly -- standing in for the submission transaction
    # that will eventually write it (still unreachable today; see
    # `ArtifactMaterialisationService`'s own module docstring).
    async with factory() as session, session.begin():
        await chain.append_event(
            session,
            artifact_id=artifact_id,
            revision_id=revision_id,
            event_type=EVENT_INITIALIZED,
            actor=SYSTEM_ACTOR,
            payload=build_event_payload(initial_freshness_basis="connector_verified", retention_floor_days=730),
            authorization_decision_reference="it-test:genesis",
            authority_evidence_digest="1" * 64,
            idempotency_key="genesis",
        )

    async with factory() as session:
        audit_before = (await session.execute(text("SELECT count(*) FROM arc_audit_outbox"))).scalar_one()
    service = SourceStatusService(factory, clock=clock, operational_chain_appender=chain)

    await service.record_revocation(source_evidence_id, reason_code="upstream_revoked")

    # 1. Source status flipped.
    status_row = await _status_row(factory, source_evidence_id)
    assert status_row.status == STATUS_REVOKED  # type: ignore[attr-defined]

    # 2. The dependent revision cascaded to revoked.
    async with factory() as session:
        lifecycle_state = (
            await session.execute(
                text("SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar_one()
    assert lifecycle_state == "revoked"

    # 3. A second, real operational event exists and the chain verifies.
    async with factory() as session:
        sequence_count = (
            await session.execute(
                text("SELECT count(*) FROM arc_operational_events WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar_one()
    assert sequence_count == 2
    async with factory() as session:
        await chain.verify_chain(session, revision_id)  # must not raise

    # 4. A pending checkpoint exists for the new event.
    async with factory() as session:
        pending = (
            await session.execute(
                text(
                    "SELECT count(*) FROM arc_operational_chain_checkpoints "
                    "WHERE revision_id = :rid AND exported_at IS NULL"
                ),
                {"rid": revision_id},
            )
        ).scalar_one()
    assert pending == 2  # genesis's own checkpoint, plus this one

    # 5. An audit row was written, in the same transaction.
    async with factory() as session:
        audit_after = (await session.execute(text("SELECT count(*) FROM arc_audit_outbox"))).scalar_one()
    assert audit_after == audit_before + 1


@pytest.mark.asyncio
async def test_record_revocation_with_no_dependents_still_flips_status(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A source can be revoked before anything cites it -- the status flip
    and audit row happen; the cascade simply has nothing to touch."""
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")
    chain = OperationalChainService(clock=clock, deployment_id="cascade-test-2")
    service = SourceStatusService(factory, clock=clock, operational_chain_appender=chain)

    await service.record_revocation(source_evidence_id, reason_code="upstream_revoked")

    status_row = await _status_row(factory, source_evidence_id)
    assert status_row.status == STATUS_REVOKED  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_record_revocation_twice_cascades_exactly_once_against_real_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")
    artifact_id, revision_id = await _seed_dependent_active_revision(factory, source_evidence_id=source_evidence_id)
    chain = OperationalChainService(clock=clock, deployment_id="cascade-test-3")
    async with factory() as session, session.begin():
        await chain.append_event(
            session,
            artifact_id=artifact_id,
            revision_id=revision_id,
            event_type=EVENT_INITIALIZED,
            actor=SYSTEM_ACTOR,
            payload=build_event_payload(initial_freshness_basis="connector_verified"),
            authorization_decision_reference="it-test:genesis",
            authority_evidence_digest="1" * 64,
            idempotency_key="genesis",
        )
    service = SourceStatusService(factory, clock=clock, operational_chain_appender=chain)

    await service.record_revocation(source_evidence_id, reason_code="upstream_revoked")
    await service.record_revocation(source_evidence_id, reason_code="upstream_revoked")

    async with factory() as session:
        event_count = (
            await session.execute(
                text("SELECT count(*) FROM arc_operational_events WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar_one()
    assert event_count == 2  # genesis + exactly one cascade, never two


# ---------------------------------------------------------------------------
# SourceStatusRefreshWorker against real rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worker_extends_the_window_for_a_due_current_row(factory: async_sessionmaker[AsyncSession]) -> None:
    # Assertions below are scoped to this test's own `source_evidence_id`
    # rather than the worker result's aggregate counts: the container
    # backing this session is shared and never rolled back (see the module
    # docstring), and other tests in this suite leave their own due-or-
    # stuck rows behind in the same table, which the aggregate would also
    # count. Scoping by id is what keeps this test honest regardless of
    # what else happens to be in `arc_source_approval_status` at the time.
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")
    clock.tick(FRESHNESS_WINDOW)
    service = SourceStatusService(factory, clock=clock)
    worker = SourceStatusRefreshWorker(factory, service, clock=clock)

    result = await worker.run_once()

    assert result.failed == 0
    row = await _status_row(factory, source_evidence_id)
    assert row.checked_at == clock.now()
    assert row.next_check_at == clock.now() + FRESHNESS_WINDOW

    # The freshly extended window makes the source trustable again, at the
    # same clock reading that was "overdue" a moment before this pass ran.
    await service.check_status(source_evidence_id)


@pytest.mark.asyncio
async def test_running_the_worker_twice_immediately_refreshes_the_row_only_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")
    clock.tick(FRESHNESS_WINDOW)
    service = SourceStatusService(factory, clock=clock)
    worker = SourceStatusRefreshWorker(factory, service, clock=clock)

    await worker.run_once()
    once = await _status_row(factory, source_evidence_id)
    assert once.next_check_at == clock.now() + FRESHNESS_WINDOW

    await worker.run_once()
    twice = await _status_row(factory, source_evidence_id)

    assert (twice.status, twice.checked_at, twice.next_check_at) == (once.status, once.checked_at, once.next_check_at)


@pytest.mark.asyncio
async def test_the_worker_attempts_expiry_at_the_deadline_and_leaves_the_row_unchanged(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(_NOW)
    expires_at = _NOW + datetime.timedelta(seconds=120)
    source_evidence_id = await _admit_source(
        factory, clock=clock, expires_at=expires_at.isoformat().replace("+00:00", "Z")
    )
    before = await _status_row(factory, source_evidence_id)

    clock.set(expires_at)
    service = SourceStatusService(factory, clock=clock)
    worker = SourceStatusRefreshWorker(factory, service, clock=clock)

    await worker.run_once()

    after = await _status_row(factory, source_evidence_id)
    assert _fields(after) == _fields(before)


@pytest.mark.asyncio
async def test_an_upstream_revocation_is_attempted_and_the_row_is_left_unchanged(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")
    before = await _status_row(factory, source_evidence_id)
    clock.tick(FRESHNESS_WINDOW)
    service = SourceStatusService(factory, clock=clock)
    worker = SourceStatusRefreshWorker(
        factory, service, clock=clock, remote_provider=_RevokedProvider(source_evidence_id)
    )

    await worker.run_once()

    after = await _status_row(factory, source_evidence_id)
    assert _fields(after) == _fields(before)


@pytest.mark.asyncio
async def test_two_concurrent_compare_and_swap_refreshes_apply_exactly_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`update_status_refresh`'s own compare-and-swap guard, proven under
    real concurrency rather than against an in-memory fake: two connections
    racing to refresh the identical row, with the identical new values,
    must not both apply -- exactly one write can win, the other must see
    the row already refreshed and back off."""
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")
    checked_at = _NOW + FRESHNESS_WINDOW
    next_check_at = checked_at + FRESHNESS_WINDOW

    async def _apply() -> bool:
        async with factory() as session, session.begin():
            return await queries.update_status_refresh(
                session, source_evidence_id=source_evidence_id, checked_at=checked_at, next_check_at=next_check_at
            )

    applied_a, applied_b = await asyncio.gather(_apply(), _apply())

    assert sorted([applied_a, applied_b]) == [False, True], "exactly one concurrent compare-and-swap must apply"
    row = await _status_row(factory, source_evidence_id)
    assert row.next_check_at == next_check_at


# ---------------------------------------------------------------------------
# The worker is reachable through the real scheduler registration, not only
# through a directly constructed SourceStatusRefreshWorker.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def wired_app(pg_container: str) -> AsyncIterator[FastAPI]:
    settings = default_settings(pg_container)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest.mark.asyncio
async def test_the_scheduler_registers_and_can_actually_run_the_refresh_job(wired_app: FastAPI) -> None:
    """Runs the exact callable `create_app`'s own scheduler wiring
    registered -- not a worker this test constructed itself -- against the
    real database that app is wired to. A registration that could not
    actually be invoked (a wrong signature, a missing collaborator) fails
    here rather than staying invisible until the interval trigger fires in
    a running deployment."""
    services = wired_app.state.services
    factory = services.session_factory
    clock = FakeClock(_NOW)
    source_evidence_id = await _admit_source(factory, clock=clock, expires_at="2027-01-01T00:00:00Z")

    # The deployment's own clock is the real wall clock, not something this
    # test can inject -- so instead of advancing a `FakeClock` we rewrite
    # this row's own due timestamp to the database's current time, which is
    # what proves the pass this fires is the real, unmodified worker rather
    # than one this test substituted a test clock into.
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_source_approval_status SET next_check_at = now() - interval '1 second' "
                "WHERE source_evidence_id = :id"
            ),
            {"id": source_evidence_id},
        )

    job = services.scheduler.get_job("arc_source_status_refresh")
    assert job is not None, "arc_source_status_refresh must be registered by the app's own scheduler wiring"

    await job.func()  # the exact coroutine register_periodic wrapped and scheduled

    row = await _status_row(factory, source_evidence_id)
    assert row.status == STATUS_CURRENT
    assert row.next_check_at > row.checked_at
