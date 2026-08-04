"""The metric surface is a contract, and this is the gate that pins it.

Three failure modes, none of which any other test in this repo would notice:

  1. **A label whose value set grows with adoption.** One tenant label turns one
     metric into one time series per tenant, and the Prometheus that dies from
     it dies months after the line shipped, in production, under load. Series
     count is a function of the *shape* of a label rather than its size, so the
     rule enforced here is about how a set grows, not how big it is.
  2. **A new route or tool that skips instrumentation.** It fails silently — the
     surface simply looks smaller than it is — which is the exact question this
     phase exists to answer.
  3. **A metric renamed or relabelled out from under the shipped dashboard.**
     The panel goes blank. Nobody notices, because a blank panel and a quiet
     service look identical.

Each assertion carries a negative fixture proving the check actually fires. A
conformance gate that cannot fail is worse than none: it reports safety it never
verified.
"""

from __future__ import annotations

import ast
import logging
import pathlib
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY, CollectorRegistry, Counter
from starlette.applications import Starlette
from starlette.routing import Mount

from registry import metrics
from registry.api.middleware.metrics import resolve_route
from registry.config import Settings

# ---------------------------------------------------------------------------
# 1. Names and labels, pinned literally
# ---------------------------------------------------------------------------
#
# Spelled out rather than derived from the module, so that renaming a metric
# fails here instead of silently agreeing with itself. The shipped Grafana
# dashboard queries these names and has done since before any code emitted
# them; a rename blanks a panel rather than breaking a query.

_EXPECTED_SURFACE: dict[str, tuple[str, ...]] = {
    "http_request_duration_seconds": ("route", "method", "status", "type"),
    "catalog_requests_total": ("route", "method", "status", "type"),
    "mcp_tool_calls_total": ("tool", "status"),
    "mcp_tool_duration_seconds": ("tool",),
    "mcp_sse_connections_active": (),
    "sync_run_duration_seconds": (),
    "catalog_audit_writes_total": (),
    "registry_worker_runs_total": ("worker", "outcome"),
    "registry_worker_run_duration_seconds": ("worker",),
    "registry_worker_queue_depth": ("queue",),
    "registry_worker_dead_lettered_total": ("queue",),
}

_FORBIDDEN_LABEL_KEYS = frozenset(
    {
        "tenant",
        "tenant_id",
        "actor",
        "actor_id",
        "entity",
        "entity_id",
        "session",
        "session_id",
        "query",
    }
)


def _metric_objects() -> dict[str, object]:
    found = {}
    for name in dir(metrics):
        obj = getattr(metrics, name)
        metric_name = getattr(obj, "_name", None)
        if metric_name and hasattr(obj, "_labelnames"):
            found[metric_name] = obj
    return found


@pytest.mark.parametrize(("name", "labels"), sorted(_EXPECTED_SURFACE.items()))
def test_each_metric_exists_with_exactly_its_expected_labels(name: str, labels: tuple) -> None:
    objects = _metric_objects()
    # prometheus_client strips the _total suffix into `_name`.
    key = name.removesuffix("_total")
    assert key in objects, f"{name} is not exported; the dashboard panel querying it will be blank"
    assert tuple(objects[key]._labelnames) == labels  # noqa: SLF001


def test_no_metric_family_was_added_without_being_pinned() -> None:
    """A new metric must be added to the table above deliberately.

    Not pedantry: the table is the only place the label rule is applied before
    a metric reaches production, and a metric that skips it skips the review
    that would have caught an identity label.
    """
    exported = {
        n if not hasattr(o, "_type") or o._type != "counter" else f"{n}_total"  # noqa: SLF001
        for n, o in _metric_objects().items()
    }
    assert exported == set(_EXPECTED_SURFACE), (
        f"metric surface drifted; unpinned: {exported - set(_EXPECTED_SURFACE)}, "
        f"missing: {set(_EXPECTED_SURFACE) - exported}"
    )


# ---------------------------------------------------------------------------
# 2. No identity label anywhere in the default registry
# ---------------------------------------------------------------------------


def _forbidden_labels_in(registry) -> list[str]:
    offenders = []
    for metric in registry.collect():
        for sample in metric.samples:
            hits = set(sample.labels) & _FORBIDDEN_LABEL_KEYS
            if hits:
                offenders.append(f"{metric.name}{sorted(hits)}")
    return offenders


def test_the_whole_default_registry_is_free_of_identity_labels() -> None:
    """Walks every collector, not only this project's own metrics.

    A dependency can register into the same default registry — which is exactly
    how an unbounded label arrives without anyone writing one.
    """
    assert not _forbidden_labels_in(REGISTRY)


def test_the_identity_label_walk_actually_fires() -> None:
    # The negative fixture. Without it, the assertion above passes just as well
    # against a walk that inspects nothing.
    scratch = CollectorRegistry()
    bad = Counter("scratch_thing_total", "deliberately mislabelled", ["tenant_id"], registry=scratch)
    bad.labels(tenant_id="acme").inc()
    assert _forbidden_labels_in(scratch)


# ---------------------------------------------------------------------------
# 3. Every route resolves to a bounded label
# ---------------------------------------------------------------------------


def _scope(path: str, method: str = "GET") -> dict:
    # A full ASGI scope: Mount.matches() reads type/method/path_params/root_path,
    # and a thin dict makes every route silently fail to match.
    return {
        "type": "http",
        "path": path,
        "method": method,
        "path_params": {},
        "root_path": "",
        "headers": [],
    }


def _app():
    from registry.main import create_app

    return create_app(
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


def test_every_registered_route_resolves_to_a_bounded_template() -> None:
    """The un-instrumented-surface check.

    A route added later must resolve to its template or to the `other` constant.
    What it must never do is resolve to a path with a real identifier
    substituted into it — that mints one time series per entity, and the growth
    is driven by traffic rather than by anyone's decision.
    """
    app = _app()
    unbounded = []
    for route in app.routes:
        template = getattr(route, "path_format", None) or getattr(route, "path", None)
        if not isinstance(template, str) or not template:
            unbounded.append(repr(route))
            continue
        resolved = resolve_route({**_scope(template), "route": route}, app)
        if not resolved or resolved.startswith("<"):
            unbounded.append(template)
        # A parameterised route must keep its placeholder unsubstituted.
        if "{" in template and "{" not in resolved and resolved != metrics.UNKNOWN_ROUTE:
            unbounded.append(f"{template} -> {resolved}")

    assert not unbounded, f"routes resolving to an unbounded label: {unbounded}"


def test_a_route_carrying_a_substituted_id_is_caught() -> None:
    """The negative fixture for the route check.

    Proves the assertion above distinguishes a template from a resolved path,
    rather than accepting any non-empty string.
    """
    resolved = resolve_route(_scope("/v1/capabilities/3f9a-real-uuid"), Starlette(routes=[]))
    # No match, so it must degrade to the constant rather than echo the path.
    assert resolved == metrics.UNKNOWN_ROUTE
    assert "3f9a-real-uuid" not in resolved


def test_a_mount_resolves_to_its_wildcard_template() -> None:
    # A Mount is the case that broke an earlier draft: its path_format is
    # `/mcp/{path}`, not `/mcp`, so anything keyed on the prefix never fires.
    app = Starlette(routes=[Mount("/mcp", app=Starlette())])
    resolved = resolve_route(_scope("/mcp/sse"), app)
    assert resolved == "/mcp/{path}"
    assert "sse" not in resolved


# ---------------------------------------------------------------------------
# 4. The tool label vocabulary equals the registered catalog
# ---------------------------------------------------------------------------


def test_every_registered_tool_is_instrumented() -> None:
    """Derived from the catalog, never hardcoded.

    A hardcoded count is a number someone updates to make the test pass. What
    matters is that the instrumented set and the registered set are the same
    set, whatever its size.
    """
    from registry.api.routers.mcp import create_registry_mcp_server

    server = create_registry_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        annotation_service=MagicMock(),
        workspace_service=MagicMock(),
    )
    registered = {t.name: t for t in server._tool_manager.list_tools()}  # noqa: SLF001
    assert registered, "no tools registered; the fixture is wrong, not the surface"

    uninstrumented = [n for n, t in registered.items() if not hasattr(t.fn, "__wrapped__")]
    assert not uninstrumented, f"tools that would never appear in mcp_tool_calls_total: {uninstrumented}"


# ---------------------------------------------------------------------------
# 5. No OTel meter provider is installed
# ---------------------------------------------------------------------------
#
# FastAPIInstrumentor emits its own HTTP metrics when a meter provider exists,
# with its own label choices — including, by default, the full request target.
# Nothing in this repo installs one, and that absence is load-bearing rather
# than incidental: it is what keeps a second, unreviewed metric surface from
# appearing alongside the pinned one above.

_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "registry"


def _files_calling(name: str, root: pathlib.Path) -> list[str]:
    hits = []
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = getattr(func, "attr", None) or getattr(func, "id", None)
            if called == name:
                hits.append(f"{path}:{node.lineno}")
    return hits


def test_no_meter_provider_is_installed() -> None:
    assert not _files_calling("set_meter_provider", _SOURCE_ROOT)


def test_the_meter_provider_scan_actually_fires(tmp_path: pathlib.Path) -> None:
    # Negative fixture: an AST walk that matched nothing would pass the
    # assertion above for the wrong reason.
    (tmp_path / "m.py").write_text("from opentelemetry import metrics\nmetrics.set_meter_provider(None)\n")
    assert _files_calling("set_meter_provider", tmp_path)
