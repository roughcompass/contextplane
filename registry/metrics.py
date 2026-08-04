"""Request and tool-call metrics for the whole service.

`/metrics` serves the implicit global registry that every `Counter`/`Gauge`/
`Histogram` joins at construction, so defining a metric is enough to publish it.

Metrics live beside their emitters — twenty-odd modules construct their own at
import time, and that is the intended shape: a metric's meaning is the code
around it, and hauling them all here would trade cohesion for a directory
listing. What must be global is the *naming discipline*: one construction per
name per process (the "Duplicate timeseries" error is two constructions under
one name), a `registry_` prefix, and unique names across the codebase — all of
which the conformance suite enforces rather than this module's docstring
merely requesting.

Cardinality discipline
----------------------
Every label below resolves to a member of a closed set fixed in this file. That
is not a style preference — a Prometheus label whose value set grows without
operator action turns one metric into an unbounded number of time series, and
the database falls over long after the code that caused it shipped.

The rule this module applies: a dimension may be a label only if its values are
enumerable here, at authoring time. Route templates qualify — the set changes
when an engineer adds a route, which is a deliberate act, and it is readable from
the app's own route table. Tenants, actors, entities, sessions, and query text do
not, at any count: those sets grow with adoption. Route count and tenant count
are both large numbers and only one of them is a hazard, which is why the test is
about how a set grows rather than how big it is.

The `observe_*` helpers raise on an unrecognised label value rather than passing
it through. A metric that silently accepts a typo publishes a series nobody is
looking at, and the mistake surfaces as a missing line on a dashboard weeks later.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "REQUEST_TYPES",
    "STATUS_CLASSES",
    "STREAMING_PATHS",
    "UNKNOWN_ROUTE",
    "observe_request",
    "observe_sync_run",
    "observe_mcp_tool",
    "observe_mcp_tool_call",
    "observe_audit_write",
    "observe_worker_run",
    "observe_queue_depth",
    "observe_dead_lettered",
    "WORKER_OUTCOMES",
    "sse_connection_opened",
    "sse_connection_closed",
]

# ---------------------------------------------------------------------------
# Closed label vocabularies
# ---------------------------------------------------------------------------

# Read, write, and MCP are the three the shipped dashboard splits latency by.
# `other` is the bucket for everything that is neither an API read nor an API
# write — health probes, the metrics scrape itself, inbound webhooks. Those are
# counted, because "is my health endpoint being hammered" is a real question, but
# they are kept out of `read`/`write` so a 15-second liveness probe cannot drag
# the reported API latency percentile down toward zero.
REQUEST_TYPES: frozenset[str] = frozenset({"read", "write", "mcp", "other"})

# Class, never the raw code. Raw status codes are effectively unbounded once
# proxies, load balancers, and clients are involved, and nobody alerts on the
# difference between 502 and 504 at this layer.
STATUS_CLASSES: frozenset[str] = frozenset({"2xx", "3xx", "4xx", "5xx", "other"})

# Long-lived streaming endpoints, excluded from the request-duration histogram.
# An SSE connection held open for an hour would land as one enormous observation
# against a histogram whose largest bucket is ten seconds, and the dashboard panel
# that reads it is titled "query latency" — it would then report session lifetime
# and be believed. Concurrency is the honest question about a long-lived
# connection, so those get a gauge instead.
#
# A closed set rather than a prefix match: adding a second streaming endpoint
# should be a deliberate line here, visible to the metric-surface gate, not an
# accident of string matching.
STREAMING_PATHS: frozenset[str] = frozenset({"/mcp/sse"})

# The label value for a request that matched no route. A 404 flood must not mint
# one time series per attempted path — which is exactly what using the raw path
# would do, and is the shape an attacker can drive deliberately.
UNKNOWN_ROUTE: str = "other"

# Buckets. Deliberately not `prometheus_client`'s defaults, which are reasonable
# but sparse between 100ms and 500ms — the range where this service's own latency
# bounds sit. Denser there, and the tail is kept because a 10s request is a real
# event worth seeing even though nothing targets it.
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 2.5, 5.0, 10.0)


# ---------------------------------------------------------------------------
# HTTP requests
# ---------------------------------------------------------------------------
#
# The names are not free choices. The shipped Grafana dashboard already queries
# `http_request_duration_seconds_bucket{type=...}` and `catalog_requests_total`,
# and has done since before any code emitted them. Matching those names makes the
# existing panels resolve rather than requiring a second, parallel dashboard.

REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "Wall-clock duration of one HTTP request, measured around the whole downstream call.",
    ["route", "method", "status", "type"],
    buckets=_LATENCY_BUCKETS,
)

REQUESTS_TOTAL = Counter(
    "catalog_requests_total",
    "HTTP requests served, by route template, method, status class, and traffic type.",
    ["route", "method", "status", "type"],
)


def observe_request(*, route: str, method: str, status: str, request_type: str, seconds: float | None) -> None:
    """Count one request, and time it unless it was a streaming connection.

    `seconds is None` means "counted but not timed" — the caller decided this was
    a long-lived connection whose duration would poison the histogram. Passing
    zero instead would be worse than not observing at all: it would pull the
    percentile down while looking like a real measurement.
    """
    if request_type not in REQUEST_TYPES:
        msg = f"unknown request type {request_type!r}; expected one of {sorted(REQUEST_TYPES)}"
        raise ValueError(msg)
    if status not in STATUS_CLASSES:
        msg = f"unknown status class {status!r}; expected one of {sorted(STATUS_CLASSES)}"
        raise ValueError(msg)

    REQUESTS_TOTAL.labels(route=route, method=method, status=status, type=request_type).inc()
    if seconds is not None:
        REQUEST_DURATION_SECONDS.labels(route=route, method=method, status=status, type=request_type).observe(seconds)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------
#
# Which tools are called at all is the cheapest high-information number this
# service can publish, and it is currently unknown. The tool name is a closed set
# by construction — it comes from the registered tool catalog, which changes only
# when someone adds a decorator.

MCP_TOOL_CALLS_TOTAL = Counter(
    "mcp_tool_calls_total",
    "MCP tool invocations, by tool name and outcome status class.",
    ["tool", "status"],
)

MCP_TOOL_DURATION_SECONDS = Histogram(
    "mcp_tool_duration_seconds",
    "Wall-clock duration of one MCP tool invocation.",
    ["tool"],
    buckets=_LATENCY_BUCKETS,
)

# Concurrency, not duration. A connection that is open is the fact worth
# reporting; how long it stayed open is only knowable once it closes, by which
# time the number describes the past rather than the present.
MCP_SSE_CONNECTIONS_ACTIVE = Gauge(
    "mcp_sse_connections_active",
    "MCP server-sent-event connections currently open.",
)


def observe_mcp_tool_call(*, tool: str, status: str, seconds: float) -> None:
    """Count and time one MCP tool call."""
    if status not in STATUS_CLASSES:
        msg = f"unknown status class {status!r}; expected one of {sorted(STATUS_CLASSES)}"
        raise ValueError(msg)
    MCP_TOOL_CALLS_TOTAL.labels(tool=tool, status=status).inc()
    MCP_TOOL_DURATION_SECONDS.labels(tool=tool).observe(seconds)


@contextmanager
def observe_mcp_tool(tool: str) -> Iterator[None]:
    """Time one tool invocation and classify its outcome.

    Two outcomes only, because at this layer there is nothing finer to say: the
    call returned, or it raised. A raising tool is recorded as `5xx` and the
    exception is re-raised untouched — swallowing it here would convert a broken
    tool into a silent one, which is the opposite of what instrumentation is for.
    """
    started = time.perf_counter()
    status = "2xx"
    try:
        yield
    except Exception:
        status = "5xx"
        raise
    finally:
        # Guarded so a metrics failure cannot mask the tool's own result — an
        # exception raised here would replace whatever the tool was returning
        # or, worse, replace the exception it was raising.
        try:
            observe_mcp_tool_call(tool=tool, status=status, seconds=time.perf_counter() - started)
        except Exception:  # pragma: no cover - instrumentation never breaks a call
            pass


def sse_connection_opened() -> None:
    MCP_SSE_CONNECTIONS_ACTIVE.inc()


def sse_connection_closed() -> None:
    MCP_SSE_CONNECTIONS_ACTIVE.dec()


# ---------------------------------------------------------------------------
# Background work
# ---------------------------------------------------------------------------
#
# Unlabeled. A sync source is not a bounded set — an operator adds them at will,
# and one per source is exactly the growth-without-code-change shape the label
# rule forbids. Per-source duration belongs in the sync-run table, which already
# records it and is queryable.

SYNC_RUN_DURATION_SECONDS = Histogram(
    "sync_run_duration_seconds",
    "Wall-clock duration of one completed sync run.",
    buckets=(1.0, 5.0, 15.0, 30.0, 60.0, 300.0, 900.0, 1800.0, 3600.0),
)

# The success counterpart to the failure counter that already exists in the audit
# module. The dashboard's audit panel queries both and has only ever been able to
# draw one, so the panel has shown failures against nothing to compare them to.
AUDIT_WRITES_TOTAL = Counter(
    "catalog_audit_writes_total",
    "Audit-log rows written successfully.",
)


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------
#
# One family across every worker, labelled by worker name, rather than a
# bespoke metric per module. The alternative is what the codebase already
# demonstrates: the extraction drain has its own three metrics under its own
# naming scheme, so a dashboard panel answering "is any background work
# stuck" has to know every worker that exists and be edited whenever one is
# added. A shared family with a `worker` label means a new worker appears on
# the panel by registering, not by someone remembering.
#
# `worker` is a closed set by construction — the values come from the module
# that defines each worker, so the set changes only when an engineer adds one.
# That is the same test the route-template label passes.

WORKER_RUNS_TOTAL = Counter(
    "registry_worker_runs_total",
    "Background worker invocations, by worker name and outcome.",
    ["worker", "outcome"],
)

WORKER_RUN_DURATION_SECONDS = Histogram(
    "registry_worker_run_duration_seconds",
    "Wall-clock duration of one background worker invocation.",
    ["worker"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 300.0),
)

# Depth, not rate. "How far behind is this queue" is the question an operator
# actually asks, and it is the one a counter cannot answer — a counter tells
# you how much was processed, which looks identical whether the backlog is
# empty or growing without bound.
WORKER_QUEUE_DEPTH = Gauge(
    "registry_worker_queue_depth",
    "Rows currently awaiting processing, by queue name.",
    ["queue"],
)

WORKER_DEAD_LETTERED_TOTAL = Counter(
    "registry_worker_dead_lettered_total",
    "Rows abandoned after exhausting retries, by queue name.",
    ["queue"],
)

# Two outcomes, matching the tool-call vocabulary: the run returned, or it
# raised. A worker that raises is invisible otherwise — it is not on any
# request path, so nobody gets an error, and the only symptom is work quietly
# not happening.
WORKER_OUTCOMES: frozenset[str] = frozenset({"ok", "error"})


@contextmanager
def observe_worker_run(worker: str) -> Iterator[None]:
    """Time one worker invocation and record whether it completed.

    The exception is re-raised. A scheduler that sees the failure can back off
    or alert; one that sees a swallowed success cannot.
    """
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        try:
            WORKER_RUNS_TOTAL.labels(worker=worker, outcome=outcome).inc()
            WORKER_RUN_DURATION_SECONDS.labels(worker=worker).observe(time.perf_counter() - started)
        except Exception:  # pragma: no cover - instrumentation never breaks a worker
            pass


def observe_queue_depth(*, queue: str, depth: int) -> None:
    """Report how many rows are waiting. Safe to call every tick."""
    WORKER_QUEUE_DEPTH.labels(queue=queue).set(depth)


def observe_dead_lettered(*, queue: str, count: int = 1) -> None:
    WORKER_DEAD_LETTERED_TOTAL.labels(queue=queue).inc(count)


def observe_sync_run(*, seconds: float) -> None:
    SYNC_RUN_DURATION_SECONDS.observe(seconds)


def observe_audit_write() -> None:
    AUDIT_WRITES_TOTAL.inc()
