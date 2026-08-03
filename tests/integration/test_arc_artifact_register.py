"""Artifact registration: the only write path into governed content.

Registration records an *already-approved* upstream revision; it is not a
policy editor. The tests that matter most here are the ones proving that:
the source-identity uniqueness constraint, the refusal to register a
revision that could never match anything, and the fact that a partial write
leaves nothing behind.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.artifact import (
    LIFECYCLE_DRAFT,
    ApplicabilityDraft,
    ArtifactService,
    DirectiveDraft,
    RevisionDraft,
    SourceIdentity,
)
from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.types import ArcRequestContext, AuthorityScope, DetailAudience
from registry.audit import actions
from registry.exceptions import ConflictError, NotFoundError, ValidationError
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


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-register")


class _AllVisible:
    async def visible_capability_ids(self, ctx: object, capability_ids: object) -> list[uuid.UUID]:
        return list(capability_ids)  # type: ignore[arg-type]


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession]) -> ArtifactService:
    return ArtifactService(
        factory,
        # These tests exercise the checks *beyond* the verification gate, so
        # they opt in. A deployment with no registered verifier refuses to
        # activate at all -- covered separately.
        approval_verification_enabled=True,
        authorization=ArcAuthorizationService(visibility=_AllVisible(), global_write_allowlist=()),
        clock=FakeClock(ARC_NOW),
    )


def _ctx(seed: ArcSeed, *, roles: list[str] | None = None) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=roles or ["admin"], oidc_subject="s"
    )
    return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"}, host_id="host-1")


def _draft(seed: ArcSeed, **overrides: object) -> RevisionDraft:
    unique = uuid.uuid4().hex[:12]
    base: dict[str, object] = {
        "artifact_id": seed.artifact_id,
        "source": SourceIdentity(
            source_system="confluence",
            source_canonical_locator=f"conf://{unique}",
            source_revision_locator=f"conf://{unique}@3",
            content_digest=uuid.uuid4().hex + uuid.uuid4().hex,
        ),
        "content_classification": "internal",
        "detail_audience": DetailAudience.ALL_MATCHED_ACTORS,
        "freshness_basis": "revision_pinned_only",
        "effective_from": ARC_NOW,
        "review_expires_at": ARC_NOW + datetime.timedelta(days=365),
        "content_retention_until": ARC_NOW + datetime.timedelta(days=730),
        "body_plaintext": "All production deploys require a reviewed change record.",
        "directives": (
            DirectiveDraft(
                directive_id=uuid.uuid4(),
                directive_type="require",
                source_anchor="#deploy-policy",
                compact_statement="Production deploys require a reviewed change record.",
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
            ),
        ),
    }
    base.update(overrides)
    return RevisionDraft(**base)  # type: ignore[arg-type]


async def _seed_conflict_domain(factory: async_sessionmaker[AsyncSession], digest: str) -> None:
    """`arc_directives.conflict_subject_digest` is a foreign key.

    The domain row is the shared grouping every directive with this subject
    joins on, so it exists independently of any one revision — that shared
    row is what makes two directives from different artifacts comparable.
    """
    subject = {
        "namespace": _CONFLICT_KEY["namespace"],
        "subject_selector": _CONFLICT_KEY["subject_selector"],
        "operation": _CONFLICT_KEY["operation"],
        "action_class": _CONFLICT_KEY["action_class"],
        "target_selector": _CONFLICT_KEY["target_selector"],
    }
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_conflict_domains (conflict_subject_digest, conflict_subject_key) "
                "VALUES (:d, CAST(:k AS JSONB)) ON CONFLICT DO NOTHING"
            ),
            {"d": digest, "k": json.dumps(subject, sort_keys=True)},
        )


@pytest_asyncio.fixture(autouse=True)
async def conflict_domain(factory: async_sessionmaker[AsyncSession]) -> None:
    from registry.arc.service.artifact import _conflict_subject_digest

    await _seed_conflict_domain(factory, _conflict_subject_digest(dict(_CONFLICT_KEY)))


# --- the happy path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_revision_is_registered_as_draft_not_active(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """Registration records; activation is what puts a rule into force. An
    operator recording content must not accidentally bind every agent."""
    result = await service.register_revision(_ctx(seed), _draft(seed))

    assert result.lifecycle_state == LIFECYCLE_DRAFT
    async with factory() as session:
        state = (
            await session.execute(
                text("SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid"),
                {"rid": result.revision_id},
            )
        ).scalar_one()
    assert state == "draft"


@pytest.mark.asyncio
async def test_registration_records_where_the_content_came_from(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    draft = _draft(seed)
    result = await service.register_revision(_ctx(seed), draft)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT source_system, source_canonical_locator, source_revision_locator, "
                    "       content_digest, detail_audience, content_classification "
                    "FROM arc_revisions WHERE revision_id = :rid"
                ),
                {"rid": result.revision_id},
            )
        ).one()

    assert row.source_system == "confluence"
    assert row.source_revision_locator == draft.source.source_revision_locator
    assert row.content_digest == draft.source.content_digest
    assert row.detail_audience == "all_matched_actors"


@pytest.mark.asyncio
async def test_directives_and_rules_land_with_the_revision(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    result = await service.register_revision(_ctx(seed), _draft(seed))

    async with factory() as session:
        directives = (
            await session.execute(
                text("SELECT count(*) FROM arc_directives WHERE revision_id = :rid"),
                {"rid": result.revision_id},
            )
        ).scalar_one()
        rules = (
            await session.execute(
                text("SELECT count(*) FROM arc_applicability_rules WHERE revision_id = :rid"),
                {"rid": result.revision_id},
            )
        ).scalar_one()
    assert directives == 1
    assert rules == 1


@pytest.mark.asyncio
async def test_the_directive_inherits_the_artifacts_tenant(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """Denormalized from the artifact rather than taken from the caller: a
    caller able to name the tenant on a directive could file governance
    under somebody else's."""
    result = await service.register_revision(_ctx(seed), _draft(seed))

    async with factory() as session:
        tenant_id = (
            await session.execute(
                text("SELECT tenant_id FROM arc_directives WHERE revision_id = :rid"),
                {"rid": result.revision_id},
            )
        ).scalar_one()
    assert tenant_id == seed.tenant_id


@pytest.mark.asyncio
async def test_registration_emits_an_audit_row(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    result = await service.register_revision(_ctx(seed), _draft(seed))

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT event_type, event_payload FROM arc_audit_outbox "
                    "WHERE event_payload ->> 'revision_id' = :rid"
                ),
                {"rid": str(result.revision_id)},
            )
        ).one()
    assert row.event_type == actions.ARC_ARTIFACT_REGISTERED
    assert row.event_payload["directive_count"] == 1


# --- one upstream revision, one ARC revision ---------------------------------------


@pytest.mark.asyncio
async def test_registering_the_same_upstream_revision_twice_is_refused(
    service: ArtifactService, seed: ArcSeed
) -> None:
    """The constraint that makes "registration, not authoring" literal."""
    draft = _draft(seed)
    await service.register_revision(_ctx(seed), draft)

    with pytest.raises(ConflictError, match="already registered"):
        await service.register_revision(_ctx(seed), draft)


@pytest.mark.asyncio
async def test_the_same_locator_with_different_content_is_a_different_revision(
    service: ArtifactService, seed: ArcSeed
) -> None:
    """Upstream edited in place: same locator, new digest. That is a new
    revision, not a duplicate — the digest is part of the identity."""
    first = _draft(seed)
    await service.register_revision(_ctx(seed), first)

    edited = _draft(
        seed,
        source=SourceIdentity(
            source_system=first.source.source_system,
            source_canonical_locator=first.source.source_canonical_locator,
            source_revision_locator=first.source.source_revision_locator,
            content_digest=uuid.uuid4().hex + uuid.uuid4().hex,
        ),
    )
    assert await service.register_revision(_ctx(seed), edited)


@pytest.mark.asyncio
async def test_a_failed_registration_leaves_nothing_behind(
    factory: async_sessionmaker[AsyncSession], service: ArtifactService, seed: ArcSeed
) -> None:
    """A revision whose directives failed to write would be an artifact
    that binds nobody while looking registered."""
    draft = _draft(seed)
    await service.register_revision(_ctx(seed), draft)

    async with factory() as session:
        before = (
            await session.execute(
                text("SELECT count(*) FROM arc_directives WHERE tenant_id = :tid"), {"tid": seed.tenant_id}
            )
        ).scalar_one()

    with pytest.raises(ConflictError):
        await service.register_revision(_ctx(seed), draft)

    async with factory() as session:
        after = (
            await session.execute(
                text("SELECT count(*) FROM arc_directives WHERE tenant_id = :tid"), {"tid": seed.tenant_id}
            )
        ).scalar_one()
    assert after == before


# --- validation, before any row is written -------------------------------------------


@pytest.mark.asyncio
async def test_a_revision_with_no_directives_is_refused(service: ArtifactService, seed: ArcSeed) -> None:
    with pytest.raises(ValidationError, match="at least one directive"):
        await service.register_revision(_ctx(seed), _draft(seed, directives=()))


@pytest.mark.asyncio
async def test_a_revision_with_no_applicability_rule_is_refused(
    service: ArtifactService, seed: ArcSeed
) -> None:
    """It could never match anything, so it is governance nobody is subject
    to — almost certainly an authoring mistake rather than intent."""
    with pytest.raises(ValidationError, match="never match anything"):
        await service.register_revision(_ctx(seed), _draft(seed, rules=()))


@pytest.mark.asyncio
async def test_an_action_protecting_directive_without_a_conflict_key_is_refused(
    service: ArtifactService, seed: ArcSeed
) -> None:
    """A directive that can block an action must be comparable with others,
    and it cannot be compared without a conflict key."""
    bad = DirectiveDraft(
        directive_id=uuid.uuid4(),
        directive_type="require",
        source_anchor="#x",
        compact_statement="Something required.",
        conflict_key=None,
    )
    with pytest.raises(ValidationError, match="must carry a conflict key"):
        await service.register_revision(_ctx(seed), _draft(seed, directives=(bad,)))


@pytest.mark.asyncio
async def test_a_citation_only_directive_may_omit_the_conflict_key(
    service: ArtifactService, seed: ArcSeed
) -> None:
    """The negative control: it carries no comparable constraint, so it has
    nothing to conflict with."""
    citation = DirectiveDraft(
        directive_id=uuid.uuid4(),
        directive_type="citation_only",
        source_anchor="#background",
        compact_statement="Background reading.",
        conflict_key=None,
    )
    assert await service.register_revision(_ctx(seed), _draft(seed, directives=(citation,)))


@pytest.mark.asyncio
async def test_a_duplicate_directive_id_within_one_revision_is_refused(
    service: ArtifactService, seed: ArcSeed
) -> None:
    shared = uuid.uuid4()
    twins = (
        DirectiveDraft(
            directive_id=shared,
            directive_type="citation_only",
            source_anchor="#a",
            compact_statement="A",
        ),
        DirectiveDraft(
            directive_id=shared,
            directive_type="citation_only",
            source_anchor="#b",
            compact_statement="B",
        ),
    )
    with pytest.raises(ValidationError, match="appears twice"):
        await service.register_revision(_ctx(seed), _draft(seed, directives=twins))


@pytest.mark.asyncio
async def test_a_review_date_before_the_effective_date_is_refused(
    service: ArtifactService, seed: ArcSeed
) -> None:
    """It would be expired the moment it took effect."""
    with pytest.raises(ValidationError, match="review_expires_at"):
        await service.register_revision(
            _ctx(seed), _draft(seed, review_expires_at=ARC_NOW - datetime.timedelta(days=1))
        )


@pytest.mark.asyncio
async def test_a_malformed_content_digest_is_refused(service: ArtifactService, seed: ArcSeed) -> None:
    bad_source = SourceIdentity(
        source_system="confluence",
        source_canonical_locator="conf://x",
        source_revision_locator="conf://x@1",
        content_digest="too-short",
    )
    with pytest.raises(ValidationError, match="64-character"):
        await service.register_revision(_ctx(seed), _draft(seed, source=bad_source))


@pytest.mark.asyncio
async def test_a_tenant_scoped_rule_without_a_target_tenant_is_refused(
    service: ArtifactService, seed: ArcSeed
) -> None:
    rule = ApplicabilityDraft(scope=AuthorityScope.TENANT, effective_from=ARC_NOW, target_tenant_id=None)
    with pytest.raises(ValidationError, match="requires target_tenant_id"):
        await service.register_revision(_ctx(seed), _draft(seed, rules=(rule,)))


@pytest.mark.asyncio
async def test_a_capability_scoped_rule_without_a_capability_is_refused(
    service: ArtifactService, seed: ArcSeed
) -> None:
    rule = ApplicabilityDraft(scope=AuthorityScope.CAPABILITY, effective_from=ARC_NOW, capability_ids=())
    with pytest.raises(ValidationError, match="requires at least one capability"):
        await service.register_revision(_ctx(seed), _draft(seed, rules=(rule,)))


# --- authorization ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_admin_cannot_register(service: ArtifactService, seed: ArcSeed) -> None:
    from registry.arc.service.authorization import ArcAuthorizationError

    with pytest.raises(ArcAuthorizationError):
        await service.register_revision(_ctx(seed, roles=["consumer"]), _draft(seed))


@pytest.mark.asyncio
async def test_an_admin_of_another_tenant_cannot_register(service: ArtifactService, seed: ArcSeed) -> None:
    """Elevation within a tenant never reaches across one."""
    from registry.arc.service.authorization import ArcAuthorizationError

    other = TenantContext(
        tenant_id=uuid.uuid4(), actor_id=seed.actor_id, roles=["admin"], oidc_subject="s"
    )
    ctx = ArcRequestContext.from_validated_claims(other, {"iss": "https://idp.example.test"}, host_id="h")
    with pytest.raises(ArcAuthorizationError):
        await service.register_revision(ctx, _draft(seed))


@pytest.mark.asyncio
async def test_registering_against_an_unknown_artifact_is_not_found(
    service: ArtifactService, seed: ArcSeed
) -> None:
    with pytest.raises(NotFoundError):
        await service.register_revision(_ctx(seed), _draft(seed, artifact_id=uuid.uuid4()))
