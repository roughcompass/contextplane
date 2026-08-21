"""The ARC REST surface: routing, request shape, and error translation.

These tests are about the adapter layer, not the services beneath it —
those have their own files. What matters here is that the routes exist,
reject what they should reject before reaching a service, and translate
typed ARC exceptions into the statuses the interface promises.

The most important assertions are the ones about what a caller *cannot*
say: server-derived identity is not accepted from the body, and a rejection
returns one bounded code regardless of which check refused it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)

_HOST_HEADER = {"x-arc-host-id": "host-1"}


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


@pytest_asyncio.fixture
async def client(harness: EntitlementAuthHarness) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def persona(harness: EntitlementAuthHarness, client: AsyncClient):
    p = harness.add_persona(f"arc-rest-{uuid.uuid4().hex[:6]}", roles=["consumer"])
    harness.configure_fetcher_for(p)
    with patch_validator_for_actor(p):
        resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=p.slug))
        assert resp.status_code == 200, resp.text
    return p


def _challenge_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "session_id": "sess-1",
        "manifest_claims_digest": "a" * 64,
        "idempotency_key": uuid.uuid4().hex,
    }
    body.update(overrides)
    return body


# --- the routes exist and are mounted ------------------------------------------


@pytest.mark.asyncio
async def test_the_arc_routes_are_registered(harness: EntitlementAuthHarness) -> None:
    """A route that exists in the router but was never included in the app
    would fail only when a caller tried to use it."""
    paths = {r.path for r in harness.app.routes if hasattr(r, "path")}
    assert "/v1/arc/challenges" in paths
    assert "/v1/arc/resolve" in paths
    assert "/v1/arc/receipts/{receipt_id}" in paths
    assert "/v1/arc/receipts/{receipt_id}/detail" in paths
    assert "/v1/arc/receipts/{receipt_id}/explain" in paths
    assert "/v1/arc/metadata" in paths


@pytest.mark.asyncio
async def test_challenge_issuance_requires_authentication(client: AsyncClient) -> None:
    resp = await client.post("/v1/arc/challenges", json=_challenge_body(), headers=_HOST_HEADER)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_detail_requires_authentication(client: AsyncClient) -> None:
    resp = await client.post(
        f"/v1/arc/receipts/{uuid.uuid4()}/detail",
        json={"context_handle": "h", "request_kind": "directive", "idempotency_key": "k1"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_receipt_read_requires_authentication(client: AsyncClient) -> None:
    resp = await client.get(f"/v1/arc/receipts/{uuid.uuid4()}")
    assert resp.status_code == 401


# --- what a caller may not say ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_caller_cannot_supply_its_own_tenant_or_host_in_the_body(client: AsyncClient, persona) -> None:
    """Server-derived identity is not caller-writable. The request model is
    closed, so naming one of those fields is rejected outright rather than
    silently ignored — a caller that believes it set something ARC never
    read is worse off than one told it cannot."""
    with patch_validator_for_actor(persona):
        for forbidden in ("tenant_id", "host_id", "actor_id", "oidc_subject"):
            resp = await client.post(
                "/v1/arc/challenges",
                json=_challenge_body(**{forbidden: str(uuid.uuid4())}),
                headers={**bearer_headers(tenant_slug=persona.slug), **_HOST_HEADER},
            )
            assert resp.status_code == 422, f"{forbidden}: {resp.text}"


@pytest.mark.asyncio
async def test_challenge_issuance_without_a_host_identity_is_refused(client: AsyncClient, persona) -> None:
    """A challenge binds to a host. A caller with no host identity has
    nothing to bind to, and must not get one bound to a default."""
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/challenges",
            json=_challenge_body(),
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 403
    assert resp.json()["errors"][0]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_a_malformed_claims_digest_is_rejected_before_any_service_runs(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/challenges",
            json=_challenge_body(manifest_claims_digest="too-short"),
            headers={**bearer_headers(tenant_slug=persona.slug), **_HOST_HEADER},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_malformed_idempotency_key_is_rejected(client: AsyncClient, persona) -> None:
    """The pattern is part of the contract: a key with characters outside it
    would still digest fine, so the shape has to be enforced here."""
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/challenges",
            json=_challenge_body(idempotency_key="not a valid key!"),
            headers={**bearer_headers(tenant_slug=persona.slug), **_HOST_HEADER},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_an_unknown_detail_request_kind_is_rejected(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/v1/arc/receipts/{uuid.uuid4()}/detail",
            json={
                "context_handle": "h",
                "request_kind": "something_else",
                "idempotency_key": "k1",
            },
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_an_oversized_page_request_is_rejected(client: AsyncClient, persona) -> None:
    """The page ceiling is enforced at the boundary, not left to the service
    to clamp silently."""
    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/v1/arc/receipts/{uuid.uuid4()}/detail",
            json={
                "context_handle": "h",
                "request_kind": "directive",
                "idempotency_key": "k1",
                "max_response_bytes": 999_999,
            },
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 422


# --- not-found is indistinguishable from not-yours ---------------------------------


@pytest.mark.asyncio
async def test_an_unknown_receipt_is_not_found(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.get(f"/v1/arc/receipts/{uuid.uuid4()}", headers=bearer_headers(tenant_slug=persona.slug))
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["code"] == "not_found"


@pytest.mark.asyncio
async def test_explaining_an_unknown_receipt_is_not_found(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.get(
            f"/v1/arc/receipts/{uuid.uuid4()}/explain",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 404


# --- verification metadata ----------------------------------------------------------


@pytest.mark.asyncio
async def test_verification_metadata_is_readable_without_a_credential(client: AsyncClient) -> None:
    """A verifier holding a receipt may not be a registry caller at all, and
    the payload is public key material by construction."""
    resp = await client.get("/v1/arc/metadata")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_verification_metadata_names_the_profiles_a_verifier_needs(
    client: AsyncClient,
) -> None:
    """Without the profile names a verifier can hold the right public key
    and still not know what bytes to reconstruct."""
    body = (await client.get("/v1/arc/metadata")).json()
    assert body["receipt_event_signature_profile"] == "arc_receipt_event_sig_v1"
    assert "arc_receipt_event_v1" in body["canonical_profiles"]
    assert "arc_host_attestation_v1_payload" in body["canonical_profiles"]
    assert isinstance(body["keys"], list)


@pytest.mark.asyncio
async def test_verification_metadata_carries_no_private_material(client: AsyncClient) -> None:
    """The route publishes a key manifest; a private key reaching it would
    be catastrophic and silent."""
    raw = (await client.get("/v1/arc/metadata")).text.lower()
    for leaked in ("private", "secret", "-----begin"):
        assert leaked not in raw


# --- resolution ----------------------------------------------------------------


def _resolve_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "manifest": {
            "session_id": "sess-1",
            "intent_kind": "deployment",
            "requested_action_classes": ["deploy"],
            "entity_ids": [],
            "domain_ids": [],
            "environment": "production",
            "data_sensitivity": "internal",
            "repository_identity": "git://example/repo",
            "supported_context_bundle_content_profiles": ["arc_context_bundle_content_v1"],
        },
        "attestation": {
            "profile": "arc_host_attestation_v1_payload",
            "signer_key_id": "hk-1",
            "attestation_id": f"att-{uuid.uuid4().hex[:12]}",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2036-01-01T00:00:00Z",
            "payload": {"host_id": "host-1"},
            "signature": "c2ln",
        },
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_resolution_requires_authentication(client: AsyncClient) -> None:
    resp = await client.post("/v1/arc/resolve", json=_resolve_body(), headers=_HOST_HEADER)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_resolution_without_a_host_identity_is_refused(client: AsyncClient, persona) -> None:
    """The host identity comes from a header the gateway sets, never the
    body. Without one there is nothing to bind a challenge to."""
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/resolve",
            json=_resolve_body(),
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_an_unconfigured_deployment_says_so_rather_than_failing(client: AsyncClient, persona) -> None:
    """Resolution signs a receipt and seals the retained response, so a
    deployment with no ARC key material cannot do it.

    503 rather than 500: the deployment is not broken, it is not configured,
    and an operator reading a 500 would go looking for a fault that is not
    there.
    """
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/resolve",
            json=_resolve_body(),
            headers={**bearer_headers(tenant_slug=persona.slug), **_HOST_HEADER},
        )
    assert resp.status_code == 503, resp.text
    assert resp.json()["errors"][0]["code"] == "unavailable"


@pytest.mark.asyncio
async def test_a_caller_cannot_declare_its_own_host_in_the_manifest(client: AsyncClient, persona) -> None:
    """`host_id` is not a manifest field. A caller able to name its own host
    could bind a resolution to somebody else's identity, so the closed model
    must reject it rather than ignore it."""
    body = _resolve_body()
    body["manifest"]["host_id"] = "host-someone-else"  # type: ignore[index]
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/resolve",
            json=body,
            headers={**bearer_headers(tenant_slug=persona.slug), **_HOST_HEADER},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_an_unknown_task_kind_is_rejected_before_any_service_runs(client: AsyncClient, persona) -> None:
    """Closed vocabulary. Reported specifically rather than as one bounded
    code: the caller sent the value, so naming it tells them nothing they
    did not already know, and a bare 403 here is merely confusing."""
    body = _resolve_body()
    body["manifest"]["intent_kind"] = "not-a-real-kind"  # type: ignore[index]
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/resolve",
            json=body,
            headers={**bearer_headers(tenant_slug=persona.slug), **_HOST_HEADER},
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["errors"][0]["code"] == "invalid_manifest"


@pytest.mark.asyncio
async def test_an_oversized_context_budget_is_rejected(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/resolve",
            json=_resolve_body(max_context_bytes=10_000_000),
            headers={**bearer_headers(tenant_slug=persona.slug), **_HOST_HEADER},
        )
    assert resp.status_code == 422
