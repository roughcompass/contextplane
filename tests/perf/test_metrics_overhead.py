"""What the instrumentation costs, measured rather than assumed.

Two numbers are asserted against every request the service serves, so both are
worth measuring rather than reasoning about:

  * added latency per request, against a build with the middlewares removed;
  * the scrape latency of `/metrics` itself at the current series count.

Neither test needs a database. The comparison is against an app whose route
handler does nothing, which isolates the middleware chain — the point is the
delta, and any real work in the handler only makes the instrumentation look
cheaper by dilution.
"""

from __future__ import annotations

import statistics
import time

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from registry.api.middleware.metrics import MetricsMiddleware
from registry.api.middleware.request_id import RequestIdMiddleware

pytestmark = [pytest.mark.perf, pytest.mark.slow]

_ITERATIONS = 2000
_WARMUP = 200

# NF targets, restated here so a failure names the number it broke.
_MAX_ADDED_P99_MS = 1.0
_MAX_SCRAPE_P95_MS = 250.0


def _bare_app() -> Starlette:
    async def handler(request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/v1/thing", handler)])


def _instrumented_app() -> Starlette:
    app = _bare_app()
    # Same order as create_app: request-id outermost, then metrics.
    return RequestIdMiddleware(MetricsMiddleware(app))  # type: ignore[return-value]


async def _timings(app, path: str = "/v1/thing") -> list[float]:
    """Per-request wall-clock in milliseconds, warm-up discarded."""
    samples: list[float] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for _ in range(_WARMUP):
            await client.get(path)
        for _ in range(_ITERATIONS):
            started = time.perf_counter()
            await client.get(path)
            samples.append((time.perf_counter() - started) * 1000.0)
    return samples


def _pct(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    index = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[index]


@pytest.mark.asyncio
async def test_instrumentation_adds_under_a_millisecond_at_p99() -> None:
    """Measured as a delta against the same app without the middlewares.

    An absolute latency assertion would measure the machine rather than the
    change — a slow runner fails a correct implementation, and a fast one
    passes a regression. The delta is what the requirement is actually about.
    """
    baseline = await _timings(_bare_app())
    instrumented = await _timings(_instrumented_app())

    added_p99 = _pct(instrumented, 0.99) - _pct(baseline, 0.99)
    added_median = statistics.median(instrumented) - statistics.median(baseline)

    print(
        f"\nbaseline    p50={statistics.median(baseline):.4f}ms "
        f"p99={_pct(baseline, 0.99):.4f}ms"
        f"\ninstrumented p50={statistics.median(instrumented):.4f}ms "
        f"p99={_pct(instrumented, 0.99):.4f}ms"
        f"\nadded        p50={added_median:.4f}ms p99={added_p99:.4f}ms"
    )

    assert (
        added_p99 <= _MAX_ADDED_P99_MS
    ), f"instrumentation adds {added_p99:.4f}ms at p99, over the {_MAX_ADDED_P99_MS}ms budget"


async def _populate_series() -> None:
    """Drive traffic across the real route table to mint label combinations."""
    import logging

    from registry.config import Settings
    from registry.main import create_app

    app = create_app(
        Settings(  # type: ignore[arg-type]
            database_url="postgresql+asyncpg://user:pass@localhost:9999/db",
            pgbouncer_url="postgresql+asyncpg://user:pass@localhost:9999/db",
            scheduler_jobstore_url="postgresql+asyncpg://user:pass@localhost:9999/db",
            scheduler_use_memory_jobstore=True,
            embedding_provider="stub",
            otlp_endpoint=None,
            log_format="json",
            log_level=logging.INFO,
        )
    )
    paths = [r.path for r in app.routes if isinstance(getattr(r, "path", None), str)]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for path in paths:
            for method in ("GET", "POST"):
                try:
                    await client.request(method, path.replace("{", "x").replace("}", ""))
                except Exception:
                    pass


@pytest.mark.asyncio
async def test_the_metrics_scrape_stays_under_the_latency_budget() -> None:
    """Scrape cost grows with series count, so this is the cardinality canary.

    A label whose values grow with adoption shows up here as a scrape that
    slowly gets slower — long before it shows up as a Prometheus that fell
    over. Exercised against `generate_latest()` directly rather than the
    endpoint so the number is the serialization cost, not the HTTP stack.
    """
    # Populate the registry with a realistic series count first. A fresh
    # process exposes almost nothing, and measuring that would report the cost
    # of serialising an empty registry — which is not the number the budget is
    # about, and would not move even if cardinality exploded.
    await _populate_series()

    for _ in range(20):
        generate_latest()

    samples = []
    for _ in range(200):
        started = time.perf_counter()
        payload = generate_latest()
        samples.append((time.perf_counter() - started) * 1000.0)

    p95 = _pct(samples, 0.95)
    series = payload.decode().count("\n")
    print(f"\nscrape p50={statistics.median(samples):.3f}ms p95={p95:.3f}ms lines={series}")

    assert CONTENT_TYPE_LATEST
    assert p95 < _MAX_SCRAPE_P95_MS, f"scrape p95 {p95:.3f}ms exceeds {_MAX_SCRAPE_P95_MS}ms"
