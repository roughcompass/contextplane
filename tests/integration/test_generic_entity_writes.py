"""The generic entity surface routes by intent, and ordinary agents cannot write canon.

The property this file exists to prove is a negative one: an ordinary caller can
exercise every endpoint here and never put a row in the canonical graph. That is
what makes a generic, profile-driven write surface safe to expose at all, and it
is exactly the kind of guarantee that decays quietly — a route that started
returning `canonical_assertion_write` for an observation would still return `201`,
still produce a plausible body, and still pass any test that only checked the
status code.

So the assertions here are about the *effect* and about which identifier came
back, not about success. An observation must produce a staged claim and must not
produce an entity id; a request must produce a review entry; only an approval
reaches the canonical path. The response model refuses a mismatched pair itself,
which means a handler that set the wrong field fails loudly rather than lying.

Two refusals are tested that a caller could otherwise route around: a body with no
intent, and a body that states its own authority. Both are the shape of a client
that has read the happy path and inferred the rest.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)
from tests.helpers.builders import make_persona_new_client

_TYPE = "capability"


async def _seed(pg_url: str, tenant_slug: str) -> None:
    """Seed the vocabulary the canonical create path checks against."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            row = (
                await session.execute(text("SELECT tenant_id FROM tenants WHERE slug = :slug"), {"slug": tenant_slug})
            ).first()
            assert row is not None, f"tenant {tenant_slug} not materialised yet"
            await session.execute(
                text(
                    "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system)"
                    " VALUES (:tid, 'entity_type', :value, FALSE) ON CONFLICT DO NOTHING"
                ),
                {"tid": row[0], "value": _TYPE},
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


async def _persona(h: EntitlementAuthHarness, pg_url: str) -> TenantPersona:
    persona = await make_persona_new_client(h, pg_url, slug=f"gen-{uuid.uuid4().hex[:6]}", roles=["producer"])
    await _seed(pg_url, persona.slug)
    return persona


def _body(intent: str, *, name: str, **extra: object) -> dict[str, object]:
    """A complete generic write envelope, so a test changes only what it names."""
    body: dict[str, object] = {
        "intent": intent,
        "subject_kind": "entity",
        "subject_type": _TYPE,
        "identity": {"handle": f"core:{_TYPE}/{name}"},
        "target_revision": {"profile_revision": "1.0.0"},
        "temporal": {"valid_from": "2026-08-13T12:00:00Z"},
        "idempotency_key": uuid.uuid4().hex,
        "provenance": {
            "source_system": "conformance",
            "source_namespace": "internal",
            "external_record_id": "rec-1",
            "observed_time": "2026-08-13T12:00:00Z",
        },
        "properties": {},
    }
    body.update(extra)
    return body


async def _post(client: AsyncClient, persona: TenantPersona, body: dict[str, object]):
    return await client.post("/v1/entities", json=body, headers=bearer_headers(tenant_slug=persona.slug))


# --- intent routing ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_observation_stages_a_claim_and_writes_no_entity(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """An ordinary agent's observation must not reach the canonical graph.

    Asserted on the effect and on the *absence* of an entity id, because a route
    that wrote canonically would return `201` with a plausible body either way.
    """
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, _body("observation", name="observed-thing"))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["effect"] == "staged_claim"
    assert body["staged_claim_id"] is not None
    assert body["entity_id"] is None


@pytest.mark.asyncio
async def test_a_request_creates_an_owner_review_entry_and_writes_no_entity(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, _body("request", name="requested-thing"))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["effect"] == "owner_review_entry"
    assert body["review_entry_id"] is not None
    assert body["entity_id"] is None


@pytest.mark.asyncio
async def test_an_authorized_approval_writes_canonically(harness: EntitlementAuthHarness, pg_container: str) -> None:
    """The two refusals above prove nothing unless the approval route actually writes.

    Without this, a surface that refused every intent would pass the whole file.
    """
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(
                client,
                persona,
                _body("authorized_approval", name="approved-thing", approval_reference="review-1"),
            )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["effect"] == "canonical_assertion_write"
    assert body["entity_id"] is not None
    assert body["staged_claim_id"] is None


# --- refusals a client could otherwise route around ----------------------------------


@pytest.mark.asyncio
async def test_a_write_with_no_intent_is_refused(harness: EntitlementAuthHarness, pg_container: str) -> None:
    """There is no default, because every default routes a write somewhere unchosen."""
    persona = await _persona(harness, pg_container)
    payload = _body("observation", name="no-intent")
    del payload["intent"]

    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, payload)

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_an_unknown_intent_is_refused(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, _body("just_write_it", name="bad-intent"))

    assert response.status_code in {403, 422}, response.text


@pytest.mark.asyncio
async def test_a_caller_supplied_authority_cannot_reach_the_canonical_path(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """A body naming its own trust class is refused as such, not merely ignored.

    Ignoring it would be nearly as bad: the caller would believe it had asserted
    authority and the platform would silently disagree, which is the disagreement
    nobody notices until an audit.
    """
    persona = await _persona(harness, pg_container)
    payload = _body("observation", name="self-authorized")
    payload["authority"] = "canonical_owner"

    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, payload)

    assert response.status_code in {403, 422}, response.text
    assert "authority" in response.text.lower()


@pytest.mark.asyncio
async def test_an_approval_reference_on_a_non_approval_intent_is_refused(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """On any route but approval, it asserts a review that did not happen."""
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(
                client,
                persona,
                _body("observation", name="fake-approval", approval_reference="review-1"),
            )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_an_approval_with_no_reference_is_refused(harness: EntitlementAuthHarness, pg_container: str) -> None:
    """The service re-resolves the reference and has nothing to resolve without it."""
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, _body("authorized_approval", name="unbacked"))

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_a_relationship_subject_on_the_entity_path_is_refused(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """A body saying `relationship` here is a caller with the wrong URL."""
    persona = await _persona(harness, pg_container)
    payload = _body("observation", name="wrong-surface")
    payload["subject_kind"] = "relationship"

    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await _post(client, persona, payload)

    assert response.status_code == 422, response.text


# --- read, resolve, readiness ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_canonically_written_entity_reads_back_with_its_governance(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """The read carries profile, provenance, validation, temporal and readiness.

    A generic reader has no other way to learn which governance accepted a row, so
    leaving those to a second call would mean every careful caller makes two and
    every careless one makes none.
    """
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            created = await _post(
                client,
                persona,
                _body("authorized_approval", name="readable-thing", approval_reference="review-2"),
            )
            assert created.status_code == 201, created.text
            entity_id = created.json()["entity_id"]

            read = await client.get(f"/v1/entities/{entity_id}", headers=bearer_headers(tenant_slug=persona.slug))

    assert read.status_code == 200, read.text
    body = read.json()
    assert body["identity"]["entity_id"] == entity_id
    assert body["identity"]["entity_type"] == _TYPE
    assert set(body) == {
        "identity",
        "properties",
        "profile",
        "provenance",
        "validation",
        "temporal",
        "readiness_state",
    }


@pytest.mark.asyncio
async def test_reading_an_entity_that_does_not_exist_is_a_404(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await client.get(
                f"/v1/entities/{uuid.uuid4()}", headers=bearer_headers(tenant_slug=persona.slug)
            )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_resolve_finds_an_entity_by_name(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            created = await _post(
                client,
                persona,
                _body("authorized_approval", name="resolvable-thing", approval_reference="review-3"),
            )
            assert created.status_code == 201, created.text

            resolved = await client.get(
                "/v1/entities:resolve",
                params={"handle": "resolvable-thing"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )

    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["identity"]["name"] == "resolvable-thing"


@pytest.mark.asyncio
async def test_resolving_an_unknown_handle_is_a_404(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            response = await client.get(
                "/v1/entities:resolve",
                params={"handle": "nothing-called-this"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_readiness_reports_ready_for_an_entity_with_no_required_relationships(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Readiness is recounted on demand rather than read off a stored flag.

    The stored state on an assertion is what was true when that row was written,
    which is the right answer for an audit and the wrong one for "may this go
    active now?".
    """
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            created = await _post(
                client,
                persona,
                _body("authorized_approval", name="ready-thing", approval_reference="review-4"),
            )
            assert created.status_code == 201, created.text
            entity_id = created.json()["entity_id"]

            readiness = await client.post(
                f"/v1/entities/{entity_id}:validate-readiness",
                headers=bearer_headers(tenant_slug=persona.slug),
            )

    assert readiness.status_code == 200, readiness.text
    body = readiness.json()
    assert body["entity_id"] == entity_id
    assert body["readiness_state"] == "ready"
    assert body["blocking"] == []


@pytest.mark.asyncio
async def test_an_update_routes_by_intent_the_same_way_a_create_does(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """An observation must not amend a canonical row directly.

    An update that bypassed the intent split would be a way around the whole
    surface: assert canonically once, then edit freely.
    """
    persona = await _persona(harness, pg_container)
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            created = await _post(
                client,
                persona,
                _body("authorized_approval", name="updatable-thing", approval_reference="review-5"),
            )
            assert created.status_code == 201, created.text
            entity_id = created.json()["entity_id"]

            patched = await client.patch(
                f"/v1/entities/{entity_id}",
                json=_body("observation", name="updatable-thing"),
                headers=bearer_headers(tenant_slug=persona.slug),
            )

    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["effect"] == "staged_claim"
    assert body["entity_id"] is None
