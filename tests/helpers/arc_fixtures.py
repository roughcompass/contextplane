"""Shared seeding for ARC receipt-path integration tests.

Receipt creation touches nine tables with real foreign keys between them, so
every test in this area needs the same scaffolding: a tenant, an actor, a
challenge to consume, and -- once selected rows are involved -- an artifact,
a revision, and a directive for them to point at.

Building that inline in each test file would mean four copies of the same
INSERT block, drifting apart as the schema moves. It lives here instead.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service.bundle import ContextBundle
from registry.arc.service.receipt import ReceiptProvenance, ReplayEnvelope
from registry.arc.service.signing import (
    RECEIPT_SIGNING_ALGORITHM,
    KeyPurpose,
    KeyRecord,
    ReceiptSigningProvider,
    ed25519_signer,
)
from registry.arc.types import ResolutionStatus

ARC_NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
SIGNING_KEY_ID = "rk-test-1"


def signing_provider() -> ReceiptSigningProvider:
    """An Ed25519 receipt signer backed by a freshly generated local key.

    Real deployments inject a custody-backed signer; holding raw private
    bytes in process is a test-only affordance the provider explicitly
    supports.
    """
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    records = {
        SIGNING_KEY_ID: KeyRecord(
            key_id=SIGNING_KEY_ID,
            purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
            algorithm=RECEIPT_SIGNING_ALGORITHM,
            public_key=raw_public,
        )
    }
    return ReceiptSigningProvider(
        records, active_key_id=SIGNING_KEY_ID, signer=ed25519_signer({SIGNING_KEY_ID: raw_private})
    )


def provenance() -> ReceiptProvenance:
    return ReceiptProvenance(
        selection_engine_version="arc-selection/0.1.0",
        registry_build_revision="0" * 40,
        canonical_profile_versions={"bundle": "arc_context_bundle_content_v1"},
        selection_config_digest="c" * 64,
    )


def replay_envelope() -> ReplayEnvelope:
    return ReplayEnvelope(ciphertext=b"sealed-response", nonce=b"nonce-12-byte", key_id="replay-key-1")


def ready_bundle(directive_count: int = 0) -> ContextBundle:
    return ContextBundle(
        status=ResolutionStatus.READY,
        directives=tuple({"directive_id": str(uuid.uuid4())} for _ in range(directive_count)),
        cap_facts=(),
        rendered_content_bytes=128,
        budget_limit_bytes=12288,
    )


def blocked_bundle(reason: str = "blocked_budget_exceeded") -> ContextBundle:
    return ContextBundle(
        status=ResolutionStatus.BLOCKED,
        directives=(),
        cap_facts=(),
        rendered_content_bytes=0,
        budget_limit_bytes=12288,
        blocked_reasons=(reason,),
    )


@dataclasses.dataclass(frozen=True)
class ArcSeed:
    """Identifiers for one seeded tenant's worth of ARC scaffolding."""

    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    artifact_id: uuid.UUID
    revision_id: uuid.UUID
    directive_id: uuid.UUID
    host_signer_key_id: str


async def seed_arc(factory: async_sessionmaker[AsyncSession], *, slug_prefix: str = "arc") -> ArcSeed:
    """Insert a tenant, actor, receipt-signing key, artifact, revision, and directive.

    The signing-key row is registered because `arc_receipt_events.signer_key_id`
    is a real foreign key into `arc_receipt_signing_keys` -- a signer that
    exists only in process memory would fail the event insert.
    """
    seed = ArcSeed(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        artifact_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        directive_id=uuid.uuid4(),
        host_signer_key_id=f"hk-{uuid.uuid4().hex[:12]}",
    )
    now = datetime.datetime.now(tz=datetime.UTC)

    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": seed.tenant_id, "slug": f"{slug_prefix}-{seed.tenant_id.hex[:8]}", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'arc-test-actor', :sub, :now)"
            ),
            {"aid": seed.actor_id, "tid": seed.tenant_id, "sub": f"sub-{seed.actor_id.hex[:8]}", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO arc_receipt_signing_keys ("
                "  signer_key_id, algorithm, public_key, purpose, valid_from, manifest_digest"
                ") VALUES (:kid, 'Ed25519', :pub, 'arc_receipt_event_v1', :vfrom, :digest) "
                "ON CONFLICT (signer_key_id) DO NOTHING"
            ),
            {
                "kid": SIGNING_KEY_ID,
                "pub": base64.b64encode(b"x" * 32).decode("ascii"),
                "vfrom": ARC_NOW - datetime.timedelta(days=1),
                "digest": "d" * 64,
            },
        )
        await _seed_artifact(session, seed, now)

    return seed


async def _seed_artifact(session: AsyncSession, seed: ArcSeed, now: datetime.datetime) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_artifacts (artifact_id, tenant_id, slug, kind, created_at) "
            "VALUES (:aid, :tid, :slug, 'policy', :now)"
        ),
        {"aid": seed.artifact_id, "tid": seed.tenant_id, "slug": f"a-{seed.artifact_id.hex[:8]}", "now": now},
    )
    await session.execute(
        text(
            "INSERT INTO arc_revisions ("
            "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
            "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
            "  review_expires_at, detail_audience, freshness_basis, content_classification,"
            "  content_retention_until, content_storage_mode, source_body_plaintext, created_at"
            ") VALUES ("
            "  :rid, :aid, :tid, 'test-system', :locator, :revision_locator, :digest, 'active', :efrom,"
            "  :review, 'all_matched_actors', 'revision_pinned_only', 'internal',"
            "  :retention, 'none', 'body', :now)"
        ),
        {
            "rid": seed.revision_id,
            "aid": seed.artifact_id,
            "tid": seed.tenant_id,
            # `(source_system, source_revision_locator, content_digest)` is
            # globally unique -- one upstream revision maps to one ARC
            # revision -- so every seeded revision needs its own identity.
            "locator": f"loc://{seed.revision_id.hex[:8]}",
            "revision_locator": f"loc://{seed.revision_id.hex[:8]}@1",
            "digest": seed.revision_id.hex + seed.revision_id.hex,
            "efrom": ARC_NOW - datetime.timedelta(days=1),
            "review": ARC_NOW + datetime.timedelta(days=365),
            "retention": ARC_NOW + datetime.timedelta(days=730),
            "now": now,
        },
    )
    # `arc_directives.directive_id` is a foreign key into the stable-identity
    # table: a directive keeps one identity across revisions, so the identity
    # must exist before any revision's copy of it.
    await session.execute(
        text("INSERT INTO arc_directive_identities (directive_id, artifact_id) VALUES (:did, :aid)"),
        {"did": seed.directive_id, "aid": seed.artifact_id},
    )
    # `citation_only` deliberately: any action-protecting type must carry a
    # complete conflict key and a matching `arc_conflict_domains` row, and
    # the receipt-path tests only need a directive row to point a foreign key
    # at. Seeding a conflict key here would be scaffolding that no assertion
    # reads.
    await session.execute(
        text(
            "INSERT INTO arc_directives ("
            "  directive_id, revision_id, tenant_id, directive_type,"
            "  compact_statement_plaintext, source_anchor"
            ") VALUES (:did, :rid, :tid, 'citation_only', 'Do the thing', 'anchor-1')"
        ),
        {"did": seed.directive_id, "rid": seed.revision_id, "tid": seed.tenant_id},
    )


async def seed_challenge(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    host_id: str = "host-1",
    session_id: str = "sess-1",
) -> uuid.UUID:
    """One consumable challenge. Every receipt needs exactly one."""
    challenge_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_context_challenges ("
                "  challenge_id, tenant_id, host_id, session_id, manifest_claims_digest,"
                "  arc_nonce_digest, nonce_derivation_key_id, issued_at, expires_at,"
                "  idempotency_key_digest"
                ") VALUES (:cid, :tid, :host, :sess, :claims, :nonce, 'nk1', :issued, :expires, :idem)"
            ),
            {
                "cid": challenge_id,
                "tid": tenant_id,
                "host": host_id,
                "sess": session_id,
                "claims": "a" * 64,
                "nonce": uuid.uuid4().hex + uuid.uuid4().hex,
                "issued": ARC_NOW,
                "expires": ARC_NOW + datetime.timedelta(minutes=5),
                "idem": uuid.uuid4().hex + uuid.uuid4().hex,
            },
        )
    return challenge_id


async def consume_challenge(session: AsyncSession, challenge_id: uuid.UUID) -> None:
    """Mark consumed, satisfying the deferred one-receipt-per-challenge trigger."""
    await session.execute(
        text("UPDATE arc_context_challenges SET consumed_at = :at WHERE challenge_id = :cid"),
        {"cid": challenge_id, "at": ARC_NOW},
    )
