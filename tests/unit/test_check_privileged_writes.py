"""The privileged-write gate is the enforcement, so it needs its own tests.

A structural gate that silently matches nothing is worse than no gate: the
invariant reads as enforced in the architecture docs and in review, while any
module is free to write the rows. Each test here breaks the rule one way and
asserts the gate notices.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_privileged_writes import RULES, check_file, main, resolve_targets


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
    """The gate's own subject. Fails the moment a second writer lands."""
    assert main([]) == 0


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
    target = _write(repo_root, 'SQL = "insert   into    lmm_claims (x) VALUES (:x)"\n')
    assert len(check_file(target)) == 1


def test_a_read_is_not_a_write(repo_root: Path) -> None:
    """Every reader of these tables would otherwise have to be allowlisted,
    and an allowlist that large stops being a control."""
    target = _write(repo_root, 'SQL = "SELECT claim_id FROM lmm_claims WHERE status = :s"\n')
    assert check_file(target) == []


def test_a_similarly_named_table_is_not_matched(repo_root: Path) -> None:
    """`lmm_claims_archive` is not `lmm_claims`. A prefix match would flag
    tables the rule says nothing about, and the noise is how a gate gets
    disabled."""
    target = _write(repo_root, 'SQL = "INSERT INTO lmm_claims_archive (x) VALUES (:x)"\n')
    assert check_file(target) == []


def test_the_permitted_caller_is_exempt_only_for_its_own_table(repo_root: Path) -> None:
    """`claims.py` may write claims; it may not create tenants. An exemption
    that covered every governed table would make one allowlist entry a
    blanket privilege."""
    path = repo_root / "registry" / "registry" / "service"
    path.mkdir(parents=True)
    target = path / "claims.py"
    target.write_text(
        'A = "INSERT INTO lmm_claims (x) VALUES (:x)"\n' 'B = "INSERT INTO tenants (tenant_id) VALUES (:x)"\n',
        encoding="utf-8",
    )

    found = check_file(target)
    assert [v.rule.table for v in found] == ["tenants"]


def test_migrations_are_out_of_scope(repo_root: Path) -> None:
    """Migrations legitimately seed rows during bootstrapping, and the
    migration runner decides when they run."""
    path = repo_root / "registry" / "registry" / "storage" / "migrations" / "versions"
    path.mkdir(parents=True)
    (path / "0099_x.py").write_text('SQL = "INSERT INTO lmm_claims (x) VALUES (:x)"\n')

    assert resolve_targets(["registry/registry"]) == []


def test_an_out_of_scope_path_reports_rather_than_passing_silently(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 on an empty scope is deliberate — a typo'd --paths in CI must not
    read as a clean run without saying so."""
    assert main(["--paths", "does/not/exist"]) == 0
    assert "no files in scope" in capsys.readouterr().err


def test_a_violation_exits_non_zero_and_names_the_file(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = repo_root / "registry" / "registry" / "service"
    path.mkdir(parents=True)
    (path / "rogue.py").write_text('SQL = "INSERT INTO lmm_claims (x) VALUES (:x)"\n')

    assert main(["--paths", "registry/registry"]) == 1
    out = capsys.readouterr()
    assert "registry/registry/service/rogue.py:1" in out.out
    assert "ClaimService" in out.err
