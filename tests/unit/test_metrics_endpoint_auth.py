"""`/metrics` is a credentialed endpoint.

It publishes process-global counters: the full route table, how often
entitlement checks fail, how often the rate limiter rejects, which MCP tools
exist and how often each is called. That is a map of the service's surface and
of how often its authorization fails, and it was previously readable by anyone
who could reach the port — with no rate limit, because /metrics is a bypass
prefix.
"""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient

from registry.config import Settings
from registry.main import create_app

_TOKEN = "scrape-me-c0ffee"


def _settings(**overrides: object) -> Settings:
    base: dict = {
        "database_url": "postgresql+asyncpg://user:pass@localhost:9999/db",
        "pgbouncer_url": "postgresql+asyncpg://user:pass@localhost:9999/db",
        "scheduler_jobstore_url": "postgresql+asyncpg://user:pass@localhost:9999/db",
        "scheduler_use_memory_jobstore": True,
        "embedding_provider": "stub",
        "otlp_endpoint": None,
        "log_format": "json",
        "log_level": logging.INFO,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _get(settings: Settings, headers: dict | None = None):
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.get("/metrics", headers=headers or {})


@pytest.mark.asyncio
async def test_the_right_credential_serves_the_exposition() -> None:
    response = await _get(_settings(metrics_bearer_token=_TOKEN), {"authorization": f"Bearer {_TOKEN}"})
    assert response.status_code == 200
    assert "catalog_requests_total" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong-token"},
        {"authorization": _TOKEN},  # no scheme
        {"authorization": "Basic " + _TOKEN},
        {"authorization": "Bearer "},
    ],
)
async def test_anything_but_the_right_credential_is_401_with_no_payload(headers: dict) -> None:
    response = await _get(_settings(metrics_bearer_token=_TOKEN), headers)
    assert response.status_code == 401
    # The body assertion is the one that matters. A 401 that still carried part
    # of the exposition would hand over the exact thing the credential exists to
    # withhold, and the status code would make it look protected.
    assert response.text == ""
    assert "catalog_requests_total" not in response.text
    assert response.headers.get("www-authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_an_unconfigured_token_refuses_to_serve() -> None:
    """Fails closed, and says why.

    503 rather than 401 because the caller did nothing wrong — the deployment
    is misconfigured. A scraper reports the target down and someone fixes it,
    which is the loud failure. Serving openly instead would produce a
    deployment that looks healthy while publishing to anyone who asks.
    """
    response = await _get(_settings(metrics_bearer_token=None), {"authorization": "Bearer x"})
    assert response.status_code == 503
    assert "METRICS_BEARER_TOKEN" in response.text
    assert "catalog_requests_total" not in response.text


@pytest.mark.asyncio
async def test_an_empty_configured_token_is_treated_as_unset() -> None:
    # An empty string is what an operator gets from an unset env var expanded
    # into a chart value. Accepting it would authenticate every caller that
    # sends `Authorization: Bearer `.
    response = await _get(_settings(metrics_bearer_token=""), {"authorization": "Bearer "})
    assert response.status_code == 503


def test_the_default_is_unset_rather_than_a_value() -> None:
    # A default credential ships in the source and is therefore public; it
    # would be indistinguishable from no credential at all.
    assert _settings().metrics_bearer_token is None
