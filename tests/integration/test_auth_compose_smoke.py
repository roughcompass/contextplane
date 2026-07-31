"""Live-stack smoke test for the entitlement auth path.

The only test that drives the real wire-level chain end to end: fetch a JWT
from the identity provider, present it to a running registry over HTTP, and
have the registry fetch discovery + JWKS, verify the signature, call the
entitlement service, and resolve a tenant.

Everything else stops short of that. Unit and integration tests mint tokens
with ``make_jwt`` and patch ``validate_oidc_token`` away;
``test_rbac_oidc.py`` exercises signature validation against an in-process
IdP but never over a socket, and never through the entitlement resolver.

Runs against **either** stack — ``make dev-up`` or ``docker compose up`` —
because both publish the same services on the same ports. There is no env var
to remember: the test probes for a reachable stack and skips when there is not
one, which is the same condition it was checking before, just measured rather
than declared.

    make dev-up && make dev-token
    pytest tests/integration/test_auth_compose_smoke.py -m compose -q
"""

from __future__ import annotations

import os
import pathlib

import httpx
import pytest

pytestmark = pytest.mark.compose


_MOCK_OIDC_URL = os.environ.get("MOCK_OIDC_URL", "http://localhost:8090")
_MOCK_ENTITLEMENT_URL = os.environ.get("MOCK_ENTITLEMENT_URL", "http://localhost:8091")
_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://localhost:8000")

_ENV_DEV = pathlib.Path(__file__).parent.parent.parent / ".env.dev"


def _dev_identity() -> dict[str, str]:
    """Read the identity `make dev-token` provisioned.

    The values have to come from the file rather than a constant. The bootstrap
    script owns the tenant slug, and a hardcoded default silently rots the day
    it changes — which is exactly what happened: this test asserted a slug of
    `111205` long after the bootstrap had settled on `dev`, so it failed on the
    last line whenever anyone actually ran it. Environment variables still win,
    for a stack provisioned some other way.
    """
    values: dict[str, str] = {}
    if _ENV_DEV.is_file():
        for line in _ENV_DEV.read_text().splitlines():
            key, _, value = line.partition("=")
            if key and value:
                values[key.strip()] = value.strip()
    return {
        "user_id": os.environ.get("DEV_USER_ID") or values.get("DEV_USER_ID", "dev-admin"),
        "tenant_slug": os.environ.get("DEV_TENANT_SLUG") or values.get("DEV_TENANT_SLUG", "dev"),
        "client_id": os.environ.get("CLIENT_ID") or values.get("CLIENT_ID", "registry-dev"),
        "client_secret": os.environ.get("CLIENT_SECRET") or values.get("CLIENT_SECRET", "dev-secret"),
    }


def _stack_reachable() -> str | None:
    """Return None when a usable stack is up, else the reason it is not."""
    if not _ENV_DEV.is_file():
        return "no .env.dev — run `make dev-token` to provision the dev tenant"
    probes = (
        (f"{_MOCK_OIDC_URL}/default/.well-known/openid-configuration", "mock IdP"),
        (f"{_MOCK_ENTITLEMENT_URL}/healthz", "mock entitlement service"),
        (f"{_REGISTRY_URL}/healthz", "registry API"),
    )
    for url, name in probes:
        try:
            with httpx.Client(timeout=2.0) as client:
                if client.get(url).status_code != 200:
                    return f"{name} at {url} did not return 200"
        except httpx.HTTPError:
            return f"{name} at {url} is unreachable — start a stack with `make dev-up`"
    return None


@pytest.mark.skipif(_stack_reachable() is not None, reason=_stack_reachable() or "")
def test_real_jwt_flows_through_to_whoami() -> None:
    """A JWT minted by the real IdP authenticates against a running registry.

    One scenario on purpose. Failure modes are covered by the unit and
    integration suites; what only this test can show is that the wire-level
    pieces interconnect — discovery document, JWKS fetch, signature
    verification, entitlement lookup, tenant resolution.
    """
    identity = _dev_identity()

    # Entitlements are whatever `make dev-token` provisioned — this test does
    # not seed its own. It used to, and the copy drifted: it keyed the seed by
    # actor id, while under client_credentials the JWT's `sub` is the client
    # id, so the resolver looked somewhere the test had not written and the
    # call came back 403. Reading the provisioned state instead of restating it
    # is both simpler and a stronger check, since a broken `dev-token` now
    # shows up here.
    with httpx.Client(timeout=10.0) as client:
        # Obtain a real signed JWT via client_credentials.
        token_resp = client.post(
            f"{_MOCK_OIDC_URL}/default/token",
            data={
                "grant_type": "client_credentials",
                "client_id": identity["client_id"],
                "client_secret": identity["client_secret"],
                "scope": "registry",
            },
        )
        assert token_resp.status_code == 200, f"IdP token endpoint failed: {token_resp.status_code} {token_resp.text}"
        access_token = token_resp.json()["access_token"]

        api_resp = client.get(
            f"{_REGISTRY_URL}/v1/whoami",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        # The same request without a token must not be accepted, or the
        # assertion above proves nothing about authentication.
        anon_resp = client.get(f"{_REGISTRY_URL}/v1/whoami")

    assert api_resp.status_code == 200, f"registry rejected a real JWT: {api_resp.status_code} {api_resp.text}"
    body = api_resp.json()
    assert body["tenant_slug"] == identity["tenant_slug"]
    assert body["roles"], "entitlement service resolved no roles for the dev user"

    assert anon_resp.status_code == 401, f"unauthenticated /v1/whoami returned {anon_resp.status_code}, expected 401"
