"""The privileged-write gate is the enforcement, so it needs its own tests.

A structural gate that silently matches nothing is worse than no gate: the
invariant reads as enforced in the architecture docs and in review, while any
module is free to write the rows. Each test here breaks the rule one way and
asserts the gate notices.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_privileged_writes import (
    RULES,
    check_file,
    main,
    resolve_targets,
)


def _write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "candidate.py"
    target.write_text(body, encoding="utf-8")
    return target


@pytest.fixture
def repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the gate at a scratch tree so tests never depend on real sources."""
    monkeypatch.setattr("scripts.check_privileged_writes._REPO_ROOT", tmp_path)
    return tmp_path


def test_the_real_tree_passes() -> None:
    """The gate's own subject. Fails the moment a second writer lands.

    The scope is passed explicitly, resolved from this file, rather than left to the
    script's default — which only resolves when the checkout is literally named
    `registry`, so in a git worktree this used to scan nothing and pass, in exactly
    the checkouts used to isolate risky work.

    The other branch reached for a visible skip here instead, on the grounds that
    re-rooting the scan would stop the permitted-writer paths matching and turn every
    legitimate writer into a violation. That was true of the code it was written
    against. Those paths are matched as suffixes now, so the constraint is gone and
    the test can assert something wherever it runs rather than declining to.
    """
    package = Path(__file__).resolve().parents[2] / "registry"

    assert main(["--paths", str(package)]) == 0


def test_every_governed_table_names_a_permitted_caller() -> None:
    """A rule with no permitted caller would forbid the write outright, which
    is a different decision and should be made deliberately, not by omission."""
    for rule in RULES:
        assert rule.allowed_callers, f"{rule.table} has no permitted writer"
        assert rule.guidance.strip(), f"{rule.table} has no guidance for a would-be caller"


@pytest.mark.parametrize("table", [r.table for r in RULES])
def test_an_insert_from_an_unlisted_module_is_flagged(repo_root: Path, table: str) -> None:
    target = _write(repo_root, f'SQL = "INSERT INTO {table} (x) VALUES (:x)"\n')
    found = check_file(target)
    assert [v.rule.table for v in found] == [table]
    assert found[0].line_no == 1


@pytest.mark.parametrize("table", [r.table for r in RULES])
def test_an_update_is_flagged_not_only_an_insert(repo_root: Path, table: str) -> None:
    """A module that can rewrite a row bypasses the same invariants as one that
    creates it — flipping a claim from unlinked to staged without re-resolving
    its subject is exactly the write the gate exists to prevent."""
    target = _write(repo_root, f'SQL = "UPDATE {table} SET x = :x"\n')
    assert [v.rule.table for v in check_file(target)] == [table]


@pytest.mark.parametrize("table", [r.table for r in RULES])
def test_a_delete_is_flagged(repo_root: Path, table: str) -> None:
    target = _write(repo_root, f'SQL = "DELETE FROM {table} WHERE x = :x"\n')
    assert [v.rule.table for v in check_file(target)] == [table]


def test_extra_whitespace_does_not_evade_the_gate(repo_root: Path) -> None:
    target = _write(repo_root, 'SQL = "insert   into    memory_claims (x) VALUES (:x)"\n')
    assert len(check_file(target)) == 1


def test_a_read_is_not_a_write(repo_root: Path) -> None:
    """Every reader of these tables would otherwise have to be allowlisted,
    and an allowlist that large stops being a control."""
    target = _write(repo_root, 'SQL = "SELECT claim_id FROM memory_claims WHERE status = :s"\n')
    assert check_file(target) == []


def test_a_similarly_named_table_is_not_matched(repo_root: Path) -> None:
    """`memory_claims_archive` is not `memory_claims`. A prefix match would flag
    tables the rule says nothing about, and the noise is how a gate gets
    disabled."""
    target = _write(repo_root, 'SQL = "INSERT INTO memory_claims_archive (x) VALUES (:x)"\n')
    assert check_file(target) == []


def test_the_permitted_caller_is_exempt_only_for_its_own_table(repo_root: Path) -> None:
    """`claim_writer.py` may write claims; it may not create tenants. An exemption
    that covered every governed table would make one allowlist entry a
    blanket privilege."""
    path = repo_root / "registry" / "service" / "memory"
    path.mkdir(parents=True)
    target = path / "claim_writer.py"
    target.write_text(
        'A = "INSERT INTO memory_claims (x) VALUES (:x)"\n' 'B = "INSERT INTO tenants (tenant_id) VALUES (:x)"\n',
        encoding="utf-8",
    )

    found = check_file(target)
    assert [v.rule.table for v in found] == ["tenants"]


def test_migrations_are_out_of_scope(repo_root: Path) -> None:
    """Migrations legitimately seed rows during bootstrapping, and the
    migration runner decides when they run."""
    path = repo_root / "registry" / "storage" / "migrations" / "versions"
    path.mkdir(parents=True)
    (path / "0099_x.py").write_text('SQL = "INSERT INTO memory_claims (x) VALUES (:x)"\n')

    assert resolve_targets(["registry"]) == []


def test_an_out_of_scope_path_fails_rather_than_passing_silently(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scope that does not exist is a failure, not a clean run.

    This used to exit 0 and say so on stderr, which is not the same thing: CI
    reads the exit code. So a mistyped --paths passed, and so did the default
    scope from any checkout shaped differently from the one the script resolves
    it against — a git worktree, for instance, which is named for its branch.
    This gate is the only thing between a new caller and a privileged table, and
    it was unenforced in exactly the checkouts used to isolate risky work.
    """
    assert main(["--paths", "does/not/exist"]) == 1
    assert "scope does not exist" in capsys.readouterr().err


def test_a_scope_that_exists_but_holds_nothing_still_passes(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The distinction that keeps the rule above honest.

    "I looked and there was nothing to flag" is a real pass. Only "I could not
    find what you asked me to look at" is the failure.
    """
    (repo_root / "empty").mkdir()

    assert main(["--paths", "empty"]) == 0
    assert "no files to scan" in capsys.readouterr().err


def test_a_default_scope_that_resolves_to_nothing_fails(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Unlike a caller's typo, an unresolvable default means the gate cannot run.

    The distinction is who chose the scope. A bad `--paths` is the caller's mistake
    and is theirs to read in the log; a default scope that resolves to nothing means
    every governed table went unchecked while the gate printed success — which is
    what this module's docstring calls worse than having no gate at all.
    """
    assert main([]) == 1
    err = capsys.readouterr().err
    assert "resolved to no files" in err
    assert "--paths" in err, "the error must say how to recover, not just that it failed"


def test_a_violation_exits_non_zero_and_names_the_file(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = repo_root / "registry" / "service"
    path.mkdir(parents=True)
    (path / "rogue.py").write_text('SQL = "INSERT INTO memory_claims (x) VALUES (:x)"\n')

    assert main(["--paths", "registry"]) == 1
    out = capsys.readouterr()
    assert "registry/service/rogue.py:1" in out.out
    assert "ClaimService" in out.err
