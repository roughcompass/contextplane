"""Local traces + metrics sink, standing in for Jaeger, Prometheus and Grafana.

Those three are excellent and this is not a replacement for them. It
exists because they ship as container images or downloaded binaries, and
some environments permit neither. What it covers is the two things the
inner dev loop actually needs from them:

- *show me the spans for the request I just made*
- *show me a metric move while I poke at the API*

What it does not cover: PromQL, alerting, dashboard editing, retention
beyond the process lifetime, or anything resembling a production
observability posture. A developer who needs those should run the
compose stack, which still has the real thing.

Three jobs in one process, no dependencies beyond what the app already
installs:

- OTLP/HTTP trace receiver on `/v1/traces`. The app exports through
  `opentelemetry.exporter.otlp.proto.http`, so the body is a protobuf
  `ExportTraceServiceRequest`; `opentelemetry-proto` ships with the
  exporter and decodes it.
- A scraper polling the API's `/metrics` endpoint on the same interval
  `prometheus.yml` uses.
- A viewer on Jaeger's port, so existing docs and muscle memory land
  somewhere useful.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from prometheus_client.parser import text_string_to_metric_families

_log = logging.getLogger("devstack.obs")

# Ring-buffer sizes. Everything is in memory and dies with the process —
# this is a dev aid, not a datastore.
MAX_TRACES = 500
MAX_SAMPLES_PER_METRIC = 240  # ~1 hour at the default scrape interval

DEFAULT_SCRAPE_INTERVAL_S = 15.0

# Retry cadence before the first successful scrape. See `_scrape_loop`.
STARTUP_SCRAPE_INTERVAL_S = 1.0


@dataclass
class Span:
    """One span, flattened out of the OTLP protobuf into something printable."""

    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    kind: int
    start_ns: int
    end_ns: int
    status_code: int
    service: str
    attributes: dict[str, Any]

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000


@dataclass
class Trace:
    """Spans sharing a trace id."""

    trace_id: str
    spans: list[Span] = field(default_factory=list)

    @property
    def root(self) -> Span | None:
        by_id = {s.span_id for s in self.spans}
        for span in self.spans:
            if not span.parent_span_id or span.parent_span_id not in by_id:
                return span
        return self.spans[0] if self.spans else None

    @property
    def start_ns(self) -> int:
        return min((s.start_ns for s in self.spans), default=0)

    @property
    def duration_ms(self) -> float:
        if not self.spans:
            return 0.0
        return (max(s.end_ns for s in self.spans) - self.start_ns) / 1_000_000

    @property
    def errored(self) -> bool:
        # OTLP status code 2 is STATUS_CODE_ERROR.
        return any(s.status_code == 2 for s in self.spans)


class Store:
    """In-memory traces and metric series."""

    def __init__(self) -> None:
        self.traces: dict[str, Trace] = {}
        self.order: deque[str] = deque(maxlen=MAX_TRACES)
        self.metrics: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=MAX_SAMPLES_PER_METRIC))
        self.scrape_error: str | None = None

    def add_spans(self, spans: list[Span]) -> None:
        for span in spans:
            trace = self.traces.get(span.trace_id)
            if trace is None:
                trace = Trace(trace_id=span.trace_id)
                self.traces[span.trace_id] = trace
                self.order.append(span.trace_id)
                # deque(maxlen) drops from the left silently; drop the
                # matching trace so the dict does not grow without bound.
                if len(self.traces) > MAX_TRACES:
                    live = set(self.order)
                    for stale in [t for t in self.traces if t not in live]:
                        del self.traces[stale]
            trace.spans.append(span)

    def add_metric_sample(self, name: str, value: float, at: float) -> None:
        self.metrics[name].append((at, value))

    def recent_traces(self, limit: int = 50) -> list[Trace]:
        ids = list(self.order)[-limit:]
        traces = [self.traces[i] for i in ids if i in self.traces]
        return sorted(traces, key=lambda t: t.start_ns, reverse=True)


def _hex(raw: bytes) -> str:
    return raw.hex()


def decode_traces(body: bytes) -> list[Span]:
    """Decode an OTLP/HTTP protobuf export request into Spans."""
    request = ExportTraceServiceRequest()
    request.ParseFromString(body)

    spans: list[Span] = []
    for resource_spans in request.resource_spans:
        service = "unknown"
        for attr in resource_spans.resource.attributes:
            if attr.key == "service.name":
                service = attr.value.string_value
        for scope_spans in resource_spans.scope_spans:
            for proto_span in scope_spans.spans:
                spans.append(
                    Span(
                        trace_id=_hex(proto_span.trace_id),
                        span_id=_hex(proto_span.span_id),
                        parent_span_id=_hex(proto_span.parent_span_id),
                        name=proto_span.name,
                        kind=int(proto_span.kind),
                        start_ns=int(proto_span.start_time_unix_nano),
                        end_ns=int(proto_span.end_time_unix_nano),
                        status_code=int(proto_span.status.code),
                        service=service,
                        attributes={attr.key: _attr_value(attr.value) for attr in proto_span.attributes},
                    )
                )
    return spans


def _attr_value(value: Any) -> Any:
    """Unwrap an OTLP AnyValue into a plain Python value."""
    for attribute in ("string_value", "int_value", "double_value", "bool_value"):
        if value.HasField(attribute):
            return getattr(value, attribute)
    return str(value).strip()


async def _scrape_loop(store: Store, metrics_url: str, interval: float) -> None:
    """Poll the API's /metrics endpoint into the store until cancelled.

    The sink starts before the API does — it owns the OTLP port the API
    exports to — so the first few scrapes are expected to fail. Until one
    succeeds, retry quickly rather than on the full interval: a developer
    who runs `make dev-up` and opens the viewer should not be looking at
    an empty metrics table for fifteen seconds over a service that is
    already up.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        scraped_once = False
        while True:
            try:
                response = await client.get(metrics_url)
                response.raise_for_status()
                now = time.time()
                for family in text_string_to_metric_families(response.text):
                    for sample in family.samples:
                        label_suffix = ""
                        if sample.labels:
                            rendered = ",".join(f"{k}={v}" for k, v in sorted(sample.labels.items()))
                            label_suffix = f"{{{rendered}}}"
                        store.add_metric_sample(f"{sample.name}{label_suffix}", float(sample.value), now)
                store.scrape_error = None
                scraped_once = True
            except (httpx.HTTPError, ValueError) as exc:
                # Almost always "the API is not listening yet". Not worth a
                # stack trace every interval.
                store.scrape_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(interval if scraped_once else STARTUP_SCRAPE_INTERVAL_S)


def create_app(metrics_url: str, scrape_interval: float = DEFAULT_SCRAPE_INTERVAL_S) -> FastAPI:
    """Build the sink app.

    The scraper is *not* started here. This one app is served on two
    ports, so anything tied to application startup would run twice and
    leave a duplicate task polling in the background. `serve()` owns the
    scraper's lifetime instead.
    """
    store = Store()
    app = FastAPI(title="devstack-obs-sink")
    app.state.store = store
    app.state.metrics_url = metrics_url
    app.state.scrape_interval = scrape_interval

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "traces": len(store.traces),
            "metric_series": len(store.metrics),
            "scrape_error": store.scrape_error,
        }

    @app.post("/v1/traces")
    async def ingest_traces(request: Request) -> Response:
        body = await request.body()
        try:
            spans = decode_traces(body)
        except Exception as exc:
            _log.warning("failed to decode OTLP payload: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=400)
        store.add_spans(spans)
        # OTLP expects a serialised ExportTraceServiceResponse; an empty
        # body is a valid one and the exporter is happy with it.
        return Response(content=b"", media_type="application/x-protobuf")

    @app.get("/api/traces")
    async def list_traces(limit: int = 50) -> dict[str, Any]:
        return {
            "traces": [
                {
                    "trace_id": t.trace_id,
                    "root": t.root.name if t.root else None,
                    "spans": len(t.spans),
                    "duration_ms": round(t.duration_ms, 2),
                    "errored": t.errored,
                    "start": t.start_ns,
                }
                for t in store.recent_traces(limit)
            ]
        }

    @app.get("/api/traces/{trace_id}")
    async def get_trace(trace_id: str) -> dict[str, Any]:
        trace = store.traces.get(trace_id)
        if trace is None:
            return {"error": "not found"}
        return {
            "trace_id": trace_id,
            "spans": [
                {
                    "name": s.name,
                    "span_id": s.span_id,
                    "parent_span_id": s.parent_span_id,
                    "service": s.service,
                    "duration_ms": round(s.duration_ms, 3),
                    "start_ns": s.start_ns,
                    "status_code": s.status_code,
                    "attributes": s.attributes,
                }
                for s in sorted(trace.spans, key=lambda s: s.start_ns)
            ],
        }

    @app.get("/api/metrics")
    async def get_metrics() -> dict[str, Any]:
        return {
            "scrape_error": store.scrape_error,
            "series": {name: [{"t": t, "v": v} for t, v in samples] for name, samples in sorted(store.metrics.items())},
        }

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _render_index(store)

    @app.get("/trace/{trace_id}", response_class=HTMLResponse)
    async def trace_page(trace_id: str) -> str:
        return _render_trace(store, trace_id)

    return app


# --- rendering -----------------------------------------------------------
# Plain server-rendered HTML on purpose: no build step, no CDN fetch, and
# it works in an environment with no outbound network access.

_STYLE = """
  body { font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
         margin: 0; background: #12141a; color: #dfe3ec; }
  header { padding: 14px 20px; border-bottom: 1px solid #262a35; }
  h1 { font-size: 15px; margin: 0; font-weight: 600; }
  .sub { color: #7c8496; font-size: 12px; margin-top: 3px; }
  main { padding: 16px 20px 48px; }
  h2 { font-size: 13px; margin: 22px 0 8px; color: #9aa3b8;
       text-transform: uppercase; letter-spacing: .07em; }
  table { border-collapse: collapse; width: 100%; }
  th { text-align: left; color: #7c8496; font-weight: 500;
       border-bottom: 1px solid #262a35; padding: 5px 8px; }
  td { padding: 4px 8px; border-bottom: 1px solid #1c1f28; }
  tr:hover td { background: #171a22; }
  a { color: #7cc4ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .err { color: #ff8b7b; }
  .ok { color: #86d492; }
  .muted { color: #7c8496; }
  .bar { background: #3b6fd4; height: 11px; border-radius: 2px; display: inline-block;
         min-width: 2px; vertical-align: middle; }
  .barwrap { background: #1c1f28; border-radius: 2px; width: 340px;
             display: inline-block; vertical-align: middle; }
  .empty { color: #7c8496; padding: 10px 8px; }
  code { color: #d5b3ff; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<meta http-equiv='refresh' content='10'>"
        f"<style>{_STYLE}</style></head><body>{body}</body></html>"
    )


def _render_index(store: Store) -> str:
    traces = store.recent_traces(50)
    if traces:
        rows = "".join(
            "<tr>"
            f"<td><a href='/trace/{t.trace_id}'>{t.trace_id[:16]}…</a></td>"
            f"<td>{html.escape(t.root.name if t.root else '—')}</td>"
            f"<td>{len(t.spans)}</td>"
            f"<td>{t.duration_ms:.1f} ms</td>"
            f"<td class='{'err' if t.errored else 'ok'}'>"
            f"{'error' if t.errored else 'ok'}</td>"
            "</tr>"
            for t in traces
        )
        trace_table = (
            "<table><tr><th>trace</th><th>root span</th><th>spans</th>"
            f"<th>duration</th><th>status</th></tr>{rows}</table>"
        )
    else:
        trace_table = (
            "<div class='empty'>No spans received yet. Make a request to the API "
            "&mdash; tracing is on whenever <code>OTLP_ENDPOINT</code> is set.</div>"
        )

    metric_rows = []
    for name, samples in sorted(store.metrics.items()):
        if not samples:
            continue
        latest = samples[-1][1]
        first = samples[0][1]
        delta = latest - first
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "·")
        metric_rows.append(
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{latest:g}</td>"
            f"<td class='muted'>{arrow} {delta:+g} over {len(samples)} samples</td>"
            "</tr>"
        )
    if metric_rows:
        metric_table = (
            "<table><tr><th>metric</th><th>latest</th><th>change</th></tr>" + "".join(metric_rows) + "</table>"
        )
    else:
        detail = f" Last scrape failed: {html.escape(store.scrape_error)}" if store.scrape_error else ""
        metric_table = f"<div class='empty'>No metrics scraped yet.{detail}</div>"

    return _page(
        "devstack observability",
        "<header><h1>devstack observability</h1>"
        "<div class='sub'>Traces and metrics for the local stack. "
        "In-memory only &mdash; cleared when the stack stops. "
        "Refreshes every 10s.</div></header>"
        f"<main><h2>Recent traces</h2>{trace_table}"
        f"<h2>Metrics</h2>{metric_table}</main>",
    )


def _render_trace(store: Store, trace_id: str) -> str:
    trace = store.traces.get(trace_id)
    if trace is None:
        return _page("trace not found", "<main><p>No such trace.</p></main>")

    spans = sorted(trace.spans, key=lambda s: s.start_ns)
    base = trace.start_ns
    total = max((s.end_ns for s in spans), default=base) - base or 1

    rows = []
    for span in spans:
        offset_pct = (span.start_ns - base) / total * 100
        width_pct = max((span.end_ns - span.start_ns) / total * 100, 0.4)
        attrs = ", ".join(f"{k}={v}" for k, v in sorted(span.attributes.items()))
        rows.append(
            "<tr>"
            f"<td>{html.escape(span.name)}</td>"
            f"<td>{span.duration_ms:.3f} ms</td>"
            "<td><span class='barwrap'>"
            f"<span class='bar' style='margin-left:{offset_pct:.2f}%;"
            f"width:{width_pct:.2f}%'></span></span></td>"
            f"<td class='muted'>{html.escape(attrs[:160])}</td>"
            "</tr>"
        )

    return _page(
        f"trace {trace_id[:16]}",
        f"<header><h1>trace {html.escape(trace_id)}</h1>"
        f"<div class='sub'>{len(spans)} spans &middot; "
        f"{trace.duration_ms:.1f} ms total &middot; "
        f"<a href='/'>back</a></div></header>"
        "<main><table><tr><th>span</th><th>duration</th><th>timeline</th>"
        f"<th>attributes</th></tr>{''.join(rows)}</table></main>",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4318, help="OTLP ingest port")
    parser.add_argument("--viewer-port", type=int, default=16686, help="viewer port")
    parser.add_argument(
        "--metrics-url",
        default="http://localhost:8000/metrics",
        help="API metrics endpoint to scrape",
    )
    parser.add_argument(
        "--scrape-interval",
        type=float,
        default=DEFAULT_SCRAPE_INTERVAL_S,
        help="seconds between metric scrapes",
    )
    args = parser.parse_args(argv)

    app = create_app(args.metrics_url, args.scrape_interval)
    store: Store = app.state.store

    async def _serve() -> None:
        # One app, two ports: OTLP ingest keeps Jaeger's collector port so
        # OTLP_ENDPOINT is unchanged, and the viewer keeps Jaeger's UI port
        # so the docs' links still land somewhere.
        configs = [
            uvicorn.Config(app, host="localhost", port=args.port, log_level="warning"),
            uvicorn.Config(app, host="localhost", port=args.viewer_port, log_level="warning"),
        ]
        servers = [uvicorn.Server(c) for c in configs]
        scraper = asyncio.create_task(_scrape_loop(store, args.metrics_url, args.scrape_interval))
        try:
            await asyncio.gather(*(s.serve() for s in servers))
        finally:
            scraper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scraper

    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
