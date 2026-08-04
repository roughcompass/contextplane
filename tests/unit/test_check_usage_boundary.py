"""The usage-boundary gate is the enforcement, so it needs its own tests.

A structural gate that matches nothing is worse than no gate: the rule reads as
enforced in the docs and in review while any module is free to break it. Each test
here breaks the boundary one way and asserts the gate notices, and the negative
fixtures assert it stays quiet on the things that merely look similar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_usage_boundary import (
    _DEFAULT_SCOPE,
    ALLOWED_IMPORTERS,
    ALLOWED_SQL_OUTSIDE_PACKAGE,
    check_file,
    main,
    resolve_targets,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the gate at a scratch tree so tests never depend on real sources."""
    monkeypatch.setattr("scripts.check_usage_boundary._REPO_ROOT", tmp_path)
    return tmp_path


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The gate's own subject
# ---------------------------------------------------------------------------


def test_the_real_tree_passes() -> None:
    """Fails the moment an undeclared module reaches for usage data."""
    targets = resolve_targets(list(_DEFAULT_SCOPE))
    assert targets, f"the default scope resolved to no files under {_REPO_ROOT} — nothing was checked"
    assert main([]) == 0


def test_every_declared_importer_states_a_reason() -> None:
    """A declaration without a reason is an allowlist entry with no argument behind it.

    The point of the inverted list is that adding an importer forces someone to say
    why. An empty reason field would turn it back into a list of names.
    """
    for importer in ALLOWED_IMPORTERS:
        assert importer.reason.strip(), f"{importer.path} is declared with no reason"
        assert len(importer.reason) > 40, f"{importer.path}'s reason is too short to be one"


def test_every_declared_importer_still_imports_usage() -> None:
    """A stale permission is one nobody is thinking about.

    It will still be there the day someone needs it for the wrong reason, and by
    then the declaration reads as prior approval.
    """
    for importer in ALLOWED_IMPORTERS:
        path = _REPO_ROOT / importer.path
        assert path.exists(), f"{importer.path} is declared but does not exist"
        assert "registry.usage" in path.read_text(
            encoding="utf-8"
        ), f"{importer.path} is declared as a usage importer but no longer imports it"


# ---------------------------------------------------------------------------
# Rule 1 — importing usage requires a declaration
# ---------------------------------------------------------------------------


def test_an_undeclared_service_importing_usage_is_flagged(repo_root: Path) -> None:
    target = _write(repo_root, "registry/service/deprecation.py", "from registry.usage import reads\n")
    found = check_file(target)
    assert [v.rule for v in found] == ["undeclared-usage-importer"]
    assert found[0].line_no == 1
    # The guidance has to point somewhere, or the gate is a wall with no door.
    assert "audit log" in found[0].guidance


def test_a_deferred_import_inside_a_function_is_flagged_too(repo_root: Path) -> None:
    """The obvious way around a line-based gate.

    A deferred import is still a dependency, and putting it inside a function is
    exactly what someone does when a top-level one gets rejected.
    """
    target = _write(
        repo_root,
        "registry/service/deprecation.py",
        "def decide() -> None:\n    import registry.usage.reads  # noqa: PLC0415\n",
    )
    assert [v.rule for v in check_file(target)] == ["undeclared-usage-importer"]


def test_a_declared_importer_is_not_flagged(repo_root: Path) -> None:
    declared = ALLOWED_IMPORTERS[0].path
    target = _write(repo_root, declared, "from registry.usage.writer import UsageWriter\n")
    assert check_file(target) == []


def test_the_usage_package_may_import_itself(repo_root: Path) -> None:
    target = _write(repo_root, "registry/usage/reads.py", "from registry.usage.vocabularies import SURFACES\n")
    assert check_file(target) == []


def test_an_unrelated_import_is_not_flagged(repo_root: Path) -> None:
    # Negative fixture: a gate that fired on every import would be turned off
    # within a day.
    target = _write(
        repo_root,
        "registry/service/deprecation.py",
        "from registry.service.catalog.core import CatalogService\nimport datetime\n",
    )
    assert check_file(target) == []


def test_a_similarly_named_module_is_not_matched(repo_root: Path) -> None:
    """`registry.usages` and `registry.usage_helpers` are not `registry.usage`.

    A substring match would flag modules the rule says nothing about, and that
    noise is how a gate gets disabled.
    """
    target = _write(repo_root, "registry/service/x.py", "from registry.usagelike import thing\n")
    assert check_file(target) == []


# ---------------------------------------------------------------------------
# Rule 2 — SQL against the usage tables belongs to the package
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        'SQL = "SELECT count(*) FROM usage_events WHERE tenant_id = :t"',
        'SQL = "INSERT INTO usage_events (event_id) VALUES (:e)"',
        'SQL = "UPDATE usage_rollup_tenant_day SET calls = 0"',
        'SQL = "SELECT * FROM usage_rollup_tool_day"',
        'SQL = "SELECT x FROM caps JOIN usage_events ON true"',
    ],
)
def test_sql_against_a_usage_table_outside_the_package_is_flagged(repo_root: Path, sql: str) -> None:
    target = _write(repo_root, "registry/service/deprecation.py", sql + "\n")
    assert [v.rule for v in check_file(target)] == ["usage-sql-outside-package"]


def test_sql_inside_the_package_is_allowed(repo_root: Path) -> None:
    target = _write(repo_root, "registry/usage/reads.py", 'SQL = "SELECT * FROM usage_events"\n')
    assert check_file(target) == []


def test_the_retention_worker_may_delete(repo_root: Path) -> None:
    """Retention is a worker by the same pattern as every other expiry sweep here.

    Moving its statement into the package would split the sweep from the batching
    and logging it shares with its siblings.
    """
    (allowed,) = ALLOWED_SQL_OUTSIDE_PACKAGE
    target = _write(repo_root, allowed, 'SQL = "DELETE FROM usage_events WHERE occurred_at < :cutoff"\n')
    assert check_file(target) == []


def test_migrations_may_create_the_tables(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/storage/migrations/versions/0099_x.py",
        'DDL = "CREATE TABLE usage_events (event_id UUID)"\n',
    )
    assert check_file(target) == []


def test_a_similarly_named_table_is_not_matched(repo_root: Path) -> None:
    # `usage_events_archive` is not `usage_events`, and a prefix match would flag
    # tables the rule says nothing about.
    target = _write(repo_root, "registry/service/x.py", 'SQL = "SELECT * FROM usage_events_archive"\n')
    assert check_file(target) == []


def test_the_word_usage_in_prose_is_not_a_query(repo_root: Path) -> None:
    # Negative fixture. The rule is about SQL, and a docstring discussing usage
    # events is not SQL.
    target = _write(
        repo_root,
        "registry/service/x.py",
        '"""Reads nothing from usage_events, and explains at length why not."""\n',
    )
    assert check_file(target) == []


# ---------------------------------------------------------------------------
# Rule 3 — usage may not reach the decision layers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["registry.service.catalog.core", "registry.arc.service.selection"])
def test_usage_importing_a_decision_layer_is_flagged(repo_root: Path, module: str) -> None:
    target = _write(repo_root, "registry/usage/recording.py", f"from {module} import Thing\n")
    assert [v.rule for v in check_file(target)] == ["usage-imports-decision-layer"]


def test_usage_may_import_shared_primitives(repo_root: Path) -> None:
    # Negative fixture: the rule is about the decision layers, not about isolation.
    # Recording needs the clock, the metric families, and the context type.
    target = _write(
        repo_root,
        "registry/usage/writer.py",
        "from registry.metrics import observe_queue_depth\nfrom registry.types import TenantContext\n",
    )
    assert check_file(target) == []


def test_a_module_outside_the_package_may_import_the_service_layer(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/api/routers/caps.py",
        "from registry.service.catalog.core import CatalogService\n",
    )
    assert check_file(target) == []


# ---------------------------------------------------------------------------
# Bypass, and the vacuity guard
# ---------------------------------------------------------------------------


def test_an_intentional_line_is_exempt(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/service/deprecation.py",
        "from registry.usage import reads  # usage-boundary: intentional\n",
    )
    assert check_file(target) == []


def test_the_bypass_is_per_line_not_per_file(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/service/deprecation.py",
        "from registry.usage import reads  # usage-boundary: intentional\n" "from registry.usage import writer\n",
    )
    found = check_file(target)
    assert len(found) == 1
    assert found[0].line_no == 2


def test_a_default_scope_that_resolves_to_nothing_fails(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A gate that scanned no files must not report success.

    Written in from the start here, because three sibling gates in this repo had to
    have it retrofitted after one of them let nine violations through.
    """
    assert main([]) == 1
    assert "resolved to no files" in capsys.readouterr().err


def test_an_explicit_path_that_matches_nothing_still_exits_zero(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A caller's typo is theirs to read in the log; it must not fail an otherwise
    # good run. Only an unresolvable default means the gate itself is broken.
    assert main(["--paths", "does/not/exist"]) == 0
    assert "no .py files in scope" in capsys.readouterr().err


def test_a_violation_exits_non_zero_and_names_the_file(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo_root, "registry/service/rogue.py", "from registry.usage import reads\n")
    # Every declared importer must exist under the scratch root, or the staleness
    # check fires instead of the rule under test.
    for importer in ALLOWED_IMPORTERS:
        _write(repo_root, importer.path, "from registry.usage import reads\n")

    assert main(["--paths", "registry/service"]) == 1
    out = capsys.readouterr()
    assert "registry/service/rogue.py:1" in out.out
    assert "non-authoritative" in out.err


def test_a_stale_declaration_is_reported(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A declared importer that no longer imports usage is a standing permission.

    Nobody revisits it, and it reads as prior approval the day someone wants the
    data for the wrong reason.
    """
    for importer in ALLOWED_IMPORTERS:
        _write(repo_root, importer.path, "x = 1\n")

    assert main(["--paths", "registry"]) == 1
    out = capsys.readouterr()
    assert "stale-declaration" in out.out
    assert ALLOWED_IMPORTERS[0].path in out.out


def test_explain_lists_every_rule(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--explain"]) == 0
    out = capsys.readouterr().out
    for rule in ("undeclared-usage-importer", "usage-sql-outside-package", "usage-imports-decision-layer"):
        assert rule in out
    for importer in ALLOWED_IMPORTERS:
        assert importer.path in out
