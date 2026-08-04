"""Route resolution and traffic-type derivation.

Two properties carry the weight here, and both are provable without an ASGI
harness — which is why these are pure functions rather than middleware internals.

    1. A label value can never be a resolved path. Entity ids in a label are how
       a metric's series count becomes a function of row count.
    2. A health probe can never be counted as API traffic. The dashboard
       aggregates latency across every route sharing a type, so probe traffic in
       `read` would drag the reported percentile toward zero — a populated,
       wrong panel, which is worse than the blank one it replaced.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount, Route

from registry import metrics
from registry.api.middleware.metrics import derive_type, resolve_route, status_class


class _FakeAPIRoute:
    """Stands in for `fastapi.routing.APIRoute`, which sets `scope['route']`."""

    def __init__(self, path_format: str) -> None:
        self.path_format = path_format


def _scope(path: str, method: str = "GET", **extra: object) -> dict:
    return {"type": "http", "path": path, "method": method, "path_params": {}, "root_path": "", "headers": [], **extra}


class _App:
    def __init__(self, routes: list) -> None:
        self.routes = routes


def test_a_populated_scope_route_is_used_directly() -> None:
    scope = _scope("/v1/capabilities/3f9a-live-uuid")
    scope["route"] = _FakeAPIRoute("/v1/capabilities/{entity_id}")
    # The template, not the path the client actually requested.
    assert resolve_route(scope, _App([])) == "/v1/capabilities/{entity_id}"


def test_the_resolved_path_is_never_the_label() -> None:
    scope = _scope("/v1/capabilities/3f9a-live-uuid")
    scope["route"] = _FakeAPIRoute("/v1/capabilities/{entity_id}")
    assert "3f9a-live-uuid" not in resolve_route(scope, _App([]))


def test_a_plain_route_falls_back_to_its_static_path() -> None:
    # FastAPI's own docs endpoints are plain Routes and set no scope["route"].
    app = _App([Route("/openapi.json", endpoint=lambda request: None)])
    assert resolve_route(_scope("/openapi.json"), app) == "/openapi.json"


def test_a_mount_resolves_to_its_wildcard_template_not_its_prefix() -> None:
    """The value that broke an earlier draft of the streaming exclusion.

    `Mount.__init__` appends `/{path:path}` when building `path_format`, so a
    mount at `/mcp` resolves to `/mcp/{path}`. Anything keyed on `== "/mcp"`
    never fires. Pinned as an exact string so a Starlette change that alters it
    fails loudly here rather than silently disabling the exclusion.
    """
    app = _App([Mount("/mcp", app=Starlette())])
    assert resolve_route(_scope("/mcp/sse"), app) == "/mcp/{path}"


def test_an_unmatched_request_becomes_a_constant() -> None:
    # A 404 flood must not mint one series per invented URL.
    assert resolve_route(_scope("/nope/" + "x" * 50), _App([])) == metrics.UNKNOWN_ROUTE


@pytest.mark.parametrize("prefix", ["/healthz", "/readyz", "/metrics", "/webhooks"])
def test_every_bypass_prefix_is_other_not_read_or_write(prefix: str) -> None:
    # Checked for both verbs: /webhooks is a POST, so a method-first rule would
    # have classified it `write` and put inbound webhook traffic in the panel
    # that reports API write latency.
    assert derive_type(_scope(prefix, "GET")) == "other"
    assert derive_type(_scope(prefix, "POST")) == "other"


def test_api_traffic_splits_by_verb() -> None:
    assert derive_type(_scope("/v1/capabilities", "GET")) == "read"
    assert derive_type(_scope("/v1/capabilities", "POST")) == "write"
    assert derive_type(_scope("/v1/capabilities", "DELETE")) == "write"


def test_mcp_is_its_own_bucket() -> None:
    assert derive_type(_scope("/mcp/sse")) == "mcp"
    assert derive_type(_scope("/mcp/messages/", "POST")) == "mcp"


def test_a_path_merely_starting_with_mcp_is_not_the_mount() -> None:
    # `/mcpx` is a different route, and a naive startswith("/mcp") would swallow it.
    assert derive_type(_scope("/mcpx/thing", "GET")) == "read"


@pytest.mark.parametrize(
    ("code", "expected"),
    [(200, "2xx"), (301, "3xx"), (401, "4xx"), (429, "4xx"), (500, "5xx"), (599, "5xx")],
)
def test_status_reduces_to_its_class(code: int, expected: str) -> None:
    assert status_class(code) == expected


def test_an_absent_status_is_other_not_an_error_class() -> None:
    # A request that never sent a response start has no status. Calling that 5xx
    # would invent an error rate out of disconnects.
    assert status_class(None) == "other"


def test_every_derived_value_is_in_the_declared_vocabulary() -> None:
    # The link between these functions and the metric module's runtime guard: if
    # a derivation ever returned something outside the set, observe_request would
    # raise in production. This catches it here instead.
    paths = ["/healthz", "/v1/x", "/mcp/sse", "/mcpx", "/webhooks/github"]
    for path in paths:
        for method in ("GET", "POST", "TRACE"):
            assert derive_type(_scope(path, method)) in metrics.REQUEST_TYPES
    for code in (100, 200, 302, 404, 503, 999):
        assert status_class(code) in metrics.STATUS_CLASSES
