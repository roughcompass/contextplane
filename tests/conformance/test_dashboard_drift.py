"""Every series a shipped dashboard queries must be a series the app emits.

This gate exists because the failure it catches is silent in both directions. A
panel querying a metric nothing emits renders as an empty graph, and an empty
graph is indistinguishable from a healthy quiet service — so a dashboard can
ship broken and stay broken for as long as nobody has an incident. That is not
hypothetical here: these dashboards shipped querying four metric families no
code emitted, and six panels had been blank since the day they landed.

Runs entirely offline. No Grafana, no Prometheus, no network — the dashboard
JSON is parsed directly and compared against the exposition of a built app, so
this works in CI and under the air-gapped build.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re

import pytest
from prometheus_client import REGISTRY

from registry.config import Settings

_DASHBOARD_DIR = pathlib.Path(__file__).resolve().parents[2] / "packaging/helm/grafana-dashboards"

# A bare PromQL identifier. Whether it is a metric or a function name is decided
# by looking at the character that follows the match, not by a lookahead inside
# it: `\s*(?!\()` backtracks the identifier by one character to satisfy itself,
# so `histogram_quantile(` matches as `histogram_quantil`.
_METRIC_TOKEN = re.compile(r"(?<![\w:])[a-zA-Z_][a-zA-Z0-9_:]*")

# PromQL keywords, functions, and aggregation modifiers that look like metric
# names to a regex but are not.
_NOT_METRICS = frozenset(
    {
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "offset",
        "bool",
        "and",
        "or",
        "unless",
        "le",
        "type",
        "status",
        "tool",
        "route",
        "method",
        "rate",
        "irate",
        "increase",
        "sum",
        "avg",
        "min",
        "max",
        "count",
        "histogram_quantile",
        "topk",
        "bottomk",
        "quantile",
        "stddev",
        "stdvar",
        "count_values",
        "absent",
        "delta",
        "idelta",
        "deriv",
        "predict_linear",
        "label_values",
        "label_replace",
        "label_join",
        "time",
        "vector",
        "scalar",
        "clamp_max",
        "clamp_min",
        "round",
        "abs",
        "ceil",
        "floor",
        "exp",
        "ln",
        "log2",
        "log10",
        "sqrt",
        "changes",
        "resets",
        "avg_over_time",
        "sum_over_time",
        "max_over_time",
        "min_over_time",
        "count_over_time",
        "quantile_over_time",
        "stddev_over_time",
        "last_over_time",
        "e",
        "inf",
        "nan",
    }
)

# Suffixes prometheus_client appends to a declared family.
_DERIVED_SUFFIXES = ("_bucket", "_sum", "_count", "_total", "_created", "_info")


def _dashboards() -> list[pathlib.Path]:
    return sorted(_DASHBOARD_DIR.glob("*.json"))


def _exprs(doc: dict) -> list[str]:
    """Every PromQL string in a dashboard, panels and template variables alike."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("expr", "query", "definition") and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return found


def metric_names_in(expr: str) -> set[str]:
    """Metric family names referenced by a PromQL expression.

    Label matchers are stripped first so a value like `type="mcp"` cannot be
    mistaken for a series name.
    """
    stripped = re.sub(r"\{[^}]*\}", "", expr)
    stripped = re.sub(r"\[[^\]]*\]", "", stripped)
    stripped = re.sub(r"\"[^\"]*\"", "", stripped)
    names = set()
    for match in _METRIC_TOKEN.finditer(stripped):
        token = match.group(0)
        if token in _NOT_METRICS:
            continue
        # A `(` immediately after the identifier makes it a function call.
        rest = stripped[match.end() :].lstrip()
        if rest.startswith("("):
            continue
        names.add(token)
    return names


def _exposed_families() -> set[str]:
    """Every name the app's exposition can produce, derived suffixes included."""
    names: set[str] = set()
    for metric in REGISTRY.collect():
        names.add(metric.name)
        for suffix in _DERIVED_SUFFIXES:
            names.add(f"{metric.name}{suffix}")
        for sample in metric.samples:
            names.add(sample.name)
    return names


@pytest.fixture(scope="module", autouse=True)
def _built_app():
    # Importing and building the app is what populates the default registry with
    # every metric the process can emit. Without it this gate would compare the
    # dashboards against whatever happened to be imported first.
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


def test_there_are_dashboards_to_check() -> None:
    # Guards the whole file: a glob that matches nothing makes every assertion
    # below vacuously true.
    assert _dashboards()


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_every_series_a_dashboard_queries_is_actually_emitted(path: pathlib.Path) -> None:
    exposed = _exposed_families()
    missing: dict[str, str] = {}
    for expr in _exprs(json.loads(path.read_text())):
        for name in metric_names_in(expr):
            if name not in exposed:
                missing[name] = expr
    assert not missing, f"{path.name} queries series nothing emits, so these panels render blank: " + ", ".join(
        f"{n} (in {e!r})" for n, e in sorted(missing.items())
    )


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_no_dashboard_references_a_tenant_dimension(path: pathlib.Path) -> None:
    """The one grouping this phase exists to prevent.

    A panel grouping by tenant is a standing request to add the label, and
    whoever picks it up later will read a blank panel as a bug to fix rather
    than as a boundary that was drawn on purpose.
    """
    # Checked against the queries rather than the whole file: the panel prose
    # explains *why* there is no per-tenant grouping, and that explanation is
    # the thing most likely to stop someone reinstating it.
    offenders = [e for e in _exprs(json.loads(path.read_text())) if "tenant" in e.lower()]
    assert not offenders, f"{path.name} still queries a tenant dimension: {offenders}"

    doc = json.loads(path.read_text())
    variables = [v.get("name") for v in doc.get("templating", {}).get("list", [])]
    assert not [v for v in variables if v and "tenant" in v.lower()]


def test_the_gate_fires_on_a_series_that_does_not_exist() -> None:
    # The negative fixture. Without it, a name-extraction bug that returned the
    # empty set would make every assertion above pass.
    names = metric_names_in("sum by (le) (rate(totally_made_up_metric_bucket[5m]))")
    assert "totally_made_up_metric_bucket" in names
    assert names - _exposed_families()


def test_label_matcher_values_are_not_mistaken_for_series() -> None:
    # `type="mcp"` must not read as a metric called `mcp`, or the gate would
    # fail on correct dashboards and get deleted.
    names = metric_names_in(
        'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{type="mcp"}[5m]))) * 1000'
    )
    assert names == {"http_request_duration_seconds_bucket"}
