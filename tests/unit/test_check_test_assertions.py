"""The assertion-less-test gate is itself load-bearing for every other test in
the suite, so it needs the same kind of proof every sibling structural gate in
this repository gets: plant one shape of vacuous (or proven, or stale) test in
a scratch tree and confirm the checker calls it correctly, rather than trusting
that an AST walk written once continues to mean what its docstring says.

Each test below either drives the checker's own file-level API (`check_file`)
against a single scratch source file -- for the resolution rules, where the
question is "does this one file's shape count as proven" -- or drives the CLI
entry point (`main`) against a scratch `_REPO_ROOT` -- for allowlist and
reporting behavior, where the question spans the whole run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_test_assertions import ALLOWLIST, check_file, main

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the gate at a scratch tree so CLI-level tests never depend on
    the real suite's current assertion-less count."""
    monkeypatch.setattr("scripts.check_test_assertions._REPO_ROOT", tmp_path)
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
    """The gate's own reason to exist. Fails the moment a new vacuous test
    lands un-allowlisted, or an existing allowlist entry goes stale."""
    assert main([]) == 0


def test_explain_exits_zero_and_prints_the_criterion(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--explain"]) == 0
    out = capsys.readouterr().out
    assert "assertion-less-test gate" in out
    assert "same-file helper" in out
    assert f"Currently allowlisted ({len(ALLOWLIST)})" in out


# ---------------------------------------------------------------------------
# Direct detection: a plainly vacuous test
# ---------------------------------------------------------------------------


def test_a_plainly_vacuous_test_is_detected_with_file_and_line(repo_root: Path) -> None:
    path = _write(
        repo_root,
        "tests/unit/test_thing.py",
        "def test_nothing():\n    do_something()\n",
    )
    violations = check_file(path, rel="tests/unit/test_thing.py")
    assert len(violations) == 1
    violation = violations[0]
    assert violation.path == "tests/unit/test_thing.py"
    assert violation.function == "test_nothing"
    assert violation.line == 1


# ---------------------------------------------------------------------------
# Same-file helper resolution
# ---------------------------------------------------------------------------


def test_a_module_level_helper_that_asserts_clears_the_test(repo_root: Path) -> None:
    path = _write(
        repo_root,
        "tests/unit/test_thing.py",
        "def _helper():\n    assert True\n\n\ndef test_ok():\n    _helper()\n",
    )
    assert check_file(path, rel="tests/unit/test_thing.py") == []


def test_a_same_class_helper_reached_via_self_clears_the_test(repo_root: Path) -> None:
    path = _write(
        repo_root,
        "tests/unit/test_thing.py",
        "class TestThing:\n"
        "    def _expect_ok(self):\n"
        "        assert True\n\n"
        "    def test_ok(self):\n"
        "        self._expect_ok()\n",
    )
    assert check_file(path, rel="tests/unit/test_thing.py") == []


def test_a_same_class_helper_reached_via_cls_clears_the_test(repo_root: Path) -> None:
    path = _write(
        repo_root,
        "tests/unit/test_thing.py",
        "class TestThing:\n"
        "    @classmethod\n"
        "    def _expect_ok(cls):\n"
        "        assert True\n\n"
        "    @classmethod\n"
        "    def test_ok(cls):\n"
        "        cls._expect_ok()\n",
    )
    assert check_file(path, rel="tests/unit/test_thing.py") == []


def test_transitive_resolution_through_two_same_file_helpers_clears_the_test(repo_root: Path) -> None:
    """test_ok -> _helper_a -> _helper_b, where only _helper_b asserts. If the
    gate only followed one hop, this would still read as vacuous."""
    path = _write(
        repo_root,
        "tests/unit/test_thing.py",
        "def _helper_b():\n"
        "    assert True\n\n\n"
        "def _helper_a():\n"
        "    _helper_b()\n\n\n"
        "def test_ok():\n"
        "    _helper_a()\n",
    )
    assert check_file(path, rel="tests/unit/test_thing.py") == []


def test_a_helper_in_a_different_file_does_not_clear_the_test(repo_root: Path) -> None:
    """The criterion is same-file by design: a helper the test only reaches
    through an import is not something a reviewer reading this file alone can
    verify, so it must not count."""
    _write(repo_root, "tests/unit/_helpers.py", "def helper():\n    assert True\n")
    path = _write(
        repo_root,
        "tests/unit/test_thing.py",
        "from tests.unit._helpers import helper\n\n\ndef test_ok():\n    helper()\n",
    )
    violations = check_file(path, rel="tests/unit/test_thing.py")
    assert len(violations) == 1
    assert violations[0].function == "test_ok"


def test_mock_style_assert_called_once_with_counts_as_an_assertion(repo_root: Path) -> None:
    path = _write(
        repo_root,
        "tests/unit/test_thing.py",
        "def test_ok():\n    m = Mock()\n    m(1)\n    m.assert_called_once_with(1)\n",
    )
    assert check_file(path, rel="tests/unit/test_thing.py") == []


def test_pytest_raises_block_counts_as_an_assertion(repo_root: Path) -> None:
    path = _write(
        repo_root,
        "tests/unit/test_thing.py",
        "import pytest\n\n\ndef test_ok():\n    with pytest.raises(ValueError):\n        raise ValueError()\n",
    )
    assert check_file(path, rel="tests/unit/test_thing.py") == []


# ---------------------------------------------------------------------------
# Stale allowlist entries
# ---------------------------------------------------------------------------


def test_a_stale_entry_for_a_test_that_no_longer_exists_fails_the_gate(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The named function was renamed or removed -- the file is still there,
    but nothing in it matches the allowlist entry's name."""
    _write(repo_root, "tests/unit/test_thing.py", "def test_ok():\n    assert True\n")
    monkeypatch.setattr(
        "scripts.check_test_assertions.ALLOWLIST",
        (("tests/unit/test_thing.py", "test_removed", "was vacuous once"),),
    )
    assert main(["--paths", "tests/unit"]) == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "stale-allowlist-entry" in combined
    assert "test_removed" in combined
    assert "no longer matches an assertion-less test" in combined


def test_a_stale_entry_for_a_test_that_now_asserts_fails_the_gate(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The named function still exists under that name, but its body now
    proves something -- the allowlist entry is a standing permission nobody
    is using anymore."""
    _write(repo_root, "tests/unit/test_thing.py", "def test_fixed():\n    assert True\n")
    monkeypatch.setattr(
        "scripts.check_test_assertions.ALLOWLIST",
        (("tests/unit/test_thing.py", "test_fixed", "was vacuous once"),),
    )
    assert main(["--paths", "tests/unit"]) == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "stale-allowlist-entry" in combined
    assert "test_fixed" in combined


def test_paths_narrowing_does_not_hide_a_stale_entry_outside_scope(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale entry naming a tests/integration file must still fail a run
    scoped to `--paths tests/unit` -- the docstring says the stale check runs
    against each entry's own file directly, independent of the scan scope,
    and this is the sharp edge that claim rests on."""
    _write(repo_root, "tests/unit/test_thing.py", "def test_ok():\n    assert True\n")
    _write(repo_root, "tests/integration/test_other.py", "def test_ok():\n    assert True\n")
    monkeypatch.setattr(
        "scripts.check_test_assertions.ALLOWLIST",
        (("tests/integration/test_other.py", "test_missing", "stale on purpose"),),
    )
    assert main(["--paths", "tests/unit"]) == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "stale-allowlist-entry" in combined
    assert "test_missing" in combined
