"""OTel SDK bootstrap — the tracer provider `create_app` installs at startup.

Split out of `contextplane.main` so the exporter's timeout tuning (the reason
this function exists at all — see the docstring below) can be read, tested,
and changed without wading through the rest of the app factory.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from contextplane.config import Settings


def _init_otel(settings: Settings) -> TracerProvider | None:
    """Initialize the OTel SDK with OTLP HTTP export. No-op when otlp_endpoint is None.

    The exporter is given an explicit per-attempt timeout so that a slow or
    unreachable collector cannot block the BatchSpanProcessor worker thread for
    longer than the configured limit.  The default (10 s) plus exponential-backoff
    retries (up to 64 s total) are too long: a stalling export run fills the span
    queue and eventually causes span drops while the worker is occupied.  A short
    timeout fails fast, lets the worker move on, and keeps queue pressure low.

    Returns the provider so shutdown can flush it. The SDK installs an
    ``atexit`` hook of its own, which is not enough: it does not run when the
    process is killed by a signal, and that is the ordinary end of a container
    that has outstayed its grace period. Whatever is still queued at that point
    is evidence the service collected and then discarded.
    """
    if settings.otlp_endpoint is None:
        return None
    resource = Resource.create({"service.name": settings.service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=settings.otlp_endpoint,
        timeout=settings.otlp_exporter_timeout_s,
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider
