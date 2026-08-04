"""Middleware registration order, and the two responses that prove it is right.

`add_middleware` inserts at position 0, so the *last* registration is the
outermost — the reverse of how the calls read. Getting it backwards produces an
app that works, serves traffic, and silently under-reports exactly the requests
an operator cares about. That failure is invisible to every test except the two
response-level ones at the bottom of this file.
"""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY

from registry.api.middleware.metrics import MetricsMiddleware
from registry.api.middleware.ratelimit import RateLimitMiddleware
from registry.api.middleware.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from registry.config import Settings
from registry.main import create_app


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


def _classes(app) -> list[type]:
    return [m.cls for m in app.user_middleware]


def test_the_stack_is_ordered_request_id_then_metrics_then_rate_limit() -> None:
    """Asserted against the registered stack, not against a comment.

    `user_middleware` is outermost-first, which is the reverse of registration
    order. Pinning it here means a future `add_middleware` call appended in the
    obvious place fails this test rather than silently becoming the outermost
    layer.
    """
    classes = _classes(create_app(_settings()))
    for cls in (RequestIdMiddleware, MetricsMiddleware, RateLimitMiddleware):
        assert cls in classes, f"{cls.__name__} is not registered"

    assert classes.index(RequestIdMiddleware) < classes.index(MetricsMiddleware)
    assert classes.index(MetricsMiddleware) < classes.index(RateLimitMiddleware)


def _requests_total(status: str, request_type: str) -> float:
    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name != "catalog_requests":
            continue
        for sample in metric.samples:
            if (
                sample.name.endswith("_total")
                and sample.labels.get("status") == status
                and sample.labels.get("type") == request_type
            ):
                total += sample.value
    return total


@pytest.mark.asyncio
async def test_a_throttled_request_is_counted() -> None:
    """The one response that actually distinguishes the two orderings.

    Five of the rate limiter's six exits call downstream, so instrumentation on
    either side of it observes them identically. The sixth sends a 429 itself
    and never calls downstream — so it is counted only when metrics is outside.
    Register metrics inside and the error-rate panel goes flat at exactly the
    moment the service starts shedding load.

    The baseline is read *between* the two requests rather than before both.
    Taken before, the first request's own 401 moves the same counter and the
    assertion passes under either ordering — which is how this test looked when
    it was written, and it passed against a deliberately mis-ordered stack.
    """
    app = create_app(_settings(rate_limit_read_per_minute=1))
    headers = {"authorization": "Bearer order-test-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        # Spends the single available token. Its status is irrelevant.
        await client.get("/v1/capabilities", headers=headers)
        before = _requests_total("4xx", "read")
        throttled = await client.get("/v1/capabilities", headers=headers)

    assert throttled.status_code == 429
    assert _requests_total("4xx", "read") == before + 1


@pytest.mark.asyncio
async def test_an_unauthenticated_request_is_counted_and_correlated() -> None:
    """Not an ordering discriminator — the rate limiter passes these through.

    Kept because the behaviour is still required: a request carrying no bearer
    token must appear in the counter and must carry a correlation id, and the id
    proves the request-id layer is outermost, since this response is produced
    before any route handler runs.
    """
    app = create_app(_settings())

    before = _requests_total("4xx", "read")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/v1/capabilities")

    assert response.status_code == 401
    assert _requests_total("4xx", "read") > before
    assert response.headers.get(REQUEST_ID_HEADER)


@pytest.mark.asyncio
async def test_a_bypassed_probe_is_counted_as_other() -> None:
    # /healthz skips the rate limiter entirely, so it too is only visible from
    # outside — and it must land in `other` rather than dragging the read
    # latency percentile toward zero.
    app = create_app(_settings())

    before = _requests_total("2xx", "other")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert _requests_total("2xx", "other") > before
    assert response.headers.get(REQUEST_ID_HEADER)
