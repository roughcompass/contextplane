"""The visibility-chokepoint gate is the enforcement, so it needs its own tests.

Cross-tenant isolation used to rest entirely on a docstring's say-so: `visibility.py`
declares itself the one place allowed to query `entities`, and a handful of
integration tests exercise a few known endpoints, but nothing scanned the tree for
a *new* module that queries `entities` and never imports the chokepoint at all. A
gate that only a docstring enforces is enforced nowhere a reviewer isn't looking.

Each test here breaks the rule one way — a synthetic module that reads entities
without the chokepoint import — and asserts the gate notices. The mutation is the
point: if the walker matched nothing, every test below would pass for the wrong
reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_visibility_chokepoint import (
    _CHOKEPOINT_MODULE,
    ALLOWLIST,
    check_file,
    main,
    references_entities,
)


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


@pytest.fixture
def repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the gate at a scratch tree so tests never depend on real sources."""
    monkeypatch.setattr("scripts.check_visibility_chokepoint._REPO_ROOT", tmp_path)
    return tmp_path


def test_the_real_tree_passes() -> None:
    """The gate's own subject. Fails the moment a new bypass lands."""
    assert main(["--paths", "registry/service"]) == 0


def test_every_exemption_carries_a_reason() -> None:
    """An exemption with no reason is a bypass wearing the gate's clothes."""
    for exemption in ALLOWLIST:
        assert exemption.reason.strip(), f"{exemption.path} has no stated reason"


# ---------------------------------------------------------------------------
# The mutation: a synthetic module that queries entities with no chokepoint import
# ---------------------------------------------------------------------------


def test_a_raw_sql_read_with_no_chokepoint_import_is_flagged(repo_root: Path) -> None:
    """The gate's sharp edge: a brand-new service module joins in entity rows,
    never imports the chokepoint, and isn't on the allowlist. This must fail —
    if it doesn't, the gate is enforcing nothing for exactly the case it exists
    to catch: the module nobody remembered to route through visibility.py."""
    target = _write(
        repo_root,
        "registry/service/rogue.py",
        (
            "from sqlalchemy import text\n\n"
            "async def leaky_read(session, entity_id):\n"
            '    sql = text("SELECT tenant_id, name FROM entities WHERE entity_id = :eid")\n'
            "    return (await session.execute(sql, {'eid': entity_id})).first()\n"
        ),
    )
    violation = check_file(target, rel="registry/service/rogue.py")
    assert violation is not None
    assert violation.path == "registry/service/rogue.py"
    assert _CHOKEPOINT_MODULE in violation.detail


def test_an_orm_entity_import_with_no_chokepoint_import_is_flagged(repo_root: Path) -> None:
    """The second detection path: a module never writes raw SQL at all, it just
    imports the ORM row and selects it directly."""
    target = _write(
        repo_root,
        "registry/service/rogue_orm.py",
        (
            "from sqlalchemy import select\n"
            "from registry.storage.models import Entity\n\n"
            "async def leaky_read(session, entity_id):\n"
            "    stmt = select(Entity).where(Entity.entity_id == entity_id)\n"
            "    return (await session.execute(stmt)).scalar_one_or_none()\n"
        ),
    )
    violation = check_file(target, rel="registry/service/rogue_orm.py")
    assert violation is not None


def test_the_same_module_with_the_chokepoint_import_is_not_flagged(repo_root: Path) -> None:
    """The control: adding the one import the rogue module was missing clears it.
    Without this, the two tests above could be passing because the walker flags
    every file, not because it detected the specific omission."""
    target = _write(
        repo_root,
        "registry/service/honest.py",
        (
            "from sqlalchemy import text\n"
            f"from {_CHOKEPOINT_MODULE} import VisibilityService\n\n"
            "async def read(session, ctx, entity_ids, visibility: VisibilityService):\n"
            '    sql = text("SELECT tenant_id FROM entities WHERE entity_id = :eid")\n'
            "    visible = await visibility.filter_entities(ctx, entity_ids)\n"
            "    return visible\n"
        ),
    )
    assert check_file(target, rel="registry/service/honest.py") is None


def test_a_module_with_no_entities_reference_is_not_flagged(repo_root: Path) -> None:
    """Most of the tree touches neither the table nor the ORM row at all —
    those files must produce no finding regardless of what else they import."""
    target = _write(
        repo_root,
        "registry/service/unrelated.py",
        "def add(a: int, b: int) -> int:\n    return a + b\n",
    )
    assert check_file(target, rel="registry/service/unrelated.py") is None


def test_the_chokepoint_module_is_not_required_to_import_itself(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/service/governance/visibility.py",
        'from sqlalchemy import text\nSQL = "SELECT tenant_id FROM entities WHERE entity_id = :eid"\n',
    )
    assert check_file(target, rel="registry/service/governance/visibility.py") is None


def test_an_allowlisted_path_is_exempt_by_suffix(repo_root: Path) -> None:
    """Matched as a path suffix, the same way check_privileged_writes.py matches
    permitted callers — true wherever the checkout puts the repo root."""
    exemption = ALLOWLIST[0]
    body = 'from sqlalchemy import text\nSQL = "SELECT tenant_id FROM entities WHERE entity_id = :eid"\n'
    target = _write(repo_root, exemption.path, body)
    assert check_file(target, rel=exemption.path) is None


def test_the_bypass_marker_exempts_a_single_file(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/service/one_off.py",
        (
            "from sqlalchemy import text\n"
            "# visibility-chokepoint: intentional\n"
            'SQL = "SELECT tenant_id FROM entities WHERE entity_id = :eid"\n'
        ),
    )
    assert check_file(target, rel="registry/service/one_off.py") is None


def test_a_write_only_reference_is_not_flagged(repo_root: Path) -> None:
    """Creating your own tenant's entity is not a visibility question — only
    FROM/JOIN (a read) counts. An INSERT that never selects a row would
    otherwise force every entity-creation path onto the allowlist for no
    reason connected to cross-tenant exposure."""
    target = _write(
        repo_root,
        "registry/service/creator.py",
        ("from sqlalchemy import text\n" 'SQL = "INSERT INTO entities (entity_id, tenant_id) VALUES (:eid, :tid)"\n'),
    )
    assert check_file(target, rel="registry/service/creator.py") is None


def test_references_entities_detects_join_as_well_as_from() -> None:
    import ast

    source = 'SQL = "SELECT 1 FROM attributes a JOIN entities e USING (entity_id)"\n'
    assert references_entities(source, ast.parse(source))


def test_references_entities_is_case_insensitive() -> None:
    import ast

    source = 'sql = "select tenant_id from entities where entity_id = :eid"\n'
    assert references_entities(source, ast.parse(source))


def test_main_exits_nonzero_and_names_the_file(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(
        repo_root,
        "registry/service/rogue.py",
        'from sqlalchemy import text\nSQL = "SELECT tenant_id FROM entities WHERE entity_id = :eid"\n',
    )
    assert main(["--paths", "registry/service"]) == 1
    out = capsys.readouterr().out
    assert "registry/service/rogue.py" in out
    assert _CHOKEPOINT_MODULE in out


def test_a_stale_exemption_fails_rather_than_passing_silently(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An allowlist entry for a file that no longer touches entities is a
    standing permission nobody is using — the gate should say so rather than
    quietly carrying it forever."""
    exemption = ALLOWLIST[0]
    _write(repo_root, exemption.path, "def noop() -> None:\n    return None\n")
    assert main(["--paths", "registry/service"]) == 1
    err = capsys.readouterr()
    assert "stale-exemption" in err.out or "stale-exemption" in err.err


def test_an_out_of_scope_path_fails_rather_than_passing_silently(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--paths", "does/not/exist"]) == 1
    assert "scope does not exist" in capsys.readouterr().err
