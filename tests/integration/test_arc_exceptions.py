"""Approved exceptions: the most dangerous write in the subsystem.

An exception exists to make something permitted that otherwise would not
be, so every test here is about a way that could be abused.

The one that matters most: a tenant cannot except a global directive that
does not permit it. Without that, any tenant could opt itself out of
deployment-wide policy and global governance would be advisory rather than
binding.
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

from registry.arc.service.approved_exceptions import (
    ExceptionApproval,
    ExceptionDraft,
    ExceptionNotPermitted,
    ExceptionService,
)
from registry.arc.service.artifact import _conflict_subject_digest
from registry.arc.service.authorization import ArcAuthorizationError, ArcAuthorizationService
from registry.arc.types import ArcRequestContext, AuthorityScope
from registry.audit import actions
from registry.exceptions import NotFoundError, ValidationError
from registry.types import TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc
from tests.helpers.clock import FakeClock

_CONFLICT_KEY = {
    "namespace": "deploy",
    "subject_selector": "service:payments",
    "operation": "release",
    "action_class": "deploy",
    "target_selector": "production",
}
_SUBJECT_DIGEST = _conflict_subject_digest(dict(_CONFLICT_KEY))


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-exception")


class _AllVisible:
    async def visible_capability_ids(self, ctx: object, capability_ids: object) -> list[uuid.UUID]:
        return list(capability_ids)  # type: ignore[arg-type]


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession]) -> ExceptionService:
    return ExceptionService(
        factory,
        authorization=ArcAuthorizationService(visibility=_AllVisible(), global_write_allowlist=()),
        clock=FakeClock(ARC_NOW),
    )


def _ctx(seed: ArcSeed, *, tenant_id: uuid.UUID | None = None, roles: list[str] | None = None) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=tenant_id or seed.tenant_id,
        actor_id=seed.actor_id,
        roles=roles if roles is not None else ["admin"],
        oidc_subject="s",
    )
    return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"}, host_id="h")


async def _seed_directive(
    factory: async_sessionmaker[AsyncSession],
    seed: ArcSeed,
    *,
    delegable: bool,
    tenant_scoped: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A directive to be excepted, with its conflict domain and revision.

    `tenant_scoped=False` makes it deployment-wide (NULL tenant), which is
    the case the delegability rule actually protects.
    """
    artifact_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    directive_id = uuid.uuid4()
    tenant = seed.tenant_id if tenant_scoped else None

    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_conflict_domains (conflict_subject_digest, conflict_subject_key) "
                "VALUES (:d, CAST(:k AS JSONB)) ON CONFLICT DO NOTHING"
            ),
            {"d": _SUBJECT_DIGEST, "k": json.dumps(_CONFLICT_KEY, sort_keys=True)},
        )
        await session.execute(
            text(
                "INSERT INTO arc_artifacts (artifact_id, tenant_id, slug, kind, created_at) "
                "VALUES (:aid, :tid, :slug, 'policy', :now)"
            ),
            {"aid": artifact_id, "tid": tenant, "slug": f"a-{artifact_id.hex[:8]}", "now": ARC_NOW},
        )
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, source_body_plaintext,"
                "  source_body_ciphertext, source_body_nonce, source_body_wrapped_dek,"
                "  content_key_id, content_encryption_profile, created_at"
                ") VALUES (:rid, :aid, :tid, 'test', :loc, :rloc, :digest, 'active', :efrom,"
                "          :review, 'all_matched_actors', 'revision_pinned_only', 'internal',"
                "          :retention, :storage, :body, :body_ciphertext, :nonce, :dek,"
                "          :key_id, :profile, :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "tid": tenant,
                "loc": f"loc://{revision_id.hex[:8]}",
                "rloc": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": ARC_NOW,
                "review": ARC_NOW + datetime.timedelta(days=365),
                "retention": ARC_NOW + datetime.timedelta(days=730),
                # Global-scope content may not be stored as plaintext — the
                # schema enforces that deployment-wide governance is
                # encrypted at rest, so the seed has to respect it rather
                # than work around it.
                "storage": "none" if tenant is not None else "encrypted",
                "body": "body" if tenant is not None else None,
                # An encrypted envelope must be complete: the schema
                # requires nonce, wrapped DEK, and key id alongside the
                # ciphertext, which is exactly what the content-protection
                # service produces.
                "body_ciphertext": None if tenant is not None else b"sealed-body",
                "nonce": None if tenant is not None else b"nonce-12-byt",
                "dek": None if tenant is not None else b"wrapped-dek",
                "key_id": None if tenant is not None else "content-key-1",
                "profile": None if tenant is not None else "arc_content_envelope_v1",
                "now": ARC_NOW,
            },
        )
        await session.execute(
            text("INSERT INTO arc_directive_identities (directive_id, artifact_id) VALUES (:did, :aid)"),
            {"did": directive_id, "aid": artifact_id},
        )
        await session.execute(
            text(
                "INSERT INTO arc_directives ("
                "  directive_id, revision_id, tenant_id, directive_type, compact_statement_plaintext,"
                "  compact_statement_ciphertext, source_anchor, conflict_key_schema_version,"
                "  conflict_subject_digest,"
                "  conflict_key_namespace, conflict_key_subject_selector, conflict_key_operation,"
                "  conflict_key_action_class, conflict_key_target_selector, conflict_key_modality,"
                "  conflict_key_constraint_operator, conflict_key_constraint_value, delegable_exception"
                ") VALUES (:did, :rid, :tid, 'require', :statement, :ciphertext, '#p',"
                "          'arc_conflict_v1', :subject, :ns, :sel, :op, :act, :tgt,"
                "          'require', 'equals', 'reviewed', :delegable)"
            ),
            {
                "did": directive_id,
                "rid": revision_id,
                "tid": tenant,
                "subject": _SUBJECT_DIGEST,
                "ns": _CONFLICT_KEY["namespace"],
                "sel": _CONFLICT_KEY["subject_selector"],
                "op": _CONFLICT_KEY["operation"],
                "act": _CONFLICT_KEY["action_class"],
                "tgt": _CONFLICT_KEY["target_selector"],
                # Same rule as the revision body: global-scope directive
                # prose may not be stored as plaintext.
                # Exactly one representation, and global-scope prose must be
                # the encrypted one -- the schema requires both facts.
                "statement": "Deploys require review." if tenant is not None else None,
                "ciphertext": None if tenant is not None else b"sealed-directive-prose",
                "delegable": delegable,
            },
        )
    return directive_id, revision_id


async def _seed_verifier(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> str:
    """The verifier the approval evidence attests through.

    Only this is seeded: the evidence row itself is written by the service,
    in the same transaction as the exception it approves, because the two
    reference each other and the schema's deferrable foreign keys exist for
    exactly that pair.
    """
    verifier_id = f"v-{uuid.uuid4().hex[:12]}"
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_approval_verifiers ("
                "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind,"
                "  scope_tenant_id, provider_id, valid_from"
                ") VALUES (:vid, 'trusted_attestation_provider', ARRAY['exception_approval'],"
                "          'tenant', :tid, 'in-process-test', :from)"
            ),
            {"vid": verifier_id, "tid": seed.tenant_id, "from": ARC_NOW - datetime.timedelta(days=1)},
        )
    return verifier_id


def _approval(verifier_id: str) -> ExceptionApproval:
    return ExceptionApproval(
        evidence_id=uuid.uuid4(),
        approval_verifier_id=verifier_id,
        approving_principal="operator@example.test",
        approving_role="governance_owner",
        approved_payload_digest=uuid.uuid4().hex + uuid.uuid4().hex,
        audit_log_reference="audit://exception/1",
        approval_timestamp=ARC_NOW,
    )


def _draft(directive_id: uuid.UUID, revision_id: uuid.UUID, verifier_id: str, **overrides: object):
    base: dict[str, object] = {
        "higher_scope_directive_id": directive_id,
        "higher_scope_revision_id": revision_id,
        "lower_scope_kind": AuthorityScope.TENANT,
        "replacement_conflict_descriptor": {
            "conflict_subject_digest": _SUBJECT_DIGEST,
            "modality": "require",
            "constraint_operator": "in_set",
            "constraint_value": "reviewed,expedited",
        },
        "approval": _approval(verifier_id),
        "effective_from": ARC_NOW,
        "exception_statement": "Expedited review accepted for this tenant.",
        "justification": "Incident response requires an expedited path, approved by governance.",
    }
    base.update(overrides)
    return ExceptionDraft(**base)  # type: ignore[arg-type]


# --- the rule that keeps global governance binding ----------------------------------


@pytest.mark.asyncio
async def test_a_tenant_cannot_except_a_non_delegable_global_directive(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """The single most important assertion in this file.

    Without it any tenant could opt itself out of deployment-wide policy,
    and global governance would be advisory rather than binding.
    """
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=False, tenant_scoped=False)
    verifier_id = await _seed_verifier(factory, seed)

    with pytest.raises(ExceptionNotPermitted, match="does not permit exceptions"):
        await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id))


@pytest.mark.asyncio
async def test_a_tenant_can_except_a_delegable_global_directive(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """The control. Delegation has to mean something, or the flag would be
    decoration and every global rule absolute."""
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True, tenant_scoped=False)
    verifier_id = await _seed_verifier(factory, seed)

    assert await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id))


@pytest.mark.asyncio
async def test_a_non_delegable_tenant_directive_also_cannot_be_excepted(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """Delegability is a property of the directive, not of who owns it."""
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=False)
    verifier_id = await _seed_verifier(factory, seed)

    with pytest.raises(ExceptionNotPermitted):
        await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id))


@pytest.mark.asyncio
async def test_another_tenants_directive_is_not_found(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """Reported as absent rather than forbidden: distinguishing them would
    confirm the directive exists."""
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)

    with pytest.raises(NotFoundError):
        await service.approve_exception(
            _ctx(seed, tenant_id=uuid.uuid4()), _draft(directive_id, revision_id, verifier_id)
        )


# --- the exception is recorded correctly ---------------------------------------------


@pytest.mark.asyncio
async def test_an_approved_exception_is_filed_under_the_requesting_tenant(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """Taken from the authenticated context, never the body: an exception a
    caller could file against another tenant would be a way to weaken
    somebody else's rules."""
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)
    exception_id = await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id))

    async with factory() as session:
        tenant_id = (
            await session.execute(
                text("SELECT lower_scope_tenant_id FROM arc_approved_exceptions WHERE exception_id = :eid"),
                {"eid": exception_id},
            )
        ).scalar_one()
    assert tenant_id == seed.tenant_id


@pytest.mark.asyncio
async def test_approval_emits_an_audit_row(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)
    exception_id = await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id))

    async with factory() as session:
        event_type = (
            await session.execute(
                text("SELECT event_type FROM arc_audit_outbox " "WHERE event_payload ->> 'exception_id' = :eid"),
                {"eid": str(exception_id)},
            )
        ).scalar_one()
    assert event_type == actions.ARC_EXCEPTION_APPROVED


# --- the replacement must actually narrow the same rule --------------------------------


@pytest.mark.asyncio
async def test_a_replacement_for_a_different_conflict_subject_is_refused(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """An exception whose replacement addressed something else would sit
    beside the directive rather than narrowing it — leaving the original in
    force while appearing to have relaxed it."""
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)
    draft = _draft(
        directive_id,
        revision_id,
        verifier_id,
        replacement_conflict_descriptor={"conflict_subject_digest": "f" * 64},
    )

    with pytest.raises(ValidationError, match="different conflict subject"):
        await service.approve_exception(_ctx(seed), draft)


@pytest.mark.asyncio
async def test_a_global_scoped_exception_is_refused(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """An exception narrows by definition. A global one would be an
    amendment to the directive, which belongs on the registration path."""
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)

    with pytest.raises(ValidationError, match="cannot be global"):
        await service.approve_exception(
            _ctx(seed),
            _draft(directive_id, revision_id, verifier_id, lower_scope_kind=AuthorityScope.GLOBAL),
        )


@pytest.mark.asyncio
async def test_an_exception_without_justification_is_refused(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """An unexplained weakening is not auditable."""
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)

    with pytest.raises(ValidationError, match="justification"):
        await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id, justification="   "))


@pytest.mark.asyncio
async def test_a_capability_scoped_exception_requires_a_capability(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)

    with pytest.raises(ValidationError, match="lower_scope_capability_id"):
        await service.approve_exception(
            _ctx(seed),
            _draft(directive_id, revision_id, verifier_id, lower_scope_kind=AuthorityScope.CAPABILITY),
        )


@pytest.mark.asyncio
async def test_an_expiry_before_the_effective_date_is_refused(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)

    with pytest.raises(ValidationError, match="effective_until"):
        await service.approve_exception(
            _ctx(seed),
            _draft(
                directive_id,
                revision_id,
                verifier_id,
                effective_until=ARC_NOW - datetime.timedelta(days=1),
            ),
        )


# --- revocation -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoking_an_exception_restores_the_directive(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)
    exception_id = await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id))

    await service.revoke_exception(_ctx(seed), exception_id, reason="no longer needed")

    async with factory() as session:
        revoked_at = (
            await session.execute(
                text("SELECT revoked_at FROM arc_approved_exceptions WHERE exception_id = :eid"),
                {"eid": exception_id},
            )
        ).scalar_one()
    assert revoked_at is not None


@pytest.mark.asyncio
async def test_another_tenant_cannot_revoke_an_exception(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """Scoped in the predicate rather than checked afterwards: a revocation
    that could name another tenant's exception would be a way to remove
    their approved relief."""
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)
    exception_id = await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id))

    with pytest.raises(NotFoundError):
        await service.revoke_exception(_ctx(seed, tenant_id=uuid.uuid4()), exception_id, reason="not mine")


@pytest.mark.asyncio
async def test_revoking_twice_is_reported_as_not_found(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)
    exception_id = await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id))
    await service.revoke_exception(_ctx(seed), exception_id, reason="once")

    with pytest.raises(NotFoundError):
        await service.revoke_exception(_ctx(seed), exception_id, reason="twice")


@pytest.mark.asyncio
async def test_the_evidence_is_written_with_the_exception_not_before_it(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """The pair is atomic.

    The evidence names the exception it approved and the exception names the
    evidence approving it; the schema makes both foreign keys deferrable so
    one transaction can insert either order. Splitting them would allow an
    exception with no approval, or evidence pointing at an exception that
    was never created.
    """
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)
    verifier_id = await _seed_verifier(factory, seed)
    exception_id = await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id))

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT e.evidence_type, e.approved_exception_id, x.approval_evidence_id "
                    "FROM arc_approved_exceptions x "
                    "JOIN arc_approval_evidence e ON e.evidence_id = x.approval_evidence_id "
                    "WHERE x.exception_id = :eid"
                ),
                {"eid": exception_id},
            )
        ).one()

    assert row.evidence_type == "exception_approval"
    # Each names the other.
    assert row.approved_exception_id == exception_id


@pytest.mark.asyncio
async def test_a_failed_approval_leaves_no_orphan_evidence(
    factory: async_sessionmaker[AsyncSession], service: ExceptionService, seed: ArcSeed
) -> None:
    """Evidence for an exception that was refused would be a record of an
    approval that never took effect."""
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=False)
    verifier_id = await _seed_verifier(factory, seed)

    async with factory() as session:
        before = (await session.execute(text("SELECT count(*) FROM arc_approval_evidence"))).scalar_one()

    with pytest.raises(ExceptionNotPermitted):
        await service.approve_exception(_ctx(seed), _draft(directive_id, revision_id, verifier_id))

    async with factory() as session:
        after = (await session.execute(text("SELECT count(*) FROM arc_approval_evidence"))).scalar_one()
    assert after == before


# --- approving an exception is a governance write, not a read ---------------------


@pytest.mark.asyncio
async def test_a_non_admin_cannot_approve_an_exception(
    service: ExceptionService, factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Approving an exception weakens a control, so it needs write authority.

    The service used to call only `assert_request_tenant`, which rejects the
    reserved deployment tenant and checks nothing else, and the HTTP route adds
    no role gate of its own. So any authenticated actor of any role could
    approve an exception narrowing any delegable directive for their tenant --
    the one write in this subsystem whose entire purpose is to permit something
    that otherwise would not be.
    """
    verifier_id = await _seed_verifier(factory, seed)
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)

    with pytest.raises(ArcAuthorizationError):
        await service.approve_exception(_ctx(seed, roles=["consumer"]), _draft(directive_id, revision_id, verifier_id))


@pytest.mark.asyncio
async def test_an_auditor_cannot_approve_an_exception_either(
    service: ExceptionService, factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Read-only roles stay read-only. An auditor may read a tenant's receipts
    and governance; granting relief from it is a different act."""
    verifier_id = await _seed_verifier(factory, seed)
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)

    with pytest.raises(ArcAuthorizationError):
        await service.approve_exception(_ctx(seed, roles=["auditor"]), _draft(directive_id, revision_id, verifier_id))


@pytest.mark.asyncio
async def test_an_unknown_verifier_is_reported_as_not_found_not_a_crash(
    service: ExceptionService, factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """A verifier that does not exist must be a 404, not a 500.

    `_assert_verifier_is_trusted` used to return silently for an absent
    verifier, on the reasoning that the foreign key would report it more
    precisely. It does not: the constraint is immediate and not in the
    deferrable set, so the insert raises `IntegrityError`, and the admin
    router's `_translate` has no branch for it and returns the exception
    unmapped -- an unhandled 500.

    That matters more than it looks, because nothing can currently register a
    verifier at all, so *every* exception approval reaching a real deployment
    took this path.
    """
    directive_id, revision_id = await _seed_directive(factory, seed, delegable=True)

    with pytest.raises(NotFoundError, match="verifier"):
        await service.approve_exception(
            _ctx(seed), _draft(directive_id, revision_id, f"v-does-not-exist-{uuid.uuid4().hex[:8]}")
        )
