"""Four arms, four real services, one envelope that reaches `complete`.

The unit suite proves what each arm does with rows it was handed. What needs a
database is the claim this module exists to make: that the four arms compose over
the *actual* services, against real SQL, and produce one envelope with every
block populated — not four independently plausible readers that have never been
run together.

Reaching `complete` is the assertion, and it is a strict one. Any arm that
degrades, fails, withholds an item, or trips its own bound pulls the envelope to
`degraded`, and a canonical arm that cannot answer pulls it to `blocked`. So a
single test asserting `complete` with items in all four blocks covers every way
composition can quietly go wrong, and the per-arm assertions below say which one
did if it does.

The seeding is deliberately raw SQL for ARC and hand-built for the rest. Driving
the real ARC resolution path would prove something about that path rather than
about this one, and it would couple this module to a governance flow whose own
suite already covers it.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.receipt_read import ReceiptReader
from contextplane.arc.types import ArcRequestContext
from contextplane.config import Settings
from contextplane.context.arms import ContextArms
from contextplane.context.assembler import assemble
from contextplane.context.lifecycle import LifecycleProfile
from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_NAMES,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_SUCCESS,
    BLOCK_WORKSPACE,
    ENVELOPE_COMPLETE,
)
from contextplane.context.schemas.trust import ExternalReferenceV1
from contextplane.embedding.stub import StubEmbedder
from contextplane.service.catalog.global_vocabulary import GlobalVocabularyService
from contextplane.service.memory.claim_authority import Evidence
from contextplane.service.memory.claim_ontology import seed_ontology
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.memory.consolidation import ConsolidationService
from contextplane.service.retrieval import RetrievalService
from contextplane.types import TenantContext
from contextplane.workspaces.audience import RESOLVER_EXPLICIT
from contextplane.workspaces.recall import WorkspaceRecall
from contextplane.workspaces.schemas.task_memory import ROLE_CONTRIBUTOR, TaskParticipantGrantV1
from tests.helpers.clock import FakeClock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)
_EARLIER = _NOW - datetime.timedelta(hours=2)

#: The one term every arm is asked about. Present in the seeded fact body, the
#: checkpoint goal, and nothing else, so a block that comes back populated came
#: back because the arm matched rather than because it returned everything.
_TERM = "settlement"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_and_actor(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tenant_id, "slug": f"arms-{tenant_id.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, actor_kind, created_at) "
                "VALUES (:aid, :tid, 'composer', :sub, 'service', :now)"
            ),
            {"aid": actor_id, "tid": tenant_id, "sub": f"s-{actor_id.hex[:8]}", "now": _NOW},
        )
    return tenant_id, actor_id


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["consumer"])


def _arc_ctx(ctx: TenantContext) -> ArcRequestContext:
    return ArcRequestContext(tenant=ctx, oidc_issuer="https://issuer.example", host_id="host-1")


class _NoCapabilities:
    """The visibility collaborator ARC authorization takes, answering nothing.

    Reading a receipt is authorized by tenant, actor and role alone -- capability
    visibility decides which *artifacts* an actor may see, which this path never
    asks. A stub that returned rows would make the test pass for a reason the
    production wiring does not share.
    """

    async def visible_capability_ids(
        self, ctx: ArcRequestContext, capability_ids: Sequence[uuid.UUID]
    ) -> list[uuid.UUID]:
        raise AssertionError("reading a receipt must not consult capability visibility")


# -- seeding, one block at a time ------------------------------------------


async def _seed_canonical(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> uuid.UUID:
    """One capability with one lexically-matching fact.

    The fact is what the lexical retrieval arm searches -- `facts.ts_vector` is a
    generated column over `body` -- so the entity alone would return nothing and
    the canonical block would be empty for a reason unrelated to composition.
    """
    entity_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, is_active, created_at) "
                "VALUES (:eid, :tid, 'capability', :name, 'public', TRUE, :now)"
            ),
            {"eid": entity_id, "tid": tenant_id, "name": f"payments-{entity_id.hex[:8]}", "now": _EARLIER},
        )
        await session.execute(
            text(
                "INSERT INTO facts (fact_id, tenant_id, entity_id, category, body, t_valid_from, t_ingested_at) "
                "VALUES (gen_random_uuid(), :tid, :eid, 'interface_contract', :body, :from, :from)"
            ),
            {
                "tid": tenant_id,
                "eid": entity_id,
                "body": f"the {_TERM} gateway posts in euros",
                "from": _EARLIER,
            },
        )
    return entity_id


async def _seed_claim(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    subject_entity_id: uuid.UUID,
) -> uuid.UUID:
    """One consolidated claim about the seeded entity.

    Staged and consolidated through the real services rather than inserted:
    a served claim cannot be constructed without citations, and the provenance
    rows those come from are written by this path. Inserting the claim row alone
    would produce a claim the serving service refuses to serve.
    """
    clock = FakeClock(_NOW)
    await seed_ontology(GlobalVocabularyService(factory, clock=clock))
    claim = await ClaimService(factory, clock=clock).stage_claim(
        _ctx(tenant_id, actor_id),
        subject_reference=str(subject_entity_id),
        # A predicate the shipped ontology already registers. Inventing one here
        # would be rejected before the claim reached the serving path at all.
        predicate="owned_by_team",
        value="settlement-platform",
        evidence=(Evidence(kind="session_event", ref="e1", excerpt=f"{_TERM} runs over sepa"),),
    )
    await ConsolidationService(factory, clock=clock).consolidate(claim.claim_id)
    return claim.claim_id


async def _seed_workspace(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, actor: str) -> uuid.UUID:
    """One task this actor participates in, with one checkpoint mentioning the term."""
    task_id = uuid.uuid4()
    checkpoint_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO task_checkpoints (checkpoint_id, tenant_id, task_id, sequence, predecessor_id, "
                "    goal, evidence, next_action, author, recorded_at, retention_policy, digest) "
                "VALUES (:cid, :tid, :task, 1, NULL, :goal, '[]'::jsonb, :next, :author, :now, "
                "    'standard', :digest)"
            ),
            {
                "cid": checkpoint_id,
                "tid": tenant_id,
                "task": task_id,
                # Derived from the checkpoint rather than shared: a canonical digest
                # names one checkpoint, and reusing one across rows would make the
                # fixture disagree with the contract it stands in for.
                "digest": hashlib.sha256(checkpoint_id.bytes).hexdigest(),
                "goal": f"reconcile the {_TERM} ledger",
                "next": "post the batch",
                "author": actor,
                "now": _EARLIER,
            },
        )

    await _grant(factory, tenant_id, task_id, actor)
    return task_id


async def _grant(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, task_id: uuid.UUID, actor: str
) -> None:
    from contextplane.workspaces import queries_audience as audience_q

    async with factory() as session, session.begin():
        await audience_q.insert_grant(
            session,
            tenant_id=tenant_id,
            grant=TaskParticipantGrantV1(
                task_id=task_id,
                actor_id=actor,
                role=ROLE_CONTRIBUTOR,
                # An authority, not the actor itself: the contract object refuses
                # a self-grant, which is the spoof that shape exists to prevent.
                granted_by="task-owner",
                granted_at=_EARLIER,
                expires_at=None,
                resolver_version=RESOLVER_EXPLICIT,
            ),
        )


async def _seed_arc_receipt(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> uuid.UUID:
    """One `ready` receipt naming one selected directive this caller may read.

    `all_matched_actors` audience on purpose: a narrower one would be redacted for
    a plain consumer, become an exclusion, and degrade the block -- which is the
    unit suite's case, not this one's.
    """
    artifact_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    challenge_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    directive_id = uuid.uuid4()

    # Per-seed rather than shared: a revision's source identity is unique across
    # the deployment, so a fixed locator and digest would let the first test in a
    # session seed successfully and every later one collide.
    digest = hashlib.sha256(revision_id.bytes).hexdigest()
    locator = f"rev-{revision_id.hex[:8]}"
    # One attestation may back one resolution per host, so this is per-seed too.
    attestation = f"att-{receipt_id.hex[:8]}"

    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_artifacts (artifact_id, tenant_id, slug, kind, title, "
                "    created_by_issuer, created_by_subject, created_at) "
                "VALUES (:aid, :tid, :slug, 'policy', 'Settlement policy', "
                "    'https://issuer.example', 'authoring-operator', :now)"
            ),
            {"aid": artifact_id, "tid": tenant_id, "slug": f"pol-{artifact_id.hex[:8]}", "now": _EARLIER},
        )
        await session.execute(
            text(
                "INSERT INTO arc_revisions (revision_id, artifact_id, tenant_id, source_system, "
                "    source_canonical_locator, source_revision_locator, content_digest, effective_from, "
                "    review_expires_at, detail_audience, freshness_basis, content_classification, "
                "    content_retention_until, content_storage_mode) "
                "VALUES (:rid, :aid, :tid, 'git', 'policies/settlement.md', :locator, :digest, :from, "
                "    :expires, 'all_matched_actors', 'revision_pinned_only', 'internal', :expires, 'none')"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "tid": tenant_id,
                "locator": locator,
                "digest": digest,
                "from": _EARLIER,
                "expires": _NOW + datetime.timedelta(days=90),
            },
        )
        # The selected-directives row carries a composite foreign key over
        # (revision_id, directive_id), so the directive has to exist as a real
        # identity on a real revision -- there is no way to name a directive the
        # corpus does not have, which is the constraint doing its job.
        await session.execute(
            text(
                "INSERT INTO arc_directive_identities (directive_id, artifact_id, created_at) "
                "VALUES (:did, :aid, :now)"
            ),
            {"did": directive_id, "aid": artifact_id, "now": _EARLIER},
        )
        await session.execute(
            text(
                # `citation_only` rather than `require`: an action-protecting
                # directive must carry the whole conflict-key shape, and this block
                # returns a citation to a directive rather than an obligation over
                # one, so the weaker type is the honest fixture.
                "INSERT INTO arc_directives (directive_id, revision_id, tenant_id, directive_type, "
                "    source_anchor, compact_statement_plaintext, created_at) "
                "VALUES (:did, :rid, :tid, 'citation_only', 'policies/settlement.md#L1', "
                "    'settle through the approved gateway', :now)"
            ),
            {"did": directive_id, "rid": revision_id, "tid": tenant_id, "now": _EARLIER},
        )
        await session.execute(
            text(
                "INSERT INTO arc_context_challenges (challenge_id, tenant_id, host_id, session_id, "
                "    manifest_claims_digest, arc_nonce_digest, nonce_derivation_key_id, issued_at, expires_at, "
                "    consumed_at, idempotency_key_digest) "
                # Consumed on purpose: a database trigger refuses a challenge that
                # backs a receipt without being marked consumed, which is what stops
                # one challenge from backing two resolutions.
                #
                # issued_at is stated rather than left to its `now()` default. A
                # check constraint requires expires_at > issued_at, and every other
                # column here is placed on this module's frozen clock -- so taking
                # one of the pair from the server's wall clock made the row valid
                # only while that clock stayed behind the frozen expiry, and the
                # whole fixture started failing at a date rather than at a change.
                "VALUES (:cid, :tid, 'host-1', 'sess-1', :digest, :nonce, 'key-1', :issued, :expires, "
                "    :consumed, :idem)"
            ),
            {
                "cid": challenge_id,
                "tid": tenant_id,
                "digest": digest,
                "nonce": hashlib.sha256(challenge_id.bytes).hexdigest(),
                "issued": _EARLIER,
                "expires": _NOW + datetime.timedelta(hours=1),
                "consumed": _EARLIER,
                "idem": digest,
            },
        )
        await session.execute(
            text(
                "INSERT INTO arc_receipts (receipt_id, challenge_id, tenant_id, actor_id, host_id, session_id, "
                "    manifest_fingerprint, attestation_id, resolution_status, selection_engine_version, "
                "    build_revision, canonical_profile_versions, selection_config_digest, evaluated_at, "
                "    freshness_basis, budget_limit_bytes, integrity_state, response_replay_ciphertext, "
                "    response_replay_nonce, response_replay_key_id) "
                "VALUES (:rid, :cid, :tid, :aid, 'host-1', 'sess-1', :digest, :attestation, 'ready', 'v1', "
                "    'build-1', '{}'::jsonb, :digest, :evaluated, 'revision_pinned_only', 4096, 'valid', "
                "    '\\x00'::bytea, '\\x00'::bytea, 'key-1')"
            ),
            {
                "rid": receipt_id,
                "cid": challenge_id,
                "tid": tenant_id,
                "aid": actor_id,
                "digest": digest,
                "attestation": attestation,
                "evaluated": _EARLIER,
            },
        )
        await session.execute(
            text(
                "INSERT INTO arc_receipt_selected_directives (receipt_id, revision_id, directive_id, tenant_id, "
                "    artifact_id, is_mandatory, visibility_decision_id, source_locator, "
                "    source_revision_locator, content_digest, obligation_fields, context_handle_digest) "
                "VALUES (:receipt, :rev, :did, :tid, :art, FALSE, 'vd-1', 'policies/settlement.md', "
                "    :locator, :digest, '{}'::jsonb, :digest)"
            ),
            {
                "receipt": receipt_id,
                "rev": revision_id,
                "did": directive_id,
                "tid": tenant_id,
                "art": artifact_id,
                "locator": locator,
                "digest": digest,
            },
        )
    return receipt_id


# -- the composed read -----------------------------------------------------


def _composer(factory: async_sessionmaker[AsyncSession], pg_url: str) -> ContextArms:
    """The composer as the container builds it, over the four real services."""
    settings = Settings(database_url=pg_url, pgbouncer_url=pg_url, scheduler_jobstore_url=pg_url)
    clock = FakeClock(_NOW)
    return ContextArms(
        session_factory=factory,
        retrieval=RetrievalService(session_factory=factory, clock=clock, embedder=StubEmbedder(), settings=settings),
        claims=ClaimServingService(factory, clock=clock),
        arc_receipts=ReceiptReader(factory, authorization=ArcAuthorizationService(visibility=_NoCapabilities())),
        recall=WorkspaceRecall(session_factory=factory),
    )


@pytest_asyncio.fixture
async def seeded(
    factory: async_sessionmaker[AsyncSession],
    tenant_and_actor: tuple[uuid.UUID, uuid.UUID],
) -> dict[str, Any]:
    tenant_id, actor_id = tenant_and_actor
    entity_id = await _seed_canonical(factory, tenant_id)
    claim_id = await _seed_claim(factory, tenant_id, actor_id, entity_id)
    task_id = await _seed_workspace(factory, tenant_id, str(actor_id))
    receipt_id = await _seed_arc_receipt(factory, tenant_id, actor_id)
    return {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "entity_id": entity_id,
        "claim_id": claim_id,
        "task_id": task_id,
        "receipt_id": receipt_id,
    }


async def test_the_four_arms_compose_into_one_complete_envelope(
    factory: async_sessionmaker[AsyncSession], pg_container: str, seeded: dict[str, Any]
) -> None:
    """Every block populated from its own service, and nothing degraded.

    `complete` is the strict assertion: one arm degrading, failing, withholding an
    item or tripping its bound would move the envelope off it. The per-block
    assertions that follow exist to say which arm did if that happens.
    """
    ctx = _ctx(seeded["tenant_id"], seeded["actor_id"])
    arms = _composer(factory, pg_container).for_request(
        ctx,
        query=_TERM,
        moment=_NOW,
        arc=_arc_ctx(ctx),
        arc_receipt_id=seeded["receipt_id"],
        subject_entity_id=seeded["entity_id"],
    )

    result = await assemble(arms, now=_NOW)
    envelope = result.envelope

    assert tuple(block.name for block in envelope.blocks) == BLOCK_NAMES
    for block in envelope.blocks:
        assert block.state == BLOCK_SUCCESS, f"{block.name}: {block.reason}"
        assert block.items, f"{block.name} came back with no items"
    assert envelope.state == ENVELOPE_COMPLETE
    assert envelope.quality.degraded_blocks == ()
    assert envelope.quality.cacheable


async def test_a_lifecycle_profile_changes_which_items_appear_and_not_the_envelope(
    factory: async_sessionmaker[AsyncSession], pg_container: str, seeded: dict[str, Any]
) -> None:
    """Selection narrows one block's contents; it does not reshape the answer.

    Composed against every real service rather than a fake, because the risk is
    not that the filter is wrong -- that is proved over a placement table -- but
    that threading a profile through the composer disturbs an arm that has
    nothing to do with it. Four blocks, same order, same states, same items
    everywhere except the one block placement can speak about.

    The seeded claim recorded no placement, so it survives a profile: that is
    the rule, and it also makes this a comparison between two populated
    envelopes rather than between one envelope and an empty one.
    """
    ctx = _ctx(seeded["tenant_id"], seeded["actor_id"])

    def compose(lifecycle: LifecycleProfile | None) -> dict[str, Any]:
        return _composer(factory, pg_container).for_request(
            ctx,
            query=_TERM,
            moment=_NOW,
            arc=_arc_ctx(ctx),
            arc_receipt_id=seeded["receipt_id"],
            subject_entity_id=seeded["entity_id"],
            lifecycle=lifecycle,
        )

    plain = (await assemble(compose(None), now=_NOW)).envelope
    profile = LifecycleProfile.of(
        [
            ExternalReferenceV1(
                source_system="control-plane",
                source_namespace="acme",
                kind="stage",
                external_id="implementation",
                classification="internal",
                external_authority="acme/delivery",
            )
        ]
    )
    narrowed = (await assemble(compose(profile), now=_NOW)).envelope

    assert tuple(block.name for block in narrowed.blocks) == BLOCK_NAMES
    assert narrowed.state == plain.state == ENVELOPE_COMPLETE
    for name in BLOCK_NAMES:
        assert narrowed.block(name).state == plain.block(name).state, name
        assert [item.receipt_item_id.value() for item in narrowed.block(name).items] == [
            item.receipt_item_id.value() for item in plain.block(name).items
        ], f"{name} changed under a profile that places nothing away from it"


async def test_every_non_canonical_item_carries_complete_trust_and_canonical_carries_none(
    factory: async_sessionmaker[AsyncSession], pg_container: str, seeded: dict[str, Any]
) -> None:
    """The asymmetry is the contract. An envelope holding a canonical item with
    trust metadata, or a contextual one without, is refused at construction --
    so this asserts the labels the composer actually chose rather than that one
    was chosen at all."""
    ctx = _ctx(seeded["tenant_id"], seeded["actor_id"])
    arms = _composer(factory, pg_container).for_request(
        ctx,
        query=_TERM,
        moment=_NOW,
        arc=_arc_ctx(ctx),
        arc_receipt_id=seeded["receipt_id"],
        subject_entity_id=seeded["entity_id"],
    )

    envelope = (await assemble(arms, now=_NOW)).envelope

    assert all(item.trust is None for item in envelope.block(BLOCK_CANONICAL).items)

    arc_trust = envelope.block(BLOCK_ARC).items[0].trust
    assert arc_trust is not None
    assert (arc_trust.trust, arc_trust.assertion_kind, arc_trust.mutability) == ("attested", "policy", "immutable")
    # The receipt's own instant, not the request's -- an attested resolution that
    # happened two hours ago must not read as fresh now.
    assert arc_trust.freshness == _EARLIER

    claim_trust = envelope.block(BLOCK_OBSERVED_CLAIMS).items[0].trust
    assert claim_trust is not None
    assert (claim_trust.trust, claim_trust.mutability) == ("observed", "mutable")

    workspace_trust = envelope.block(BLOCK_WORKSPACE).items[0].trust
    assert workspace_trust is not None
    assert workspace_trust.mutability == "immutable"


async def test_receipt_item_ids_are_stable_across_two_identical_resolutions(
    factory: async_sessionmaker[AsyncSession], pg_container: str, seeded: dict[str, Any]
) -> None:
    """Two resolutions over unchanged data name the same items.

    This is what makes a receipt checkable rather than decorative: a reader can
    point at a line and ask what produced it. An id derived from position instead
    of identity would pass a single-resolution test and fail this one.
    """
    ctx = _ctx(seeded["tenant_id"], seeded["actor_id"])
    composer = _composer(factory, pg_container)

    def _request() -> dict[str, Any]:
        return composer.for_request(
            ctx,
            query=_TERM,
            moment=_NOW,
            arc=_arc_ctx(ctx),
            arc_receipt_id=seeded["receipt_id"],
            subject_entity_id=seeded["entity_id"],
        )

    first = (await assemble(_request(), now=_NOW)).envelope
    second = (await assemble(_request(), now=_NOW)).envelope

    def _ids(envelope: Any) -> list[str]:
        return [item.receipt_item_id.value() for block in envelope.blocks for item in block.items]

    assert _ids(first) == _ids(second)
    assert len(set(_ids(first))) == len(_ids(first))


async def test_an_outsider_gets_no_workspace_items_and_no_hint_that_any_exist(
    factory: async_sessionmaker[AsyncSession], pg_container: str, seeded: dict[str, Any]
) -> None:
    """The audience boundary survives composition.

    Worth asserting here rather than only in the recall suite: the composer picks
    which recall arm runs and passes the actor through, and passing the wrong
    identity would open every task on the deployment while every other block
    still looked right.
    """
    outsider = _ctx(seeded["tenant_id"], uuid.uuid4())
    arms = _composer(factory, pg_container).for_request(outsider, query=_TERM, moment=_NOW)

    result = await assemble(arms, now=_NOW)
    workspace = result.envelope.block(BLOCK_WORKSPACE)
    evidence = next(e for e in result.evidence if e.block == BLOCK_WORKSPACE)

    assert workspace.items == ()
    # No exclusion either. Reporting that a task exists but is not yours is
    # exactly the discovery the boundary exists to prevent.
    assert evidence.exclusions == ()
    assert evidence.considered == 0


@pytest.mark.parametrize("term", [None, ""])
async def test_with_no_search_term_the_workspace_block_is_every_authorized_checkpoint(
    factory: async_sessionmaker[AsyncSession],
    pg_container: str,
    seeded: dict[str, Any],
    term: str | None,
) -> None:
    """The third workspace read, which has had no caller until now.

    A blank term must not reach the lexical arm: an empty needle either matches
    everything or nothing depending on how the SQL treats it, and both are wrong
    answers wearing the shape of a real one.
    """
    ctx = _ctx(seeded["tenant_id"], seeded["actor_id"])
    arm = _composer(factory, pg_container).workspace_arm(ctx, term=term, moment=_NOW)

    outcome = await arm()

    assert len(outcome.items) == 1
    assert outcome.items[0].payload["task_id"] == str(seeded["task_id"])
