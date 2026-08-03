"""Artifact lifecycle: activation, supersession, revocation, invalidation.

The state machine matters because agents act on what it says. An activation
that raced another and produced two live revisions would mean two answers to
"what must I do", and a revocation that let its obligation disappear would
silently unblock everything that obligation used to govern.

Those two are the tests to read first: the concurrent-activation race and
the revocation tombstone.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.artifact import (
    OBLIGATION_MISSING_INVALID,
    OBLIGATION_MISSING_REVOKED,
    OBLIGATION_SATISFIED,
    ApplicabilityDraft,
    ArtifactLifecycleError,
    ArtifactService,
    DirectiveDraft,
    RegisteredRevision,
    RevisionDraft,
    SourceIdentity,
    _conflict_subject_digest,
)
from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.types import ArcRequestContext, AuthorityScope, DetailAudience
from registry.audit import actions
from registry.exceptions import ValidationError
from registry.types import FakeClock, TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc

_CONFLICT_KEY = {
    "namespace": "deploy",
    "subject_selector": "service:payments",
    "operation": "release",
    "action_class": "deploy",
    "target_selector": "production",
    "modality": "require",
    "constraint_operator": "equals",
    "constraint_value": "reviewed",
}
_EVIDENCE_ID = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-lifecycle")


class _AllVisible:
    async def visible_capability_ids(self, ctx: object, capability_ids: object) -> list[uuid.UUID]:
        return list(capability_ids)  # type: ignore[arg-type]


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession]) -> ArtifactService:
    return ArtifactService(
        factory,
        authorization=ArcAuthorizationService(visibility=_AllVisible(), global_write_allowlist=()),
        clock=FakeClock(ARC_NOW),
    )


def _ctx(seed: ArcSeed) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=["admin"], oidc_subject="s"
    )
    return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"}, host_id="h")


@pytest_asyncio.fixture(autouse=True)
async def scaffolding(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """The conflict domain and the approval-evidence row activation requires."""
    subject = {k: _CONFLICT_KEY[k] for k in
               ("namespace", "subject_selector", "operation", "action_class", "target_selector")}
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_conflict_domains (conflict_subject_digest, conflict_subject_key) "
                "VALUES (:d, CAST(:k AS JSONB)) ON CONFLICT DO NOTHING"
            ),
            {"d": _conflict_subject_digest(dict(_CONFLICT_KEY)), "k": json.dumps(subject, sort_keys=True)},
        )


async def _seed_evidence(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, revision_id: uuid.UUID
) -> uuid.UUID:
    """A minimal approval-evidence row for one specific revision.

    Created *after* registration because the schema requires
    `artifact_activation` evidence to name `approved_revision_id`, and that
    id does not exist until the revision has been registered. That ordering
    is what `attach_approval_evidence` exists for.

    `verifier_attested` rather than `operator_signed` so the row needs no
    real key material — the verifier row it points at is seeded alongside,
    because the foreign key is real.
    """
    evidence_id = uuid.uuid4()
    verifier_id = f"v-{uuid.uuid4().hex[:12]}"
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_approval_verifiers ("
                "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind,"
                "  scope_tenant_id, provider_id, valid_from"
                ") VALUES (:vid, 'trusted_attestation_provider', ARRAY['artifact_activation'],"
                "          'tenant', :tid, 'in-process-test', :from)"
            ),
            {"vid": verifier_id, "tid": seed.tenant_id, "from": ARC_NOW - datetime.timedelta(days=1)},
        )
        await session.execute(
            text(
                "INSERT INTO arc_approval_evidence ("
                "  evidence_id, evidence_type, scope_kind, scope_tenant_id, approved_artifact_id,"
                "  approved_revision_id, approved_payload_digest, approving_principal, approving_role,"
                "  approval_timestamp, verification_method, approval_verifier_id, verifier_attestation,"
                "  verifier_identity, audit_log_reference, created_at"
                ") VALUES (:eid, 'artifact_activation', 'tenant', :tid, :aid, :rid, :digest,"
                "          'operator@example.test', 'governance_owner', :now,"
                "          'verifier_attested', :vid, CAST('{}' AS JSONB), 'in-process-test',"
                "          'audit://approval/1', :now)"
            ),
            {
                "eid": evidence_id,
                "tid": seed.tenant_id,
                "aid": seed.artifact_id,
                "rid": revision_id,
                "digest": uuid.uuid4().hex + uuid.uuid4().hex,
                "vid": verifier_id,
                "now": ARC_NOW,
            },
        )
    return evidence_id


def _draft(seed: ArcSeed, **overrides: object) -> RevisionDraft:
    unique = uuid.uuid4().hex[:12]
    base: dict[str, object] = {
        "artifact_id": seed.artifact_id,
        "source": SourceIdentity(
            source_system="confluence",
            source_canonical_locator=f"conf://{unique}",
            source_revision_locator=f"conf://{unique}@1",
            content_digest=uuid.uuid4().hex + uuid.uuid4().hex,
        ),
        "content_classification": "internal",
        "detail_audience": DetailAudience.ALL_MATCHED_ACTORS,
        "freshness_basis": "revision_pinned_only",
        "effective_from": ARC_NOW,
        "review_expires_at": ARC_NOW + datetime.timedelta(days=365),
        "content_retention_until": ARC_NOW + datetime.timedelta(days=730),
        "body_plaintext": "Deploys require review.",
        "directives": (
            DirectiveDraft(
                directive_id=uuid.uuid4(),
                directive_type="require",
                source_anchor="#p",
                compact_statement="Deploys require review.",
                conflict_key=dict(_CONFLICT_KEY),
            ),
        ),
        "rules": (
            ApplicabilityDraft(
                scope=AuthorityScope.TENANT,
                effective_from=ARC_NOW,
                target_tenant_id=seed.tenant_id,
                task_kinds=("deployment",),
                action_classes=("deploy",),
                is_mandatory=True,
            ),
        ),
    }
    base.update(overrides)
    return RevisionDraft(**base)  # type: ignore[arg-type]


async def _register(
    factory: async_sessionmaker[AsyncSession],
    service: ArtifactService,
    seed: ArcSeed,
    **overrides: object,
) -> RegisteredRevision:
    revision = await service.register_revision(_ctx(seed), _draft(seed, **overrides))
    evidence_id = await _seed_evidence(factory, seed, revision.revision_id)
    await service.attach_approval_evidence(_ctx(seed), revision.revision_id, evidence_id)
    return revision


async def _state(factory: async_sessionmaker[AsyncSession], revision_id: uuid.UUID) -> str:
    async with factory() as session:
        return (
            await session.execute(
                text("SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid"),
                {"rid": revision_id},
            )
        ).scalar_one()


# --- activation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_activation_puts_a_draft_into_force(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    revision = await _register(factory, service, seed)
    await service.activate(_ctx(seed), revision.revision_id)
    assert await _state(factory, revision.revision_id) == "active"


@pytest.mark.asyncio
async def test_activation_creates_the_mandatory_obligation(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """The durable record that survives its revision being revoked."""
    revision = await _register(factory, service, seed)
    await service.activate(_ctx(seed), revision.revision_id)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT obligation_state, current_revision_id, applicability_snapshot "
                    "FROM arc_mandatory_obligations WHERE current_revision_id = :rid"
                ),
                {"rid": revision.revision_id},
            )
        ).one()
    assert row.obligation_state == OBLIGATION_SATISFIED
    assert row.applicability_snapshot["task_kinds"] == ["deployment"]


@pytest.mark.asyncio
async def test_activation_without_approval_evidence_is_refused(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """Activation is what makes a rule bind agents; doing that on content
    nobody approved is the failure this whole subsystem exists to prevent."""
    revision = await service.register_revision(_ctx(seed), _draft(seed))
    with pytest.raises(ArtifactLifecycleError, match="no approval evidence"):
        await service.activate(_ctx(seed), revision.revision_id)


@pytest.mark.asyncio
async def test_a_revision_past_its_review_date_cannot_be_activated(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """Re-checked at activation rather than trusted from registration.

    Registration rightly refuses a review date already in the past, so the
    scenario has to be built the way it actually happens: a revision
    registered while valid, left in draft long enough to go stale. The
    backdating below stands in for that elapsed time.
    """
    revision = await _register(factory, service, seed)
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET review_expires_at = :past WHERE revision_id = :rid"),
            {"rid": revision.revision_id, "past": ARC_NOW - datetime.timedelta(days=1)},
        )

    with pytest.raises(ArtifactLifecycleError, match="review date"):
        await service.activate(_ctx(seed), revision.revision_id)


@pytest.mark.asyncio
async def test_activation_emits_an_audit_row(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    revision = await _register(factory, service, seed)
    await service.activate(_ctx(seed), revision.revision_id)

    async with factory() as session:
        event_type = (
            await session.execute(
                text(
                    "SELECT event_type FROM arc_audit_outbox "
                    "WHERE event_payload ->> 'revision_id' = :rid AND event_type = :t"
                ),
                {"rid": str(revision.revision_id), "t": actions.ARC_ARTIFACT_ACTIVATED},
            )
        ).scalar_one()
    assert event_type == actions.ARC_ARTIFACT_ACTIVATED


# --- supersession ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_activating_a_successor_supersedes_the_incumbent(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    first = await _register(factory, service, seed)
    await service.activate(_ctx(seed), first.revision_id)

    second = await _register(factory, service, seed)
    await service.activate(_ctx(seed), second.revision_id)

    assert await _state(factory, first.revision_id) == "superseded"
    assert await _state(factory, second.revision_id) == "active"


@pytest.mark.asyncio
async def test_the_superseded_revision_records_what_replaced_it(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """An auditor asking why a rule stopped applying needs the successor."""
    first = await _register(factory, service, seed)
    await service.activate(_ctx(seed), first.revision_id)
    second = await _register(factory, service, seed)
    await service.activate(_ctx(seed), second.revision_id)

    async with factory() as session:
        successor = (
            await session.execute(
                text("SELECT superseded_by_revision_id FROM arc_revisions WHERE revision_id = :rid"),
                {"rid": first.revision_id},
            )
        ).scalar_one()
    assert successor == second.revision_id


@pytest.mark.asyncio
async def test_only_one_revision_of_an_artifact_is_ever_active(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """Two live revisions would be two answers to "what must I do"."""
    for _ in range(3):
        revision = await _register(factory, service, seed)
        await service.activate(_ctx(seed), revision.revision_id)

    async with factory() as session:
        active = (
            await session.execute(
                text(
                    "SELECT count(*) FROM arc_revisions "
                    "WHERE artifact_id = :aid AND lifecycle_state = 'active'"
                ),
                {"aid": seed.artifact_id},
            )
        ).scalar_one()
    assert active == 1


@pytest.mark.asyncio
async def test_a_successor_takes_over_the_obligation_rather_than_adding_one(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """Obligations are keyed by the stable directive identity, so a
    replacement satisfies the obligation its predecessor left rather than
    creating a second one that would double-block."""
    shared_directive = uuid.uuid4()

    def with_directive(**extra: object) -> dict[str, object]:
        return {
            "directives": (
                DirectiveDraft(
                    directive_id=shared_directive,
                    directive_type="require",
                    source_anchor="#p",
                    compact_statement="Deploys require review.",
                    conflict_key=dict(_CONFLICT_KEY),
                ),
            ),
            **extra,
        }

    first = await _register(factory, service, seed, **with_directive())
    await service.activate(_ctx(seed), first.revision_id)
    second = await _register(factory, service, seed, **with_directive())
    await service.activate(_ctx(seed), second.revision_id)

    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT obligation_state, current_revision_id FROM arc_mandatory_obligations "
                    "WHERE directive_id = :did"
                ),
                {"did": shared_directive},
            )
        ).all()

    assert len(rows) == 1
    assert rows[0].current_revision_id == second.revision_id
    assert rows[0].obligation_state == OBLIGATION_SATISFIED


@pytest.mark.asyncio
async def test_concurrent_activations_serialize_to_one_active_revision(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """The family lock. Four activations launched together must leave
    exactly one active revision, not four that each checked "is anything
    active?" before any of them wrote."""
    revisions = [await _register(factory, service, seed) for _ in range(4)]

    results = await asyncio.gather(
        *(service.activate(_ctx(seed), r.revision_id) for r in revisions), return_exceptions=True
    )
    unexpected = [r for r in results if isinstance(r, BaseException) and not isinstance(r, ArtifactLifecycleError)]
    assert not unexpected, [repr(u) for u in unexpected]

    async with factory() as session:
        active = (
            await session.execute(
                text(
                    "SELECT count(*) FROM arc_revisions "
                    "WHERE artifact_id = :aid AND lifecycle_state = 'active'"
                ),
                {"aid": seed.artifact_id},
            )
        ).scalar_one()
    assert active == 1


# --- revocation and invalidation ---------------------------------------------------


@pytest.mark.asyncio
async def test_revocation_leaves_the_obligation_standing_as_a_tombstone(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """The single most important test here.

    If the obligation vanished with its revision, every resolution it used
    to block would silently start succeeding. It must remain, without a
    revision, so matching requests keep blocking until an approved
    successor satisfies it.
    """
    revision = await _register(factory, service, seed)
    await service.activate(_ctx(seed), revision.revision_id)
    await service.revoke(_ctx(seed), revision.revision_id, reason="withdrawn by governance")

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT obligation_state, current_revision_id, applicability_snapshot "
                    "FROM arc_mandatory_obligations WHERE artifact_id = :aid"
                ),
                {"aid": seed.artifact_id},
            )
        ).one()

    assert row.obligation_state == OBLIGATION_MISSING_REVOKED
    assert row.current_revision_id is None
    # The applicability survives, or nothing would know who to block.
    assert row.applicability_snapshot["task_kinds"] == ["deployment"]


@pytest.mark.asyncio
async def test_invalidation_tombstones_differently_from_revocation(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """Revocation says the rule no longer applies; invalidation says the
    content was wrong. An auditor must be able to tell them apart."""
    revision = await _register(factory, service, seed)
    await service.activate(_ctx(seed), revision.revision_id)
    await service.invalidate(_ctx(seed), revision.revision_id, reason="source deleted upstream")

    async with factory() as session:
        state = (
            await session.execute(
                text("SELECT obligation_state FROM arc_mandatory_obligations WHERE artifact_id = :aid"),
                {"aid": seed.artifact_id},
            )
        ).scalar_one()
    assert state == OBLIGATION_MISSING_INVALID


@pytest.mark.asyncio
async def test_a_revoked_revision_cannot_be_reactivated(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """Revocation is terminal. A revoked revision is evidence of what was
    once in force, not something that can bind again."""
    revision = await _register(factory, service, seed)
    await service.activate(_ctx(seed), revision.revision_id)
    await service.revoke(_ctx(seed), revision.revision_id, reason="withdrawn")

    with pytest.raises(ArtifactLifecycleError, match="cannot move"):
        await service.activate(_ctx(seed), revision.revision_id)


@pytest.mark.asyncio
async def test_revoking_twice_is_refused(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    revision = await _register(factory, service, seed)
    await service.activate(_ctx(seed), revision.revision_id)
    await service.revoke(_ctx(seed), revision.revision_id, reason="once")

    with pytest.raises(ArtifactLifecycleError):
        await service.revoke(_ctx(seed), revision.revision_id, reason="twice")


@pytest.mark.asyncio
async def test_a_draft_can_be_revoked_without_ever_being_activated(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """Registered in error, withdrawn before it ever bound anyone."""
    revision = await _register(factory, service, seed)
    await service.revoke(_ctx(seed), revision.revision_id, reason="registered in error")
    assert await _state(factory, revision.revision_id) == "revoked"


@pytest.mark.asyncio
async def test_revocation_emits_an_audit_row_with_its_reason(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    revision = await _register(factory, service, seed)
    await service.activate(_ctx(seed), revision.revision_id)
    await service.revoke(_ctx(seed), revision.revision_id, reason="superseded by external policy")

    async with factory() as session:
        payload = (
            await session.execute(
                text(
                    "SELECT event_payload FROM arc_audit_outbox "
                    "WHERE event_payload ->> 'revision_id' = :rid AND event_type = :t"
                ),
                {"rid": str(revision.revision_id), "t": actions.ARC_ARTIFACT_REVOKED},
            )
        ).scalar_one()
    assert payload["reason"] == "superseded by external policy"


@pytest.mark.asyncio
async def test_lifecycle_operations_require_write_authorization(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    from registry.arc.service.authorization import ArcAuthorizationError

    revision = await _register(factory, service, seed)
    outsider = TenantContext(
        tenant_id=uuid.uuid4(), actor_id=seed.actor_id, roles=["admin"], oidc_subject="s"
    )
    ctx = ArcRequestContext.from_validated_claims(outsider, {"iss": "https://idp.example.test"}, host_id="h")

    with pytest.raises(ArcAuthorizationError):
        await service.activate(ctx, revision.revision_id)
    with pytest.raises(ArcAuthorizationError):
        await service.revoke(ctx, revision.revision_id, reason="not mine")


# --- evidence must approve THIS revision, checked wherever it is bound ----------


@pytest.mark.asyncio
async def test_activation_refuses_evidence_that_approves_another_revision(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """Checked at activation, not only where the link was made.

    `attach_approval_evidence` was the sole enforcer of "this evidence
    approves *this* revision", and the column could be written directly. A
    revision could therefore be bound to somebody else's approval and
    activated on it -- borrowing an approval is the whole failure approval
    evidence exists to prevent. Activation is the step that puts a revision
    into force, so it has to hold regardless of how the column was populated.

    The bypass is simulated with a direct write, because the field that
    allowed it has been removed from the registration API.
    """
    other = await service.register_revision(_ctx(seed), _draft(seed))
    borrowed = await _seed_evidence(factory, seed, other.revision_id)

    target = await service.register_revision(_ctx(seed), _draft(seed))
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET approval_evidence_id = :eid WHERE revision_id = :rid"),
            {"eid": borrowed, "rid": target.revision_id},
        )

    with pytest.raises(ValidationError, match="does not approve revision"):
        await service.activate(_ctx(seed), target.revision_id)


@pytest.mark.asyncio
async def test_activation_accepts_evidence_that_does_approve_the_revision(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """The control: the check must not reject the legitimate binding."""
    revision = await service.register_revision(_ctx(seed), _draft(seed))
    evidence_id = await _seed_evidence(factory, seed, revision.revision_id)
    await service.attach_approval_evidence(_ctx(seed), revision.revision_id, evidence_id)

    await service.activate(_ctx(seed), revision.revision_id)

    async with factory() as session:
        state = (
            await session.execute(
                text("SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid"),
                {"rid": revision.revision_id},
            )
        ).scalar_one()
    assert state == "active"
