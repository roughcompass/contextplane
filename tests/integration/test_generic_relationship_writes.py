"""The generic relationship surface routes by intent, and ordinary agents cannot write canon.

The same negative property the entity suite proves, on the surface where getting
it wrong is more expensive: a relationship's canonical write takes an aggregate
lock and can refuse for reasons an entity write has no equivalent of — endpoint
type, duplicate, maximum cardinality, a cross-organization edge with no grant.

So the refusals here assert on the *code* that came back, not on the status. A
caller told `maximum_cardinality_exceeded` can act on it; a caller told `422` with
prose has to parse prose, and prose changes.

The approval route reaches `RelationshipWriteService`, so these tests also stand
as the transport-level proof that the port satisfying its endpoint resolver — the
seam that keeps the relationships package below the visibility chokepoint in the
import contract — is actually wired.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.profile.compiler import compile_profile
from contextplane.profile.schemas.entity import EntityTypeDefinition
from contextplane.profile.schemas.relationship import RelationshipTypeDefinition
from contextplane.relationships import definitions as relationship_definitions
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)
from tests.helpers.builders import make_persona_new_client

_NS = "core"
_SRC_TYPE = "capability"
_DST_TYPE = "concept"
_REL = f"{_NS}:depends_on"
_NOW = "2026-08-13T12:00:00Z"
#: The same instant as a real datetime, for the SQL fixtures. The driver binds
#: timestamps as datetimes, not as the ISO strings the JSON bodies carry.
_NOW_DT = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)


def _definition(**overrides: object) -> RelationshipTypeDefinition:
    fields: dict[str, object] = {
        "namespace": _NS,
        "type_name": "depends_on",
        "source_type": f"{_NS}:{_SRC_TYPE}",
        "destination_type": f"{_NS}:{_DST_TYPE}",
        "direction": "directed",
        "cardinality_scope": "per_source",
        "authority": "canonical_owner",
        "cross_org_policy": "deny",
        "min_cardinality": 0,
        "max_cardinality": None,
        "duplicate_policy": "reject",
        "symmetry": "asymmetric",
        "inverse_view": "read_only",
    }
    fields.update(overrides)
    return RelationshipTypeDefinition(**fields)  # type: ignore[arg-type]


async def _seed(pg_url: str, tenant_slug: str, definition: RelationshipTypeDefinition) -> dict[str, uuid.UUID]:
    """Bind the tenant to a profile declaring this relationship, and make two endpoints."""
    document = compile_profile(
        entities=[
            EntityTypeDefinition(namespace=_NS, type_name=_SRC_TYPE),
            EntityTypeDefinition(namespace=_NS, type_name=_DST_TYPE),
        ],
        relationships=[definition],
        interfaces=[],
    ).document
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    revision_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            tenant_id = (
                await session.execute(text("SELECT tenant_id FROM tenants WHERE slug = :slug"), {"slug": tenant_slug})
            ).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO profile_revisions ("
                    "  profile_revision_id, profile_family, profile_name, semantic_version,"
                    "  canonical_document, document_digest, compatibility, published_by, published_at"
                    ") VALUES (:rid, 'platform', :name, '1.0.0', CAST(:doc AS JSONB), :digest,"
                    "          'backward_compatible', 'test', :now)"
                ),
                {
                    "rid": revision_id,
                    "name": f"rel-{revision_id.hex[:12]}",
                    "doc": document,
                    "digest": revision_id.hex,
                    "now": _NOW_DT,
                },
            )
            await relationship_definitions.project_published_relationships(
                session,
                profile_revision_id=revision_id,
                document=document,
                compiled_at=_NOW_DT,  # type: ignore[arg-type]
            )
            await session.execute(
                text(
                    "INSERT INTO profile_bindings ("
                    "  binding_id, tenant_id, profile_revision_id, extension_set_digest, state,"
                    "  effective_from, actor, reason, recorded_at"
                    ") VALUES (:bid, :tid, :rid, :digest, 'active', :now, 'test', 'test', :now)"
                ),
                {
                    "bid": uuid.uuid4(),
                    "tid": tenant_id,
                    "rid": revision_id,
                    "digest": revision_id.hex,
                    "now": _NOW_DT,
                },
            )
            ids = {}
            for role, entity_type in (("source", f"{_NS}:{_SRC_TYPE}"), ("destination", f"{_NS}:{_DST_TYPE}")):
                entity_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO entities (entity_id, tenant_id, entity_type, name, is_active, created_at)"
                        " VALUES (:eid, :tid, :etype, :name, TRUE, :now)"
                    ),
                    {
                        "eid": entity_id,
                        "tid": tenant_id,
                        "etype": entity_type,
                        "name": f"{role}-{entity_id.hex[:8]}",
                        "now": _NOW_DT,
                    },
                )
                ids[role] = entity_id
            return ids
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


async def _persona(h: EntitlementAuthHarness, pg_url: str) -> TenantPersona:
    return await make_persona_new_client(h, pg_url, slug=f"rel-{uuid.uuid4().hex[:6]}", roles=["producer"])


def _body(intent: str, ids: dict[str, uuid.UUID], **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "intent": intent,
        "subject_kind": "relationship",
        "subject_type": _REL,
        "identity": {"handle": f"{_REL}/edge"},
        "endpoints": {
            "source_entity_id": str(ids["source"]),
            "destination_entity_id": str(ids["destination"]),
        },
        "target_revision": {"profile_revision": "1.0.0"},
        "temporal": {"valid_from": _NOW},
        "idempotency_key": uuid.uuid4().hex,
        "provenance": {
            "source_system": "conformance",
            "source_namespace": "internal",
            "external_record_id": "rec-1",
            "observed_time": _NOW,
        },
        "properties": {},
    }
    body.update(extra)
    return body


async def _post(client: AsyncClient, persona: TenantPersona, body: dict[str, object]):
    return await client.post("/v1/relationships", json=body, headers=bearer_headers(tenant_slug=persona.slug))


# --- intent routing ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_observation_stages_a_claim_and_writes_no_edge(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """An ordinary agent's observation must not reach the governed graph."""
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, _body("observation", ids))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["effect"] == "staged_claim"
    assert body["relationship_id"] is None


@pytest.mark.asyncio
async def test_a_request_creates_an_owner_review_entry_and_writes_no_edge(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, _body("request", ids))

    assert response.status_code == 201, response.text
    assert response.json()["effect"] == "owner_review_entry"
    assert response.json()["relationship_id"] is None


@pytest.mark.asyncio
async def test_an_authorized_approval_writes_through_the_transactional_service(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Proves the endpoint-resolver port is wired, not only that routing happened."""
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, _body("authorized_approval", ids, approval_reference="review-1"))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["effect"] == "canonical_assertion_write"
    assert body["relationship_id"] is not None
    assert body["readiness_state"] == "ready"


# --- refusals carry their code --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_duplicate_is_refused_by_name(harness: EntitlementAuthHarness, pg_container: str) -> None:
    """The refusal names the rule, so a caller can branch without parsing prose."""
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            first = await _post(client, persona, _body("authorized_approval", ids, approval_reference="review-1"))
            assert first.status_code == 201, first.text
            second = await _post(client, persona, _body("authorized_approval", ids, approval_reference="review-2"))

    assert second.status_code == 422, second.text
    # The app wraps handler detail in its own error envelope, so the code is
    # asserted as the payload it is rather than at a guessed key path.
    assert "duplicate_refused" in second.text


@pytest.mark.asyncio
async def test_an_endpoint_of_the_wrong_type_is_refused_by_name(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    swapped = {"source": ids["destination"], "destination": ids["source"]}
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(
                client, persona, _body("authorized_approval", swapped, approval_reference="review-1")
            )

    assert response.status_code == 422, response.text
    assert "endpoint_type_mismatch" in response.text


@pytest.mark.asyncio
async def test_an_unknown_relationship_type_is_refused_by_name(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(
                client,
                persona,
                _body("authorized_approval", ids, subject_type=f"{_NS}:invented", approval_reference="r"),
            )

    assert response.status_code == 422, response.text
    assert "unknown_relationship_type" in response.text


@pytest.mark.asyncio
async def test_a_caller_supplied_authority_cannot_reach_the_canonical_path(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    payload = _body("observation", ids)
    payload["authority"] = "canonical_owner"
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, payload)

    assert response.status_code in {403, 422}, response.text


@pytest.mark.asyncio
async def test_a_write_with_no_intent_is_refused(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    payload = _body("observation", ids)
    del payload["intent"]
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, payload)

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_an_entity_subject_on_the_relationship_path_is_refused(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    payload = _body("observation", ids)
    payload["subject_kind"] = "entity"
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, payload)

    assert response.status_code == 422, response.text


# --- read and bounded query -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_written_relationship_reads_back_with_its_governance(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            created = await _post(client, persona, _body("authorized_approval", ids, approval_reference="review-1"))
            assert created.status_code == 201, created.text
            relationship_id = created.json()["relationship_id"]
            read = await client.get(
                f"/v1/relationships/{relationship_id}", headers=bearer_headers(tenant_slug=persona.slug)
            )

    assert read.status_code == 200, read.text
    body = read.json()
    assert body["relationship_type"] == _REL
    assert body["provenance"]["authority"] == "canonical_owner"
    assert body["profile"]["profile_revision_id"] is not None
    assert body["readiness_state"] == "ready"


@pytest.mark.asyncio
async def test_an_outgoing_query_returns_the_stored_direction(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            await _post(client, persona, _body("authorized_approval", ids, approval_reference="review-1"))
            page = await client.post(
                "/v1/relationships:query",
                json={"entity_id": str(ids["source"]), "direction": "outgoing"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )

    assert page.status_code == 200, page.text
    body = page.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["is_inverse"] is False
    assert body["has_more"] is False


@pytest.mark.asyncio
async def test_an_incoming_query_returns_a_derived_inverse_view(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """The inverse is the same stored fact read backwards, and says so.

    A caller that treated one as a second edge would be double-counting, so
    `is_inverse` travelling with the row is the difference between a view and a
    duplicate.
    """
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            await _post(client, persona, _body("authorized_approval", ids, approval_reference="review-1"))
            page = await client.post(
                "/v1/relationships:query",
                json={"entity_id": str(ids["destination"]), "direction": "incoming"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )

    assert page.status_code == 200, page.text
    item = page.json()["items"][0]
    assert item["is_inverse"] is True
    assert item["endpoints"]["source_entity_id"] == str(ids["destination"])
    assert item["endpoints"]["destination_entity_id"] == str(ids["source"])


@pytest.mark.asyncio
async def test_a_query_page_is_bounded(harness: EntitlementAuthHarness, pg_container: str) -> None:
    """A traversal with no ceiling is a request whose cost nobody can predict."""
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await client.post(
                "/v1/relationships:query",
                json={"entity_id": str(ids["source"]), "limit": 10_000},
                headers=bearer_headers(tenant_slug=persona.slug),
            )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_reading_a_relationship_that_does_not_exist_is_a_404(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = await _persona(harness, pg_container)
    await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await client.get(
                f"/v1/relationships/{uuid.uuid4()}", headers=bearer_headers(tenant_slug=persona.slug)
            )

    assert response.status_code == 404


# --- the profile the write is checked against is the relationship family ------------


@pytest.mark.asyncio
async def test_a_declared_relationship_type_validates_clean(harness: EntitlementAuthHarness, pg_container: str) -> None:
    """The subject was checked against the family that declares it.

    Before this, the surface validated `subject_type` through `EntityValidator`,
    which reads only the `entity` family. Every relationship write — against a
    type the tenant's own profile declared — came back `valid: false` carrying
    `unknown_entity_type`. Nothing branched on it, so it went unnoticed; it was
    still a wrong answer handed to every caller, and the contract says
    `violations` may be non-empty on a *successful* write, so a client had no way
    to tell this artifact from a real finding.
    """
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, _body("authorized_approval", ids, approval_reference="review-1"))

    assert response.status_code == 201, response.text
    validation = response.json()["validation"]
    assert validation["violations"] == []
    assert validation["valid"] is True
    assert validation["mode"] == "mandatory"


@pytest.mark.asyncio
async def test_an_undeclared_subject_type_is_named_in_the_relationship_vocabulary(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """A caller sent looking through its entity declarations would never find the edge.

    The observation route is used because it stages a claim rather than reaching
    the write service, so the violation reported is the validator's own rather
    than the service's refusal of the same condition.
    """
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, _body("observation", ids, subject_type=f"{_NS}:invented"))

    assert response.status_code == 201, response.text
    validation = response.json()["validation"]
    assert any("unknown_relationship_type" in message for message in validation["violations"]), validation
    assert not any("unknown_entity_type" in message for message in validation["violations"]), validation


@pytest.mark.asyncio
async def test_the_staged_routes_are_where_property_rules_get_checked_at_all(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """`RelationshipWriteService` checks properties only on the canonical route.

    An observation stages a claim and never reaches it, so the validator is the
    only thing that looks at what was written. An undeclared property has to be
    reported here or it is reported nowhere.
    """
    persona = await _persona(harness, pg_container)
    ids = await _seed(pg_container, persona.slug, _definition())
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, _body("observation", ids, properties={"invented": "x"}))

    assert response.status_code == 201, response.text
    validation = response.json()["validation"]
    assert any("undeclared_property" in message for message in validation["violations"]), validation
