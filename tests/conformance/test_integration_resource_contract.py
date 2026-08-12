"""Pin the host-resource classes the integration tier actually has.

The unit tests prove the parser and the scanner behave. This file pins the
*answer* for the shipped tree, so a change to what contends for a host resource
has to be a deliberate edit here rather than a silent drift.

Two properties are worth more than the rest:

**No global serial class exists.** A suite-wide serial shard would preserve
exactly the serial fraction that makes this tier slow, so its absence is the
whole point of the classification — and something that must not exist needs a
test, because nothing else will notice it appearing. Process-global state is
what makes the serial class look necessary and is precisely what does not need
it: isolated workers isolate a Prometheus registry, a reloaded router, a
scheduler object, a module global, and a mutated environment for free.

**The integrated tree declares every host resource it uses.** A declaration
nobody checks is a comment. The guard walks the tree and this pins that it comes
back empty, so an undeclared fixed port added anywhere fails here rather than by
two workers colliding on a port in a measured run.
"""

from __future__ import annotations

import pytest

from tests.helpers.integration_resources import (
    Manifest,
    Outcome,
    assert_tree_declared,
    guard,
    scan_tree,
)

# Modules whose state is process-global, not host-global. Isolated workers give
# each of these its own process, so they are ordinary — grouping or serializing
# them would buy nothing and cost the parallel gain.
PROCESS_LOCAL_MODULES = (
    "tests/integration/test_http_methods_mode.py",
    "tests/integration/test_admin_progression.py",
    "tests/integration/test_closure_cache.py",
    "tests/integration/test_sync_ingest.py",
)

# Each of these builds its own head clone, downgrades, asserts, and re-upgrades.
# They share nothing with each other, so grouping them would serialize nodes
# that never collide.
INDEPENDENT_MIGRATION_MODULES = (
    "tests/integration/test_context_reference_migrations.py",
    "tests/integration/test_context_receipt_migrations.py",
    "tests/integration/test_intent_memory_migrations.py",
    "tests/integration/test_feedback_learning_migrations.py",
    "tests/integration/test_partition_migration.py",
)


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    return Manifest.load()


# -- the pinned classes ---------------------------------------------------


def test_exactly_one_co_location_group_is_declared(manifest: Manifest) -> None:
    """The embedding widths are the only shared-setup group in the tier."""
    assert [group.name for group in manifest.groups] == ["embedding"]


def test_the_embedding_group_holds_the_width_scenarios(manifest: Manifest) -> None:
    group = next(g for g in manifest.groups if g.name == "embedding")
    assert group.members == ("tests/integration/test_embedding_dim_rebuild.py",)
    assert group.reason, "a co-location group without a reason cannot be reviewed"


def test_the_compose_marker_is_the_only_external_exclusive(manifest: Manifest) -> None:
    assert [external.marker for external in manifest.external_exclusive] == ["compose"]


def test_the_compose_smoke_is_capability_gated(manifest: Manifest) -> None:
    """Exclusive and gated: it drives a stack the suite cannot start."""
    external = next(e for e in manifest.external_exclusive if e.marker == "compose")
    assert external.capability == "COMPOSE_STACK_UP"
    assert external.members == ("tests/integration/test_auth_compose_smoke.py",)


def test_the_compose_smoke_classifies_as_external_exclusive(manifest: Manifest) -> None:
    result = manifest.classify("tests/integration/test_auth_compose_smoke.py::test_anything")
    assert result.outcome is Outcome.EXTERNAL_EXCLUSIVE
    assert result.capability == "COMPOSE_STACK_UP"


# -- no global serial class ----------------------------------------------


def test_no_fourth_class_exists() -> None:
    """Something that must not exist needs a test; nothing else notices it."""
    assert {outcome.value for outcome in Outcome} == {
        "ordinary",
        "co-location-group",
        "external-exclusive",
    }


def test_no_declaration_names_a_serial_class(manifest: Manifest) -> None:
    """A group called `serial` would be a global serial shard by another name."""
    names = {group.name.lower() for group in manifest.groups}
    markers = {external.marker.lower() for external in manifest.external_exclusive}
    for label in ("serial", "global", "exclusive_all", "suite"):
        assert label not in names, f"co-location group {label!r} would be a global serial class"
        assert label not in markers, f"external-exclusive marker {label!r} would be a global serial class"


def test_no_group_swallows_the_tier(manifest: Manifest) -> None:
    """A group large enough to hold most of the tier is a serial shard.

    Two members is generous for a shared-setup group; the guard here is against
    a group that grows until it is the whole suite on one worker.
    """
    for group in manifest.groups:
        assert len(group.members) <= 4, f"co-location group {group.name!r} holds {len(group.members)} modules"


@pytest.mark.parametrize("module", PROCESS_LOCAL_MODULES)
def test_process_local_state_stays_ordinary(manifest: Manifest, module: str) -> None:
    """Prometheus registries, routers, schedulers, app and environment state.

    All process-global, so an isolated worker isolates them without help.
    """
    assert manifest.classify(f"{module}::test_anything").outcome is Outcome.ORDINARY


@pytest.mark.parametrize("module", INDEPENDENT_MIGRATION_MODULES)
def test_independent_migration_nodes_are_not_grouped(manifest: Manifest, module: str) -> None:
    assert manifest.group_of(f"{module}::test_anything") is None


def test_the_pinned_process_local_modules_still_exist(manifest: Manifest) -> None:
    """A pin on a renamed module passes while asserting nothing.

    Without this, the parametrized tests above keep passing after somebody
    renames a module, because an absent module classifies as ordinary — which is
    exactly what they assert.
    """
    from tests.helpers.integration_resources import _REPO_ROOT

    for module in PROCESS_LOCAL_MODULES + INDEPENDENT_MIGRATION_MODULES:
        assert (_REPO_ROOT / module).exists(), f"{module} no longer exists; this pin asserts nothing"


# -- the integrated-tree guard -------------------------------------------


def test_the_tree_declares_every_host_resource_it_uses() -> None:
    """An undeclared fixed port fails here, not by colliding in a measured run."""
    assert_tree_declared()


def test_the_guard_finds_the_resources_that_do_exist() -> None:
    """Proves the guard is reading the tree rather than trivially passing.

    A guard that found nothing at all would also report zero violations, so the
    clean result above only means something if the scan has real findings.
    """
    findings = scan_tree()
    assert findings, "the scanner found no host resources at all; it is not reading the tree"
    kinds = {finding.kind for finding in findings}
    assert kinds <= {"fixed_port", "shared_server_path"}


def test_every_finding_is_declared(manifest: Manifest) -> None:
    assert guard(manifest) == []


def test_the_declared_resources_are_the_developer_cluster(manifest: Manifest) -> None:
    """Both declarations belong to the single-process developer path.

    Pinned because that path is unreachable under the runner — a worker takes
    its URL from the broker manifest — so a new declaration appearing here means
    a *measured* run gained a fixed resource, which is the thing worth noticing.
    """
    assert manifest.declared_ports == frozenset({"5545"})
    assert manifest.declared_server_paths == frozenset({".devstack/pgdata-test"})
    for resource in manifest.resources:
        assert "pg_provider.py" in resource.location
        assert resource.reason


def test_the_embedding_module_no_longer_binds_a_fixed_port() -> None:
    """The precondition for this whole guard.

    The width scenarios used to start a cluster on a fixed port, four times
    over. They now take a run-scoped cluster on a dynamic port, which is why a
    tree-wide fixed-port guard can exist at all — before that, it would have had
    to reject an unavoidable module.
    """
    embedding_findings = [finding for finding in scan_tree() if "test_embedding_dim_rebuild" in finding.location]
    assert embedding_findings == []


def test_the_manifest_evidence_is_serializable(manifest: Manifest) -> None:
    """Evidence is written to a run manifest, so it has to survive json.dumps."""
    import json

    restored = json.loads(json.dumps(manifest.as_evidence()))
    assert restored["groups"] == {"embedding": ["tests/integration/test_embedding_dim_rebuild.py"]}
    assert restored["declared_ports"] == ["5545"]
