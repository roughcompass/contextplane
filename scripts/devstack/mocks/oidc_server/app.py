"""Mock OIDC provider — single-file FastAPI app that issues real JWTs.

Stands in for the enterprise identity provider in local development. It
is a drop-in replacement for the containerised mock IDP: same URL shape,
same three endpoints, same token semantics, so `OIDC_DISCOVERY_URL`,
`OIDC_ISSUER_ALLOWLIST`, and `make dev-jwt` do not change depending on
which one is running.

Run standalone: `uvicorn scripts.devstack.mocks.oidc_server.app:app --port 8090`.

What it deliberately does *not* do is let the application skip
authentication. The app performs a real discovery fetch, a real JWKS
fetch, and real RS256 signature verification against these endpoints —
the same code path that runs against the enterprise IDP in production.
A test that passes because auth was switched off proves nothing, so
there is no switch to throw.

Endpoints (`{issuer_id}` is `default` unless something asks for another):

- ``GET  /{issuer_id}/.well-known/openid-configuration``
- ``GET  /{issuer_id}/jwks``
- ``POST /{issuer_id}/token`` — ``client_credentials`` grant
- ``GET  /healthz``

Signing material is the committed test RSA key from
``tests/helpers/jwt_factory``. It is public on purpose and must never be
used anywhere real.

**Never deployed to production.**
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, status

from ..jwt_factory import get_test_jwks, make_jwt

# Lifetime of minted tokens. The containerised mock issues 3600s tokens,
# and the compose stack raises OIDC_MAX_TOKEN_TTL_SECONDS to match, so
# the same default here keeps a token minted under one provider
# acceptable under the other. Lower it to exercise the production
# ceiling (900s) without editing this file.
DEFAULT_TOKEN_TTL_SECONDS = int(os.environ.get("DEVSTACK_TOKEN_TTL_SECONDS", "3600"))

# Audience applied when the caller names none. Matches the conventional
# resource URI in RESOURCE_URI_ALLOWLIST; a token with any other audience
# is rejected by the validator.
DEFAULT_AUDIENCE = "registry"

DEFAULT_ISSUER_ID = "default"

app = FastAPI(
    title="mock-oidc-server",
    description="Local-development OIDC provider. Never deployed to production.",
)


def _issuer_for(request: Request, issuer_id: str) -> str:
    """Build the issuer URL from the request's own host.

    The containerised mock derives its issuer from the request Host, which
    is why the compose stack allowlists both the in-network and the
    published-port spelling of the issuer. Mirroring that here means a
    token minted through one hostname carries the issuer a caller at that
    hostname expects, and `OIDC_ISSUER_ALLOWLIST` behaves identically
    under either provider.
    """
    base = str(request.base_url).rstrip("/")
    return f"{base}/{issuer_id}"


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/{issuer_id}/.well-known/openid-configuration")
async def discovery(request: Request, issuer_id: str = DEFAULT_ISSUER_ID) -> dict[str, Any]:
    """OIDC discovery document.

    The validator reads exactly two fields — `issuer` and `jwks_uri` —
    but the rest are cheap and make the endpoint usable by generic OIDC
    tooling a developer might point at it.
    """
    issuer = _issuer_for(request, issuer_id)
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/jwks",
        "token_endpoint": f"{issuer}/token",
        "authorization_endpoint": f"{issuer}/authorize",
        "response_types_supported": ["code", "token", "id_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "grant_types_supported": ["client_credentials", "authorization_code"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
        ],
        "scopes_supported": ["openid", DEFAULT_AUDIENCE],
    }


@app.get("/{issuer_id}/jwks")
async def jwks(issuer_id: str = DEFAULT_ISSUER_ID) -> dict[str, Any]:
    """Public half of the signing key, in JWKS form."""
    return get_test_jwks()


@app.post("/{issuer_id}/token")
async def token(
    request: Request,
    issuer_id: str = DEFAULT_ISSUER_ID,
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(default=""),
    scope: str = Form(default=""),
    audience: str = Form(default=""),
) -> dict[str, Any]:
    """Mint a signed access token for the client_credentials grant.

    Any client_id/client_secret pair is accepted, matching the
    containerised mock's default configuration — the dev bootstrap
    "registers" a client by doing nothing more than confirming this
    server is reachable. Credential rejection is not the behaviour under
    test locally; token validation is.
    """
    if grant_type != "client_credentials":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unsupported_grant_type",
                "error_description": (
                    f"{grant_type!r} is not supported; this mock issues " "client_credentials tokens only"
                ),
            },
        )

    # Under client_credentials there is no end user, so `sub` is the
    # client itself. The dev bootstrap relies on this: it seeds the mock
    # entitlement service under the client_id precisely because that is
    # the identity the registry will resolve from the token.
    subject = client_id
    resolved_audience = audience or scope or DEFAULT_AUDIENCE
    issued_at = int(time.time())

    access_token = make_jwt(
        sub=subject,
        iss=_issuer_for(request, issuer_id),
        aud=resolved_audience,
        iat=issued_at,
        exp=issued_at + DEFAULT_TOKEN_TTL_SECONDS,
        azp=client_id,
        name=subject,
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": DEFAULT_TOKEN_TTL_SECONDS,
        "scope": scope or DEFAULT_AUDIENCE,
    }
