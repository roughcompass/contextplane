"""Metric names are unique, prefixed, and constructed once per process.

Metrics are deliberately defined beside their emitters rather than in one
module, so no single file can eyeball the namespace. This gate is what makes
the distributed shape safe: it imports the application (which constructs
every import-time metric into the default registry) and asserts the naming
discipline over the real, assembled surface — a duplicate name or a stray
unprefixed metric fails here instead of as a "Duplicate timeseries" crash on
the second construction, or as an operator's dashboard query that finds
nothing.
"""

from __future__ import annotations

from prometheus_client import REGISTRY

# Families Python's own client publishes for the process; not ours to police.
_RUNTIME_PREFIXES = ("python_", "process_")

# Prefixes this codebase established before the registry_ convention settled.
# Closed set: additions require editing this file, which is the point.
_LEGACY_PREFIXES = (
    "catalog_",
    "auth_",
    "arc_",
    "embedding_",
    "sync_",
    # The request/tool families predate the convention and are what the
    # shipped Grafana dashboards query by name; renaming them is a deliberate
    # dashboard-and-code change, not a lint fix.
    "http_",
    "mcp_",
)


def _application_families() -> list[str]:
    import contextplane.main  # noqa: F401  (importing constructs every import-time metric)

    return sorted(family.name for family in REGISTRY.collect() if not family.name.startswith(_RUNTIME_PREFIXES))


def test_every_metric_name_is_unique() -> None:
    names = _application_families()
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate metric families: {sorted(dupes)}"


def test_every_metric_carries_a_known_prefix() -> None:
    allowed = ("registry_", *_LEGACY_PREFIXES)
    strays = [n for n in _application_families() if not n.startswith(allowed)]
    assert not strays, (
        "metrics outside the naming convention (registry_ or a declared legacy "
        f"prefix): {strays}. New metrics take registry_; legacy prefixes are a "
        "closed set declared in this file."
    )
