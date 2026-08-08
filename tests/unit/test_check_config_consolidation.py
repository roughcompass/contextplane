"""The config-consolidation gate is the enforcement CLAUDE.md's "Secrets and
config" section promises, so it needs its own tests. Before this gate existed,
that section named a mechanism ("triggers the consolidation gate") nothing
implemented -- these tests plant a synthetic violation (or clearance) in a
scratch tree and assert the walker notices; the mutation is the point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_config_consolidation import (
    ALLOWLIST,
    Exemption,
    check_file,
    main,
)


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


@pytest.fixture
def repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the gate at a scratch tree so tests never depend on real sources."""
    monkeypatch.setattr("scripts.check_config_consolidation._REPO_ROOT", tmp_path)
    return tmp_path


def test_the_real_tree_passes() -> None:
    """The gate's own subject. Fails the moment a new bypass lands unmarked
    or unregistered."""
    assert main([]) == 0


def test_every_exemption_carries_a_reason() -> None:
    for exemption in ALLOWLIST:
        assert exemption.reason.strip(), f"{exemption.path} has no stated reason"
        assert exemption.path.strip(), "an Exemption names no path"


# ---------------------------------------------------------------------------
# Detection: every matched shape
# ---------------------------------------------------------------------------


def test_environ_get_call_is_flagged(repo_root: Path) -> None:
    target = _write(repo_root, "contextplane/rogue/a.py", ('import os\n\ndef f():\n    return os.environ.get("FOO")\n'))
    violations = check_file(target, rel="contextplane/rogue/a.py", allowlisted=frozenset())
    assert len(violations) == 1
    assert violations[0].kind == "unmarked"
    assert violations[0].line == 4
    assert "os.environ.get" in violations[0].detail


def test_os_getenv_call_is_flagged(repo_root: Path) -> None:
    target = _write(repo_root, "contextplane/rogue/b.py", ('import os\n\ndef f():\n    return os.getenv("FOO")\n'))
    violations = check_file(target, rel="contextplane/rogue/b.py", allowlisted=frozenset())
    assert len(violations) == 1
    assert "os.getenv" in violations[0].detail


def test_subscript_read_is_flagged(repo_root: Path) -> None:
    target = _write(repo_root, "contextplane/rogue/c.py", ('import os\n\ndef f():\n    return os.environ["FOO"]\n'))
    violations = check_file(target, rel="contextplane/rogue/c.py", allowlisted=frozenset())
    assert len(violations) == 1
    assert "os.environ[...] read" in violations[0].detail


def test_containment_check_is_flagged(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "contextplane/rogue/d.py",
        ('import os\n\ndef f():\n    if "FOO" in os.environ:\n        return True\n    return False\n'),
    )
    violations = check_file(target, rel="contextplane/rogue/d.py", allowlisted=frozenset())
    assert len(violations) == 1
    assert "in os.environ check" in violations[0].detail


def test_not_in_containment_check_is_flagged(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "contextplane/rogue/e.py",
        ('import os\n\ndef f():\n    if "FOO" not in os.environ:\n        return True\n    return False\n'),
    )
    violations = check_file(target, rel="contextplane/rogue/e.py", allowlisted=frozenset())
    assert len(violations) == 1
    assert "not in os.environ check" in violations[0].detail


def test_whole_environment_access_is_flagged(repo_root: Path) -> None:
    target = _write(repo_root, "contextplane/rogue/f.py", ("import os\n\ndef f():\n    return dict(os.environ)\n"))
    violations = check_file(target, rel="contextplane/rogue/f.py", allowlisted=frozenset())
    assert len(violations) == 1
    assert "whole-environment access" in violations[0].detail


def test_subscript_assignment_is_not_flagged(repo_root: Path) -> None:
    """A write (`os.environ[NAME] = value`) is not a config read -- see the
    module docstring's "What counts" section."""
    target = _write(repo_root, "contextplane/rogue/g.py", ('import os\n\ndef f():\n    os.environ["FOO"] = "bar"\n'))
    violations = check_file(target, rel="contextplane/rogue/g.py", allowlisted=frozenset())
    assert violations == []


def test_setdefault_is_not_flagged(repo_root: Path) -> None:
    target = _write(
        repo_root, "contextplane/rogue/h.py", ('import os\n\ndef f():\n    os.environ.setdefault("FOO", "bar")\n')
    )
    violations = check_file(target, rel="contextplane/rogue/h.py", allowlisted=frozenset())
    assert violations == []


def test_unrelated_environ_name_is_not_flagged(repo_root: Path) -> None:
    """A local variable named `environ` that has nothing to do with `os` must
    not trip the gate -- only the `os.environ` attribute chain matches."""
    target = _write(
        repo_root,
        "contextplane/rogue/i.py",
        ("def f(environ):\n    return environ.get('FOO')\n"),
    )
    violations = check_file(target, rel="contextplane/rogue/i.py", allowlisted=frozenset())
    assert violations == []


# ---------------------------------------------------------------------------
# Marker clears a hit, but only when the file is registered
# ---------------------------------------------------------------------------


def test_marked_but_unregistered_is_flagged(repo_root: Path) -> None:
    """A `# config: intentional` marker with no matching ALLOWLIST entry is
    itself a violation -- the marker alone is not the mechanism; a named,
    reasoned Exemption is."""
    target = _write(
        repo_root,
        "contextplane/rogue/j.py",
        ('import os\n\ndef f():\n    return os.environ.get("FOO")  # config: intentional\n'),
    )
    violations = check_file(target, rel="contextplane/rogue/j.py", allowlisted=frozenset())
    assert len(violations) == 1
    assert violations[0].kind == "unregistered"


def test_marked_and_registered_clears(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "contextplane/rogue/k.py",
        ('import os\n\ndef f():\n    return os.environ.get("FOO")  # config: intentional\n'),
    )
    violations = check_file(target, rel="contextplane/rogue/k.py", allowlisted=frozenset({"contextplane/rogue/k.py"}))
    assert violations == []


def test_marker_on_wrapping_statement_clears_a_multiline_call(repo_root: Path) -> None:
    """The real shape in contextplane/api/middleware/http_methods.py: the marker
    sits on the assignment's opening line, the actual os.environ.get(...)
    call is on the next line. The marker must still be found."""
    target = _write(
        repo_root,
        "contextplane/rogue/l.py",
        (
            "import os\n\n"
            "def f():\n"
            "    raw = (  # config: intentional\n"
            '        os.environ.get("FOO", "default")\n'
            "    )\n"
            "    return raw\n"
        ),
    )
    violations = check_file(target, rel="contextplane/rogue/l.py", allowlisted=frozenset({"contextplane/rogue/l.py"}))
    assert violations == []


def test_mixed_marked_and_unmarked_sites_flags_only_the_unmarked_one(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "contextplane/rogue/m.py",
        (
            "import os\n\n"
            "def f():\n"
            '    a = os.environ.get("FOO")  # config: intentional\n'
            '    b = os.environ.get("BAR")\n'
            "    return a, b\n"
        ),
    )
    violations = check_file(target, rel="contextplane/rogue/m.py", allowlisted=frozenset({"contextplane/rogue/m.py"}))
    assert len(violations) == 1
    assert violations[0].kind == "unmarked"
    assert violations[0].line == 5


# ---------------------------------------------------------------------------
# Stale-exemption detection
# ---------------------------------------------------------------------------


def test_stale_exemption_for_missing_file_is_reported(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.check_config_consolidation.ALLOWLIST",
        (Exemption(path="contextplane/nowhere.py", reason="test"),),
    )
    assert main([]) == 1


def test_stale_exemption_for_no_remaining_marked_site_is_reported(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo_root, "contextplane/clean.py", ("def f() -> int:\n    return 1\n"))
    monkeypatch.setattr(
        "scripts.check_config_consolidation.ALLOWLIST",
        (Exemption(path="contextplane/clean.py", reason="test"),),
    )
    assert main([]) == 1


def test_documented_allowlist_files_all_have_a_marked_site() -> None:
    """Every real ALLOWLIST entry corresponds to a file that still carries at
    least one marked env-touching site -- the same invariant the stale-entry
    check enforces at gate time, pinned here so a future edit to any of these
    files that removes the last marker is caught even if nobody runs the
    full gate against the exact default scope."""
    assert main([]) == 0
