"""A resource declaration is only worth having if bad ones fail loudly.

Two halves here, and the second is the one that earns its keep:

The **parser** must reject a manifest that looks authoritative and is not — a
member claimed by two groups, a node both grouped and exclusive, a path that no
longer exists. Each of those produces a schedule that reads as declared while
classifying something wrong, which is worse than no manifest at all.

The **scanner** must find real host resources without crying wolf. That cuts
both ways and both directions are tested: a fixed port has to be found, and a
domain method that happens to be spelled ``bind`` has to be left alone. A guard
with false positives gets switched off, at which point it protects nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.integration_resources import (
    Manifest,
    ManifestError,
    Outcome,
    ResourceError,
    assert_tree_declared,
    classify_all,
    guard,
    scan_module,
    scan_tree,
)

MINIMAL = """
[meta]
version = 1

[[groups]]
name = "embedding"
reason = "shared width templates"
members = ["tests/integration/test_embedding_dim_rebuild.py"]

[[external_exclusive]]
marker = "compose"
capability = "COMPOSE_STACK_UP"
reason = "shared live stack"
members = ["tests/integration/test_auth_compose_smoke.py"]
"""


def _tree(tmp_path: Path, *members: str) -> Path:
    """A fake repo root holding just the files a manifest names."""
    for member in members:
        target = tmp_path / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return tmp_path


def _write_manifest(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tests" / "integration_resources.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _load(tmp_path: Path, body: str, *members: str) -> Manifest:
    _tree(tmp_path, *members)
    return Manifest.load(_write_manifest(tmp_path, body), root=tmp_path)


DEFAULT_MEMBERS = (
    "tests/integration/test_embedding_dim_rebuild.py",
    "tests/integration/test_auth_compose_smoke.py",
)


# -- parsing and validation ----------------------------------------------


def test_a_minimal_manifest_loads(tmp_path: Path) -> None:
    manifest = _load(tmp_path, MINIMAL, *DEFAULT_MEMBERS)
    assert [group.name for group in manifest.groups] == ["embedding"]
    assert [external.marker for external in manifest.external_exclusive] == ["compose"]


def test_a_missing_manifest_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="does not exist"):
        Manifest.load(tmp_path / "nope.toml", root=tmp_path)


def test_invalid_toml_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="not valid TOML"):
        Manifest.load(_write_manifest(tmp_path, "[[groups]\nname ="), root=tmp_path)


def test_a_duplicate_group_name_is_rejected(tmp_path: Path) -> None:
    body = (
        MINIMAL
        + """
[[groups]]
name = "embedding"
reason = "again"
members = ["tests/integration/test_sync_ingest.py"]
"""
    )
    with pytest.raises(ManifestError, match="declared more than once"):
        _load(tmp_path, body, *DEFAULT_MEMBERS, "tests/integration/test_sync_ingest.py")


def test_a_member_in_two_groups_is_rejected(tmp_path: Path) -> None:
    """Two groups claiming one node is ambiguous, not redundant."""
    body = (
        MINIMAL
        + """
[[groups]]
name = "other"
reason = "overlaps"
members = ["tests/integration/test_embedding_dim_rebuild.py"]
"""
    )
    with pytest.raises(ManifestError, match="declared by both"):
        _load(tmp_path, body, *DEFAULT_MEMBERS)


def test_a_member_both_grouped_and_exclusive_is_rejected(tmp_path: Path) -> None:
    body = (
        MINIMAL
        + """
[[external_exclusive]]
marker = "other"
capability = "SOMETHING"
reason = "conflicts with the group"
members = ["tests/integration/test_embedding_dim_rebuild.py"]
"""
    )
    with pytest.raises(ManifestError, match="declared by both"):
        _load(tmp_path, body, *DEFAULT_MEMBERS)


def test_a_stale_member_path_is_rejected(tmp_path: Path) -> None:
    """The failure mode of a manifest that outlived a rename.

    It still parses and still looks authoritative, while the renamed node
    quietly classifies as ordinary.
    """
    with pytest.raises(ManifestError, match="stale declaration"):
        _load(tmp_path, MINIMAL, "tests/integration/test_auth_compose_smoke.py")


def test_an_empty_group_is_rejected(tmp_path: Path) -> None:
    body = """
[[groups]]
name = "empty"
reason = "nothing"
members = []
"""
    with pytest.raises(ManifestError, match="declares no members"):
        _load(tmp_path, body)


def test_an_ungated_external_exclusive_is_rejected(tmp_path: Path) -> None:
    """Exclusive without a capability cannot be skipped when its stack is absent."""
    body = """
[[external_exclusive]]
marker = "compose"
capability = ""
reason = "no gate"
members = ["tests/integration/test_auth_compose_smoke.py"]
"""
    with pytest.raises(ManifestError, match="declares no capability"):
        _load(tmp_path, body, "tests/integration/test_auth_compose_smoke.py")


def test_a_group_without_a_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="has no name"):
        _load(tmp_path, '[[groups]]\nreason = "x"\nmembers = ["a"]\n')


def test_an_unknown_resource_kind_is_rejected(tmp_path: Path) -> None:
    body = """
[[host_resources]]
kind = "gpu"
value = "0"
reason = "not a class this contract knows"
"""
    with pytest.raises(ManifestError, match="is not one of"):
        _load(tmp_path, body)


def test_a_duplicate_host_resource_is_rejected(tmp_path: Path) -> None:
    body = """
[[host_resources]]
kind = "fixed_port"
value = 5545
reason = "first"

[[host_resources]]
kind = "fixed_port"
value = 5545
reason = "second"
"""
    with pytest.raises(ManifestError, match="declared more than once"):
        _load(tmp_path, body)


def test_a_host_resource_without_a_reason_is_rejected(tmp_path: Path) -> None:
    """An undocumented fixed resource cannot be told apart from an oversight."""
    body = """
[[host_resources]]
kind = "fixed_port"
value = 5545
reason = ""
"""
    with pytest.raises(ManifestError, match="declares no reason"):
        _load(tmp_path, body)


def test_a_non_table_entry_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="must be a table"):
        _load(tmp_path, 'groups = ["not-a-table"]\n')


def test_a_scalar_groups_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="must be an array of tables"):
        _load(tmp_path, "groups = 3\n")


# -- classification ------------------------------------------------------


def test_a_grouped_node_reports_its_group(tmp_path: Path) -> None:
    manifest = _load(tmp_path, MINIMAL, *DEFAULT_MEMBERS)
    result = manifest.classify("tests/integration/test_embedding_dim_rebuild.py::test_width")
    assert result.outcome is Outcome.CO_LOCATION_GROUP
    assert result.group == "embedding"


def test_an_exclusive_node_reports_its_capability(tmp_path: Path) -> None:
    manifest = _load(tmp_path, MINIMAL, *DEFAULT_MEMBERS)
    result = manifest.classify("tests/integration/test_auth_compose_smoke.py::test_login")
    assert result.outcome is Outcome.EXTERNAL_EXCLUSIVE
    assert result.capability == "COMPOSE_STACK_UP"


def test_an_undeclared_node_is_ordinary(tmp_path: Path) -> None:
    """Ordinary is the default, so the manifest is not a curated coverage list."""
    manifest = _load(tmp_path, MINIMAL, *DEFAULT_MEMBERS)
    assert manifest.classify("tests/integration/test_anything.py::test_x").outcome is Outcome.ORDINARY


def test_a_parametrized_node_classifies_by_module(tmp_path: Path) -> None:
    manifest = _load(tmp_path, MINIMAL, *DEFAULT_MEMBERS)
    node = "tests/integration/test_embedding_dim_rebuild.py::test_width[1536-hnsw]"
    assert manifest.group_of(node) == "embedding"


def test_group_of_is_none_for_an_exclusive_node(tmp_path: Path) -> None:
    manifest = _load(tmp_path, MINIMAL, *DEFAULT_MEMBERS)
    assert manifest.group_of("tests/integration/test_auth_compose_smoke.py::test_login") is None


def test_only_three_outcomes_exist() -> None:
    """No fourth serial class, by construction rather than by convention."""
    assert {outcome.value for outcome in Outcome} == {"ordinary", "co-location-group", "external-exclusive"}


def test_classify_all_preserves_order(tmp_path: Path) -> None:
    manifest = _load(tmp_path, MINIMAL, *DEFAULT_MEMBERS)
    nodes = [
        "tests/integration/test_anything.py::a",
        "tests/integration/test_embedding_dim_rebuild.py::b",
        "tests/integration/test_auth_compose_smoke.py::c",
    ]
    assert [c.outcome for c in classify_all(nodes, manifest)] == [
        Outcome.ORDINARY,
        Outcome.CO_LOCATION_GROUP,
        Outcome.EXTERNAL_EXCLUSIVE,
    ]


# -- scanning: what must be found ---------------------------------------


def _scan_source(tmp_path: Path, body: str) -> list[str]:
    module = tmp_path / "tests" / "integration" / "test_probe.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(body, encoding="utf-8")
    return [f"{f.kind}:{f.value}" for f in scan_module(module, root=tmp_path)]


def test_a_fixed_port_keyword_is_found(tmp_path: Path) -> None:
    assert "fixed_port:5545" in _scan_source(tmp_path, "start(port=5545)\n")


def test_a_socket_bind_tuple_is_found(tmp_path: Path) -> None:
    assert "fixed_port:8080" in _scan_source(tmp_path, 'server.bind(("127.0.0.1", 8080))\n')


def test_an_environ_port_default_is_found(tmp_path: Path) -> None:
    """The fixed value hides in the default, as a string."""
    body = 'import os\nPORT = int(os.environ.get("CONTEXTPLANE_TEST_PG_PORT", "5545"))\n'
    assert "fixed_port:5545" in _scan_source(tmp_path, body)


def test_a_port_constant_is_found(tmp_path: Path) -> None:
    assert "fixed_port:5432" in _scan_source(tmp_path, "DEFAULT_PORT = 5432\n")


def test_a_shared_server_path_is_found(tmp_path: Path) -> None:
    assert "shared_server_path:pgdata-test" in _scan_source(tmp_path, 'DATA = ROOT / "pgdata-test"\n')


# -- scanning: what must NOT be found -----------------------------------


def test_port_zero_is_not_a_finding(tmp_path: Path) -> None:
    """Binding 0 is the fix the guard exists to encourage, not a violation."""
    assert _scan_source(tmp_path, 'server.bind(("127.0.0.1", 0))\n') == []


def test_a_dynamic_port_keyword_is_not_a_finding(tmp_path: Path) -> None:
    assert _scan_source(tmp_path, "start(port=0)\n") == []


def test_a_computed_port_is_not_a_finding(tmp_path: Path) -> None:
    assert _scan_source(tmp_path, "start(port=free_port())\n") == []


def test_a_domain_method_named_bind_is_not_a_finding(tmp_path: Path) -> None:
    """The false positive that would get this guard switched off.

    Receipts are bound to references through a method spelled `bind`, dozens of
    times. A textual search for `.bind(` flags every one of them; matching the
    call's shape does not.
    """
    body = "await index.bind(ctx, receipt_id=r, reference_ids=[x], bound_at=NOW)\n"
    assert _scan_source(tmp_path, body) == []


def test_a_run_specific_path_is_not_a_finding(tmp_path: Path) -> None:
    """A run-scoped directory removes the collision instead of scheduling it."""
    assert _scan_source(tmp_path, 'DATA = base / "run-abc123" / "pgdata"\n') == []


def test_a_tempdir_path_is_not_a_finding(tmp_path: Path) -> None:
    body = 'DATA = Path(tempfile.gettempdir()) / "pgdata"\n'
    assert _scan_source(tmp_path, body) == []


def test_a_privileged_port_is_not_a_finding(tmp_path: Path) -> None:
    """A test cannot bind below 1024 anyway, so flagging it is noise."""
    assert _scan_source(tmp_path, "start(port=80)\n") == []


def test_a_boolean_is_not_read_as_a_port(tmp_path: Path) -> None:
    assert _scan_source(tmp_path, "start(port=True)\n") == []


# -- the guard -----------------------------------------------------------


def test_an_undeclared_port_is_a_violation(tmp_path: Path) -> None:
    _tree(tmp_path, *DEFAULT_MEMBERS)
    (tmp_path / "tests" / "integration" / "test_probe.py").write_text("start(port=9999)\n", encoding="utf-8")
    manifest = Manifest.load(_write_manifest(tmp_path, MINIMAL), root=tmp_path)
    violations = guard(manifest, root=tmp_path, roots=("tests",))
    assert [v.finding.value for v in violations] == ["9999"]
    assert "bind port 0" in violations[0].detail


def test_a_declared_port_is_not_a_violation(tmp_path: Path) -> None:
    body = (
        MINIMAL
        + """
[[host_resources]]
kind = "fixed_port"
value = 9999
reason = "declared on purpose"
"""
    )
    _tree(tmp_path, *DEFAULT_MEMBERS)
    (tmp_path / "tests" / "integration" / "test_probe.py").write_text("start(port=9999)\n", encoding="utf-8")
    manifest = Manifest.load(_write_manifest(tmp_path, body), root=tmp_path)
    assert guard(manifest, root=tmp_path, roots=("tests",)) == []


def test_an_undeclared_server_path_is_a_violation(tmp_path: Path) -> None:
    _tree(tmp_path, *DEFAULT_MEMBERS)
    (tmp_path / "tests" / "integration" / "test_probe.py").write_text('D = R / "pgdata-shared"\n', encoding="utf-8")
    manifest = Manifest.load(_write_manifest(tmp_path, MINIMAL), root=tmp_path)
    violations = guard(manifest, root=tmp_path, roots=("tests",))
    assert [v.finding.value for v in violations] == ["pgdata-shared"]
    assert "derive it per run" in violations[0].detail


def test_a_declared_path_matches_its_final_segment(tmp_path: Path) -> None:
    """The manifest spells the readable whole; the source holds only a segment."""
    body = (
        MINIMAL
        + """
[[host_resources]]
kind = "shared_server_path"
value = ".devstack/pgdata-test"
reason = "developer cluster"
"""
    )
    _tree(tmp_path, *DEFAULT_MEMBERS)
    (tmp_path / "tests" / "integration" / "test_probe.py").write_text('D = R / "pgdata-test"\n', encoding="utf-8")
    manifest = Manifest.load(_write_manifest(tmp_path, body), root=tmp_path)
    assert guard(manifest, root=tmp_path, roots=("tests",)) == []


def test_a_declared_leaf_does_not_cover_a_different_directory(tmp_path: Path) -> None:
    """Declaring one data directory must not silently cover the next one added."""
    body = (
        MINIMAL
        + """
[[host_resources]]
kind = "shared_server_path"
value = "pgdata-test"
reason = "developer cluster"
"""
    )
    _tree(tmp_path, *DEFAULT_MEMBERS)
    (tmp_path / "tests" / "integration" / "test_probe.py").write_text('D = R / "pgdata-other"\n', encoding="utf-8")
    manifest = Manifest.load(_write_manifest(tmp_path, body), root=tmp_path)
    assert [v.finding.value for v in guard(manifest, root=tmp_path, roots=("tests",))] == ["pgdata-other"]


def test_the_guard_reports_every_violation_at_once(tmp_path: Path) -> None:
    """Stopping at the first would take one commit per finding to get clean."""
    _tree(tmp_path, *DEFAULT_MEMBERS)
    (tmp_path / "tests" / "integration" / "test_probe.py").write_text(
        'start(port=9998)\nD = R / "pgdata-x"\n', encoding="utf-8"
    )
    manifest = Manifest.load(_write_manifest(tmp_path, MINIMAL), root=tmp_path)
    assert len(guard(manifest, root=tmp_path, roots=("tests",))) == 2


def test_assert_tree_declared_names_every_violation(tmp_path: Path) -> None:
    _tree(tmp_path, *DEFAULT_MEMBERS)
    (tmp_path / "tests" / "integration" / "test_probe.py").write_text("start(port=9997)\n", encoding="utf-8")
    manifest = Manifest.load(_write_manifest(tmp_path, MINIMAL), root=tmp_path)
    with pytest.raises(ResourceError, match="1 undeclared host resource"):
        assert_tree_declared(manifest, root=tmp_path)


def test_the_scanner_excludes_its_own_vocabulary_and_fixtures() -> None:
    """The guard cannot scan its own definitions or its own test fixtures.

    This module defines what a fixed port is; the test module has to contain
    sample violations to prove the guard catches them. Scanning either reports
    that vocabulary as findings — a permanent failure whose only fix would be to
    stop testing the guard. Neither file binds a socket, which is what makes the
    exclusion structural rather than a convenience.
    """
    locations = {finding.location for finding in scan_tree()}
    assert "tests/helpers/integration_resources.py" not in locations
    assert "tests/unit/test_integration_resources.py" not in locations


def test_the_exclusion_is_narrow() -> None:
    """Only those two files are exempt; the rest of the tree is still scanned."""
    from tests.helpers.integration_resources import _SELF_EXCLUDED

    assert len(_SELF_EXCLUDED) == 2
    assert all(name.startswith("tests/") for name in _SELF_EXCLUDED)
