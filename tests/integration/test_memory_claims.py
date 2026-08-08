"""Staging a claim: typed, linked, provenanced — or refused with a reason.

The write path is the only thing that creates claims, so everything a claim
promises is a property of these checks and nothing else.

The rejections matter as much as the happy path. A store whose extraction has
quietly stopped conforming looks identical to one that is working, unless every
refusal is categorized and counted.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.audit import actions
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.catalog.global_vocabulary import GlobalVocabularyService
from contextplane.service.memory.claim_authority import (
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OBSERVER_INFERENCE,
    AUTHORITY_OWNER_EXTRACTION,
    AUTHORITY_OWNER_HUMAN,
    AUTHORITY_OWNER_INFERENCE,
    AUTHORITY_UNATTRIBUTED,
    REJECT_DEPRECATED_PREDICATE,
    REJECT_EVIDENCE_KIND,
    REJECT_NULL_VALUE,
    REJECT_UNKNOWN_PREDICATE,
    REJECT_VALUE_TYPE,
    REJECT_VISIBILITY,
    REJECTION_REASONS,
    SOURCE_AUTHORITY_RANK,
    STATUS_STAGED,
    STATUS_UNLINKED,
    ClaimRejected,
    Evidence,
)
from contextplane.service.memory.claim_ontology import seed_ontology
from contextplane.service.memory.claim_writer import ClaimService
from tests.helpers.clock import FakeClock
from tests.helpers.context import claim_admin_ctx, tenant_context
from tests.helpers.context import claim_producer_ctx as _ctx

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)
_EV = (Evidence(kind="session_event", ref="evt-1", excerpt="it depends on billing"),)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def ontology(factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"clm-{tid.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return tid, aid


async def _seed_entity(
    factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, *, visibility: str = "tenant-shared"
) -> uuid.UUID:
    eid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, "
                "                      visibility, is_active, created_at) "
                "VALUES (:eid, :tid, 'capability', :name, :vis, TRUE, :now)"
            ),
            {"eid": eid, "tid": tid, "name": f"cap-{eid.hex[:8]}", "vis": visibility, "now": _NOW},
        )
    return eid


async def _map_external_id(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    eid: uuid.UUID,
    *,
    system: str,
    external: str,
) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entity_external_ids (external_id_pk, entity_id, tenant_id, "
                "        external_system_slug, external_id, created_at, updated_at) "
                "VALUES (:pk, :eid, :tid, :sys, :ext, :now, :now)"
            ),
            {
                "pk": uuid.uuid4(),
                "eid": eid,
                "tid": tid,
                "sys": system,
                "ext": external,
                "now": _NOW,
            },
        )


@pytest.fixture
def claims(factory: async_sessionmaker[AsyncSession]) -> ClaimService:
    return ClaimService(factory, clock=FakeClock(_NOW))


# --- exit 1: a conforming claim ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_conforming_claim_lands_staged_and_linked(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    target = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="depends_on",
        value=str(target),
        evidence=_EV,
    )

    assert claim.status == STATUS_STAGED
    assert claim.subject_entity_id == subject
    assert claim.owning_tenant_id == tid


@pytest.mark.asyncio
async def test_provenance_resolves_in_both_directions(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The reverse direction is the one an array column could not index, and
    the one an erasure request needs: given this evidence, what did we derive?"""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="evt-42", excerpt="the platform team owns it"),),
    )

    async with factory() as session:
        forward = (
            await session.execute(
                text(
                    "SELECT evidence_kind, evidence_ref, evidence_excerpt "
                    "FROM memory_claim_provenance WHERE claim_id = :cid"
                ),
                {"cid": claim.claim_id},
            )
        ).all()
        reverse = (
            await session.execute(
                text(
                    "SELECT claim_id FROM memory_claim_provenance "
                    "WHERE evidence_kind = 'session_event' AND evidence_ref = 'evt-42'"
                )
            )
        ).all()

    assert [(r.evidence_kind, r.evidence_ref) for r in forward] == [("session_event", "evt-42")]
    assert [r.claim_id for r in reverse] == [claim.claim_id]
    assert forward[0].evidence_excerpt == "the platform team owns it"


@pytest.mark.asyncio
async def test_a_claim_without_provenance_is_refused(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """An unverifiable assertion in a store whose premise is trust is worse
    than no assertion."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    with pytest.raises(ValidationError, match="provenance"):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value="platform",
            evidence=(),
        )


# --- exit 2 and 4: conformance rejections ---------------------------------------------


@pytest.mark.asyncio
async def test_prose_where_a_duration_is_declared_is_refused(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The rejection the requirement names. A predicate declaring seconds and
    receiving a sentence produces a row that looks like every other row and
    can be reasoned with by nothing."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="recovery_time_objective_seconds",
            value="about fifteen minutes",
            evidence=_EV,
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


@pytest.mark.asyncio
async def test_a_predicate_outside_the_ontology_is_refused(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="vibes_with",
            value="x",
            evidence=_EV,
        )
    assert exc.value.reason == REJECT_UNKNOWN_PREDICATE


@pytest.mark.asyncio
async def test_adding_the_predicate_makes_the_same_claim_conform(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Exit scenario 4's second half: the ontology is the thing that decides,
    so extending it is what makes a previously-invalid claim valid."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    name = f"pred_{uuid.uuid4().hex[:8]}"
    await GlobalVocabularyService(factory, clock=FakeClock(_NOW)).create_predicate(
        value=name, value_type="string", claim_category="dependency", definition="new term"
    )

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate=name,
        value="ok",
        evidence=_EV,
    )
    assert claim.status == STATUS_STAGED


@pytest.mark.asyncio
async def test_a_deprecated_predicate_accepts_no_new_claims(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    globals_ = GlobalVocabularyService(factory, clock=FakeClock(_NOW))
    name = f"pred_{uuid.uuid4().hex[:8]}"
    await globals_.create_predicate(value=name, value_type="string", claim_category="dependency", definition="x")
    await globals_.deprecate_predicate(value=name)

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, aid), subject_reference=str(subject), predicate=name, value="x", evidence=_EV
        )
    assert exc.value.reason == REJECT_DEPRECATED_PREDICATE


@pytest.mark.asyncio
async def test_null_is_never_a_value(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """An unknown is the absence of a claim, not a claim of nothing. Storing
    both as the same row makes them indistinguishable forever after."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value=None,
            evidence=_EV,
        )
    assert exc.value.reason == REJECT_NULL_VALUE


@pytest.mark.asyncio
async def test_a_boolean_is_not_an_integer(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """`bool` subclasses `int` in Python, so True would otherwise store as 1
    under a predicate meaning seconds."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="recovery_time_objective_seconds",
            value=True,
            evidence=_EV,
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


@pytest.mark.asyncio
async def test_a_timestamp_with_an_offset_is_rejected_not_converted(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Converting silently loses which zone was meant, and a timestamp whose
    zone was guessed is worse than one refused."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="decided_at",
            value="2026-08-03T12:00:00+02:00",
            evidence=_EV,
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


# --- exit 3: unresolvable subjects -----------------------------------------------------


@pytest.mark.asyncio
async def test_an_unresolvable_subject_stores_unlinked_rather_than_dropping(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Not dropped and not guessed. Dropping loses information nobody knows is
    missing; guessing attaches an assertion to the wrong entity, which then
    looks corroborated by something unrelated."""
    tid, aid = await _seed_tenant(factory)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/not-in-the-catalog",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    assert claim.status == STATUS_UNLINKED
    assert claim.subject_entity_id is None
    assert claim.owning_tenant_id is None


@pytest.mark.asyncio
async def test_an_unlinked_claim_keeps_what_it_was_about(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The curator needs the original reference to link or discard it."""
    tid, aid = await _seed_tenant(factory)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with factory() as session:
        stored = (
            await session.execute(
                text("SELECT subject_reference, status FROM memory_claims WHERE claim_id = :cid"),
                {"cid": claim.claim_id},
            )
        ).one()
    assert stored.subject_reference == "github:acme/mystery"
    assert stored.status == STATUS_UNLINKED


@pytest.mark.asyncio
async def test_an_unlinked_claim_is_excluded_from_the_subject_lookup(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Excluded from scoring, consolidation, promotion and serving — all of
    which start from the staged-subject index."""
    tid, aid = await _seed_tenant(factory)
    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with factory() as session:
        staged = (
            await session.execute(
                text("SELECT count(*) FROM memory_claims " "WHERE author_tenant_id = :tid AND status = 'staged'"),
                {"tid": tid},
            )
        ).scalar_one()
    assert staged == 0


@pytest.mark.asyncio
async def test_a_mapped_external_id_resolves_to_its_entity(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Extraction sees `github:acme/billing`, not a UUID. If that did not
    resolve, everything a connector or a session produced would land unlinked."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    await _map_external_id(factory, tid, subject, system="github", external="acme/billing")

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/billing",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    assert claim.status == STATUS_STAGED
    assert claim.subject_entity_id == subject


@pytest.mark.asyncio
async def test_another_tenants_external_id_mapping_does_not_resolve(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """An external id means something only inside the mapping that defined it.
    Resolving across tenants would let one tenant's naming attach claims to
    another's entities."""
    owner_tid, _ = await _seed_tenant(factory)
    author_tid, author_aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, owner_tid)
    await _map_external_id(factory, owner_tid, subject, system="github", external="acme/private")

    claim = await claims.stage_claim(
        _ctx(author_tid, author_aid),
        subject_reference="github:acme/private",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    assert claim.status == STATUS_UNLINKED
    assert claim.subject_entity_id is None


# --- exit 6 and 7: visibility and ownership ---------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_cannot_be_more_visible_than_its_subject(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid, visibility="private")

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value="platform",
            evidence=_EV,
            visibility="public",
        )
    assert exc.value.reason == REJECT_VISIBILITY


@pytest.mark.asyncio
async def test_visibility_defaults_to_the_subjects_own(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid, visibility="private")

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert claim.visibility == "private"


@pytest.mark.asyncio
async def test_the_owning_tenant_is_the_subjects_not_the_authors(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A claim about somebody else's capability is governed by them. Deriving
    the owner from the author would let any tenant claim governance over
    assertions about another's entities."""
    owner_tid, _ = await _seed_tenant(factory)
    author_tid, author_aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, owner_tid, visibility="public")

    claim = await claims.stage_claim(
        _ctx(author_tid, author_aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="someone else",
        evidence=_EV,
    )

    assert claim.owning_tenant_id == owner_tid
    assert claim.owning_tenant_id != author_tid


# --- source authority: derived, never declared -----------------------------------------


async def _seed_actor(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, *, kind: str) -> uuid.UUID:
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                "                    actor_kind, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, :kind, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "kind": kind, "now": _NOW},
        )
    return aid


async def _seed_sync_run(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, *, source_type: str) -> uuid.UUID:
    source_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name, "
                "                          config, is_active, created_at) "
                "VALUES (:sid, :tid, :stype, 'src', '{}'::jsonb, TRUE, :now)"
            ),
            {"sid": source_id, "tid": tid, "stype": source_type, "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO sync_runs (sync_run_id, tenant_id, source_id, status, "
                "                        trigger, started_at) "
                "VALUES (:rid, :tid, :sid, 'done', 'manual', :now)"
            ),
            {"rid": run_id, "tid": tid, "sid": source_id, "now": _NOW},
        )
    return run_id


@pytest.mark.asyncio
async def test_the_caller_cannot_declare_its_own_authority(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The signature has no authority parameter. A producer that could name its
    own would name the highest one, which is the whole reason this is derived."""
    import inspect

    params = inspect.signature(claims.stage_claim).parameters
    assert "source_authority" not in params
    assert "derivation" not in params


@pytest.mark.asyncio
async def test_an_owners_deterministic_connector_earns_the_top_machine_tier(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A registered connector's parse is a pure function of the fetched bytes:
    re-fetch, re-parse, same triple. That reproducibility earns the tier."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    run = await _seed_sync_run(factory, tid, source_type="openapi")

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="connector_run", ref=str(run)),),
    )
    assert claim.source_authority == AUTHORITY_OWNER_EXTRACTION


@pytest.mark.asyncio
async def test_a_session_event_is_inference_not_extraction(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The artefact is real, but the step from it to a typed triple is a model
    reading text. That is not reproducible and must not rank as if it were."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert claim.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_an_unregistered_source_type_does_not_earn_the_extraction_tier(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Reproducibility comes from the connector contract, not from the evidence
    kind. A source type nobody registered has made no such promise."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    run = await _seed_sync_run(factory, tid, source_type="scraped_wiki")

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="connector_run", ref=str(run)),),
    )
    assert claim.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_an_unresolvable_connector_ref_costs_authority_rather_than_buying_it(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The caller supplies a pointer; the write path decides what it is worth.
    A bad ref must land at the floor, never at the tier it names."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="connector_run", ref="not-a-uuid"),),
    )
    assert claim.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_another_tenants_sync_run_does_not_earn_the_extraction_tier(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Citing a run you did not perform is exactly the forgery the derivation
    exists to prevent."""
    owner_tid, _ = await _seed_tenant(factory)
    author_tid, author_aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, author_tid)
    run = await _seed_sync_run(factory, owner_tid, source_type="openapi")

    claim = await claims.stage_claim(
        _ctx(author_tid, author_aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="connector_run", ref=str(run)),),
    )
    assert claim.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_mixed_evidence_takes_the_weakest_link_not_the_strongest(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Max would be a privilege-escalation primitive: attach one connector run
    to a model inference and it outranks the owner's real parse. Corroboration
    raises confidence, never authority."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    run = await _seed_sync_run(factory, tid, source_type="openapi")

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(
            Evidence(kind="connector_run", ref=str(run)),
            Evidence(kind="session_event", ref="evt-9", excerpt="someone said so"),
        ),
    )
    assert claim.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_the_provenance_row_records_which_evidence_set_the_floor(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Authority is a minimum, so the authority alone does not say why. An
    auditor needs the per-row tier to reconstruct the decision."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    run = await _seed_sync_run(factory, tid, source_type="openapi")

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(
            Evidence(kind="connector_run", ref=str(run)),
            Evidence(kind="session_event", ref="evt-9"),
        ),
    )

    async with factory() as session:
        rows = dict(
            (
                await session.execute(
                    text("SELECT evidence_kind, derivation FROM memory_claim_provenance " "WHERE claim_id = :cid"),
                    {"cid": claim.claim_id},
                )
            ).all()
        )
    assert rows == {"connector_run": "extraction", "session_event": "inference"}


@pytest.mark.asyncio
async def test_a_human_curator_raises_authority_rather_than_averaging(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A person putting their name to a model extraction asserts it in the
    first person. Under a plain minimum, confirmation would mean nothing."""
    tid, _ = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(
            Evidence(kind="session_event", ref="evt-1"),
            Evidence(kind="curator", ref=str(human), excerpt="confirmed"),
        ),
    )
    assert claim.source_authority == AUTHORITY_OWNER_HUMAN


@pytest.mark.asyncio
async def test_a_service_principal_cannot_produce_curator_evidence(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Otherwise the human tier is reachable by any worker that names it, and
    the confirmation tier becomes a self-declaration after all."""
    tid, _ = await _seed_tenant(factory)
    worker = await _seed_actor(factory, tid, kind="sync_worker")
    subject = await _seed_entity(factory, tid)

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, worker),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value="platform",
            evidence=(Evidence(kind="curator", ref=str(worker)),),
        )
    assert exc.value.reason == REJECT_EVIDENCE_KIND


@pytest.mark.asyncio
async def test_a_non_owner_human_never_outranks_an_owners_machine_claim(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The flattening is lossless only because ownership dominates derivation.
    A non-owner human overriding the owner's parse would invert the rule that
    only owners assert authoritative facts about their own capability."""
    owner_tid, owner_aid = await _seed_tenant(factory)
    observer_tid, _ = await _seed_tenant(factory)
    observer_human = await _seed_actor(factory, observer_tid, kind="human")
    subject = await _seed_entity(factory, owner_tid, visibility="public")
    run = await _seed_sync_run(factory, owner_tid, source_type="openapi")

    owner_claim = await claims.stage_claim(
        _ctx(owner_tid, owner_aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="connector_run", ref=str(run)),),
    )
    observer_claim = await claims.stage_claim(
        _ctx(observer_tid, observer_human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="somebody else",
        evidence=(Evidence(kind="curator", ref=str(observer_human)),),
    )

    assert observer_claim.source_authority == AUTHORITY_OBSERVER_HUMAN
    assert SOURCE_AUTHORITY_RANK[owner_claim.source_authority] < SOURCE_AUTHORITY_RANK[observer_claim.source_authority]


@pytest.mark.asyncio
async def test_an_unlinked_claim_is_unattributed_not_an_observer(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """There is no owner to compare the author against, so standing is
    undefined rather than low. An observer tier would assert a determination
    nobody made, and nothing would mark it stale once curation links it."""
    tid, aid = await _seed_tenant(factory)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert claim.source_authority == AUTHORITY_UNATTRIBUTED


@pytest.mark.asyncio
async def test_unattributed_loses_every_comparison(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """It is the floor, so if it ever leaks into conflict resolution it cannot
    win."""
    floor = SOURCE_AUTHORITY_RANK[AUTHORITY_UNATTRIBUTED]
    assert floor == max(SOURCE_AUTHORITY_RANK.values())


@pytest.mark.asyncio
async def test_every_owner_tier_outranks_every_observer_tier(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Ownership-major is the property that makes one ordinal safe. If any
    observer tier ever outranked an owner tier, rank comparison would silently
    start authorizing cross-tenant overrides."""
    owners = [r for v, r in SOURCE_AUTHORITY_RANK.items() if v.startswith("owner_")]
    observers = [r for v, r in SOURCE_AUTHORITY_RANK.items() if v.startswith("observer_")]
    assert max(owners) < min(observers)


@pytest.mark.asyncio
async def test_the_stored_authority_matches_what_was_returned(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The CHECK constraint is the backstop; this confirms the value reaching
    it is the derived one and not a default the column supplied."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with factory() as session:
        stored = (
            await session.execute(
                text("SELECT source_authority FROM memory_claims WHERE claim_id = :cid"),
                {"cid": claim.claim_id},
            )
        ).scalar_one()
    assert stored == claim.source_authority == AUTHORITY_OWNER_INFERENCE


# --- the subject read goes through the cross-tenant chokepoint ---------------------------


@pytest.mark.asyncio
async def test_a_private_entity_of_another_tenant_does_not_resolve(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A direct read would answer "does this id exist, and who owns it" for
    every entity in the deployment to anyone who can guess a UUID. An invisible
    subject must be indistinguishable from a missing one."""
    owner_tid, _ = await _seed_tenant(factory)
    author_tid, author_aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, owner_tid, visibility="private")

    claim = await claims.stage_claim(
        _ctx(author_tid, author_aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    assert claim.status == STATUS_UNLINKED
    assert claim.subject_entity_id is None
    assert claim.owning_tenant_id is None


@pytest.mark.asyncio
async def test_an_invisible_subject_is_indistinguishable_from_an_absent_one(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Same observable result for both, which is what makes it not an oracle."""
    owner_tid, _ = await _seed_tenant(factory)
    author_tid, author_aid = await _seed_tenant(factory)
    hidden = await _seed_entity(factory, owner_tid, visibility="private")

    real = await claims.stage_claim(
        _ctx(author_tid, author_aid),
        subject_reference=str(hidden),
        predicate="owned_by_team",
        value="x",
        evidence=_EV,
    )
    imaginary = await claims.stage_claim(
        _ctx(author_tid, author_aid),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="x",
        evidence=_EV,
    )

    assert (real.status, real.subject_entity_id, real.owning_tenant_id, real.visibility) == (
        imaginary.status,
        imaginary.subject_entity_id,
        imaginary.owning_tenant_id,
        imaginary.visibility,
    )


# --- every rejection is counted; a silent one is a defect --------------------------------


def _counter(name: str, **labels: str) -> float:
    """Current value of a labelled counter, or 0 if never incremented."""
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


@pytest.mark.asyncio
async def test_a_rejection_increments_its_own_reason(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Extraction that has quietly stopped conforming looks exactly like
    extraction with nothing to produce, unless each refusal is counted."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    metric = "contextplane_claim_rejected_total"
    before = _counter(metric, reason=REJECT_VALUE_TYPE)

    with pytest.raises(ClaimRejected):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="recovery_time_objective_seconds",
            value="about fifteen minutes",
            evidence=_EV,
        )

    assert _counter(metric, reason=REJECT_VALUE_TYPE) == before + 1


@pytest.mark.asyncio
async def test_reasons_are_counted_separately_not_lumped_together(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """One total tells an operator that writes are failing but not what to fix."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    metric = "contextplane_claim_rejected_total"
    before_type = _counter(metric, reason=REJECT_VALUE_TYPE)
    before_pred = _counter(metric, reason=REJECT_UNKNOWN_PREDICATE)

    with pytest.raises(ClaimRejected):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="nope_not_a_predicate",
            value="x",
            evidence=_EV,
        )

    assert _counter(metric, reason=REJECT_UNKNOWN_PREDICATE) == before_pred + 1
    assert _counter(metric, reason=REJECT_VALUE_TYPE) == before_type


@pytest.mark.asyncio
async def test_every_reason_constant_is_a_countable_label(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Label cardinality has to stay bounded, so the exported set and the
    enumerated set must be the same set."""
    for reason in REJECTION_REASONS:
        assert isinstance(reason, str)
        assert reason == reason.lower()
    assert len(REJECTION_REASONS) == len({r for r in REJECTION_REASONS})


@pytest.mark.asyncio
async def test_an_unresolved_subject_is_counted_as_its_own_rate(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A rising share means extraction is drifting off the entity model. No
    absolute claim count makes that visible."""
    tid, aid = await _seed_tenant(factory)
    metric = "contextplane_claim_unresolved_subject_total"
    before = _counter(metric)

    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/nowhere",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    assert _counter(metric) == before + 1


@pytest.mark.asyncio
async def test_a_staged_claim_is_counted_with_its_derived_authority(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Authority distribution is how an operator notices the pipeline has
    started producing only inference-tier claims."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    metric = "contextplane_claim_staged_total"
    labels = {"status": STATUS_STAGED, "source_authority": AUTHORITY_OWNER_INFERENCE}
    before = _counter(metric, **labels)

    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    assert _counter(metric, **labels) == before + 1


# --- values are parsed, not merely shaped ------------------------------------


@pytest.mark.asyncio
async def test_a_decimal_that_is_not_a_number_is_refused(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The type exists so availability targets keep their exactness. Accepting
    any string made `target_availability = "banana"` storable, and every later
    comparison against it undecidable forever."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="target_availability",
            value="banana",
            evidence=_EV,
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


@pytest.mark.asyncio
async def test_a_well_formed_decimal_is_accepted_as_a_string(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Stored as a string on purpose: a float would lose the precision the type
    exists to preserve."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="target_availability",
        value="0.999",
        evidence=_EV,
    )
    assert claim.status == STATUS_STAGED


@pytest.mark.asyncio
async def test_a_relative_url_is_refused(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A relative reference resolves against a base this store does not have, so
    it names nothing a reader could follow."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    for bad in ("nope", "/runbooks/auth", "example.com/runbook"):
        with pytest.raises(ClaimRejected) as exc:
            await claims.stage_claim(
                _ctx(tid, aid),
                subject_reference=str(subject),
                predicate="runbook_url",
                value=bad,
                evidence=_EV,
            )
        assert exc.value.reason == REJECT_VALUE_TYPE, bad


@pytest.mark.asyncio
async def test_an_absolute_url_is_accepted(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="runbook_url",
        value="https://runbooks.example/auth",
        evidence=_EV,
    )
    assert claim.status == STATUS_STAGED


@pytest.mark.asyncio
async def test_a_malformed_version_range_is_refused(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Validated against the same grammar the graph's own edges use, so a claim
    cannot carry a range that could never be promoted to one."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="interface_version",
            value=">=>=2",
            evidence=_EV,
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


@pytest.mark.asyncio
async def test_a_well_formed_version_range_is_accepted(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    for good in (">=2.0,<3.0", "^1.4", "~2.3.4", "1.2.3"):
        claim = await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="interface_version",
            value=good,
            evidence=_EV,
        )
        assert claim.status == STATUS_STAGED, good


@pytest.mark.asyncio
async def test_a_non_string_still_fails_before_any_parse(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Being a string is the entry condition, not the whole check — but it is
    still the first thing checked, so a number under a URL predicate does not
    reach a parser expecting text."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    with pytest.raises(ClaimRejected) as exc:
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="runbook_url",
            value=42,
            evidence=_EV,
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


# --- link_subject: the unlinked-to-staged transition ------------------------------------


@pytest.mark.asyncio
async def test_link_subject_moves_an_unlinked_claim_to_staged(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert claim.status == STATUS_UNLINKED

    await _map_external_id(factory, tid, subject, system="github", external="acme/mystery")
    linked = await claims.link_subject(
        claim_admin_ctx(tid, aid), claim_id=claim.claim_id, subject_reference="github:acme/mystery"
    )

    assert linked.status == STATUS_STAGED
    assert linked.subject_entity_id == subject
    assert linked.owning_tenant_id == tid

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT status, subject_entity_id FROM memory_claims WHERE claim_id = :cid"),
                {"cid": claim.claim_id},
            )
        ).one()
    assert row.status == STATUS_STAGED
    assert row.subject_entity_id == subject


@pytest.mark.asyncio
async def test_link_subject_scores_all_five_paired_confidence_columns_atomically(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The CHECK constraint ties five columns to confidence's own nullity, so a
    write that landed only some of them would already be refused by the schema
    -- every one has to land in the same statement that flips status."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    await _map_external_id(factory, tid, subject, system="github", external="acme/mystery")

    await claims.link_subject(
        claim_admin_ctx(tid, aid), claim_id=claim.claim_id, subject_reference="github:acme/mystery"
    )

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT confidence, confidence_scored_at, confidence_inputs, "
                    "       scorer_version, calibration_version, decay_half_life_days "
                    "FROM memory_claims WHERE claim_id = :cid"
                ),
                {"cid": claim.claim_id},
            )
        ).one()
    assert row.confidence is not None
    assert row.confidence_scored_at is not None
    assert row.confidence_inputs is not None
    assert row.scorer_version is not None
    assert row.calibration_version is not None
    assert row.decay_half_life_days is not None


@pytest.mark.asyncio
async def test_link_subject_narrows_visibility_to_the_subjects_own(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Whatever visibility was requested before the subject resolved was already
    discarded in favour of 'private' the moment resolution failed; linking
    derives fresh from the subject rather than reviving a request nobody could
    evaluate at the time -- a claim may never end up more visible than what it
    describes."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid, visibility="tenant-shared")

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/widget",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
        visibility="public",
    )
    assert claim.status == STATUS_UNLINKED
    assert claim.visibility == "private"

    await _map_external_id(factory, tid, subject, system="github", external="acme/widget")
    linked = await claims.link_subject(
        claim_admin_ctx(tid, aid), claim_id=claim.claim_id, subject_reference="github:acme/widget"
    )

    assert linked.visibility == "tenant-shared"


@pytest.mark.asyncio
async def test_link_subject_derives_owner_authority_when_the_linking_tenant_owns_the_subject(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid, visibility="public")
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/owned",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert claim.source_authority == AUTHORITY_UNATTRIBUTED

    await _map_external_id(factory, tid, subject, system="github", external="acme/owned")
    linked = await claims.link_subject(
        claim_admin_ctx(tid, aid), claim_id=claim.claim_id, subject_reference="github:acme/owned"
    )

    assert linked.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_link_subject_derives_observer_authority_when_the_linking_tenant_does_not_own_the_subject(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Authority flips on who owns the resolved subject, not on who authored the
    claim: the identical evidence earns a lower tier once the subject turns out
    to belong to somebody else's tenant."""
    owner_tid, _ = await _seed_tenant(factory)
    author_tid, author_aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, owner_tid, visibility="public")

    claim = await claims.stage_claim(
        _ctx(author_tid, author_aid),
        subject_reference="github:acme/someone-elses",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert claim.status == STATUS_UNLINKED

    await _map_external_id(factory, author_tid, subject, system="github", external="acme/someone-elses")
    linked = await claims.link_subject(
        claim_admin_ctx(author_tid, author_aid),
        claim_id=claim.claim_id,
        subject_reference="github:acme/someone-elses",
    )

    assert linked.owning_tenant_id == owner_tid
    assert linked.source_authority == AUTHORITY_OBSERVER_INFERENCE


@pytest.mark.asyncio
async def test_link_subject_reruns_contest_detection_against_the_new_neighbourhood(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A claim excluded from every neighbourhood query while unlinked can, the
    moment it gets a subject, disagree with something that was already there --
    and the disagreement has to lower both sides, not just the arriving one."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    incumbent = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert incumbent.is_contested is False

    challenger = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/mystery-team",
        predicate="owned_by_team",
        value="core-services",
        evidence=_EV,
    )
    assert challenger.status == STATUS_UNLINKED

    await _map_external_id(factory, tid, subject, system="github", external="acme/mystery-team")
    linked = await claims.link_subject(
        claim_admin_ctx(tid, aid), claim_id=challenger.claim_id, subject_reference="github:acme/mystery-team"
    )

    assert linked.is_contested is True
    async with factory() as session:
        incumbent_contested = (
            await session.execute(
                text("SELECT is_contested FROM memory_claims WHERE claim_id = :cid"),
                {"cid": incumbent.claim_id},
            )
        ).scalar_one()
    assert incumbent_contested is True


@pytest.mark.asyncio
async def test_link_subject_is_audited(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    await _map_external_id(factory, tid, subject, system="github", external="acme/mystery")

    await claims.link_subject(
        claim_admin_ctx(tid, aid), claim_id=claim.claim_id, subject_reference="github:acme/mystery"
    )

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT action, tenant_id FROM audit_log WHERE target_id = :t"),
                {"t": claim.claim_id},
            )
        ).one()
    assert row.action == actions.CLAIM_LINKED
    assert row.tenant_id == tid


@pytest.mark.asyncio
async def test_link_subject_requires_the_producer_or_admin_role(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    with pytest.raises(PermissionError):
        await claims.link_subject(
            tenant_context(tenant_id=tid, actor_id=aid, roles=["consumer"]),
            claim_id=claim.claim_id,
            subject_reference="github:acme/mystery",
        )


@pytest.mark.asyncio
async def test_link_subject_refuses_a_curator_from_another_tenant(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """An unlinked claim has no owner yet to scope its queue to, so it sits in
    the author's queue alone -- a curator from any other tenant must not be
    able to reach it, whatever role they hold."""
    tid, aid = await _seed_tenant(factory)
    other_tid, other_aid = await _seed_tenant(factory)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    with pytest.raises(PermissionError):
        await claims.link_subject(
            claim_admin_ctx(other_tid, other_aid),
            claim_id=claim.claim_id,
            subject_reference="github:acme/mystery",
        )


@pytest.mark.asyncio
async def test_link_subject_refuses_a_claim_that_is_already_staged(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert claim.status == STATUS_STAGED

    with pytest.raises(ConflictError):
        await claims.link_subject(claim_admin_ctx(tid, aid), claim_id=claim.claim_id, subject_reference=str(subject))


@pytest.mark.asyncio
async def test_link_subject_refuses_a_reference_that_still_does_not_resolve(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A failed link attempt must leave the row exactly as it was -- still
    unlinked, still unscored -- rather than partially advancing it."""
    tid, aid = await _seed_tenant(factory)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/never-existed",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert claim.status == STATUS_UNLINKED

    with pytest.raises(ValidationError):
        await claims.link_subject(
            claim_admin_ctx(tid, aid),
            claim_id=claim.claim_id,
            subject_reference="github:acme/never-existed",
        )

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT status, subject_entity_id, confidence FROM memory_claims WHERE claim_id = :cid"),
                {"cid": claim.claim_id},
            )
        ).one()
    assert row.status == STATUS_UNLINKED
    assert row.subject_entity_id is None
    assert row.confidence is None


@pytest.mark.asyncio
async def test_link_subject_raises_not_found_for_a_missing_claim(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    with pytest.raises(NotFoundError):
        await claims.link_subject(claim_admin_ctx(tid, aid), claim_id=uuid.uuid4(), subject_reference="whatever")


# --- discard: the queue's other curator decision ----------------------------------------


@pytest.mark.asyncio
async def test_discard_rejects_a_staged_claim_and_audits_the_reason(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    await claims.discard(claim_admin_ctx(tid, aid), claim_id=claim.claim_id, reason="wrong team, corrected verbally")

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT status FROM memory_claims WHERE claim_id = :cid"), {"cid": claim.claim_id}
            )
        ).one()
        audit_row = (
            await session.execute(
                text("SELECT action, after_jsonb FROM audit_log WHERE target_id = :t"), {"t": claim.claim_id}
            )
        ).one()
    assert row.status == "rejected"
    assert audit_row.action == actions.CLAIM_DISCARDED
    assert audit_row.after_jsonb["reason"] == "wrong team, corrected verbally"


@pytest.mark.asyncio
async def test_discard_rejects_a_never_resolvable_unlinked_claim(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A reference that will never resolve -- a typo, a decommissioned system,
    a name nobody will ever create -- has a way out of the queue: `rejected`
    with the subject and confidence still both NULL, exactly as the claim was
    staged. The migration legalizing that one terminal shape is what makes
    this possible; before it, the schema itself refused the write."""
    tid, aid = await _seed_tenant(factory)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert claim.status == STATUS_UNLINKED

    await claims.discard(
        claim_admin_ctx(tid, aid), claim_id=claim.claim_id, reason="dead reference, will never resolve"
    )

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT status, subject_entity_id, confidence FROM memory_claims WHERE claim_id = :cid"),
                {"cid": claim.claim_id},
            )
        ).one()
        audit_row = (
            await session.execute(
                text("SELECT action, after_jsonb FROM audit_log WHERE target_id = :t"), {"t": claim.claim_id}
            )
        ).one()
    assert row.status == "rejected"
    assert row.subject_entity_id is None
    assert row.confidence is None
    assert audit_row.action == actions.CLAIM_DISCARDED
    assert audit_row.after_jsonb["reason"] == "dead reference, will never resolve"


@pytest.mark.asyncio
async def test_discard_requires_the_producer_or_admin_role(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    with pytest.raises(PermissionError):
        await claims.discard(
            tenant_context(tenant_id=tid, actor_id=aid, roles=["consumer"]),
            claim_id=claim.claim_id,
            reason="nope",
        )


@pytest.mark.asyncio
async def test_discard_refuses_a_curator_from_another_tenant(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    other_tid, other_aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    with pytest.raises(PermissionError):
        await claims.discard(claim_admin_ctx(other_tid, other_aid), claim_id=claim.claim_id, reason="nope")


@pytest.mark.asyncio
async def test_discard_refuses_a_claim_that_is_not_staged(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    await claims.discard(claim_admin_ctx(tid, aid), claim_id=claim.claim_id, reason="first discard")

    with pytest.raises(ConflictError):
        await claims.discard(claim_admin_ctx(tid, aid), claim_id=claim.claim_id, reason="second discard")


@pytest.mark.asyncio
async def test_discard_raises_not_found_for_a_missing_claim(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    with pytest.raises(NotFoundError):
        await claims.discard(claim_admin_ctx(tid, aid), claim_id=uuid.uuid4(), reason="whatever")
