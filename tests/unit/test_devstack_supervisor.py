"""The supervised API's reload and shutdown arguments mean what they say.

Both of the behaviours pinned here failed silently before they were pinned,
and silence is the reason they are worth a test rather than a comment.

The reloader accepts an exclusion that never matches anything and reports no
error, so a relative value looks configured and does nothing. And it treats a
path that is not a directory as a glob pattern, which for an absolute value is
an error raised while the child process is starting — so an exclusion naming a
tree that a given checkout happens not to have does not weaken the exclusion,
it stops the API booting.

Neither is visible from reading the argument list. A test that constructs the
real filter is.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from uvicorn.config import Config
from uvicorn.supervisors.watchfilesreload import FileFilter

from scripts.devstack.config import Ports
from scripts.devstack.supervisor import API_SHUTDOWN_TIMEOUT_S, reload_exclude_args, services


def _api_argv() -> list[str]:
    return [str(arg) for service in services(Ports()) if service.name == "api" for arg in service.argv]


def _tree(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir(parents=True)


class TestReloadExcludeArgs:
    def test_every_exclusion_is_absolute(self, tmp_path: Path) -> None:
        _tree(tmp_path, "tests", ".venv")

        values = [arg for arg in reload_exclude_args(tmp_path) if arg != "--reload-exclude"]

        assert values, "expected at least one exclusion for a tree that exists"
        assert all(Path(value).is_absolute() for value in values), values

    def test_absent_trees_are_omitted_rather_than_named(self, tmp_path: Path) -> None:
        # Only one of the candidate trees exists here, which is the situation a
        # fresh clone and a git worktree are both in.
        _tree(tmp_path, "tests")

        values = [arg for arg in reload_exclude_args(tmp_path) if arg != "--reload-exclude"]

        assert values == [str(tmp_path / "tests")]

    def test_naming_an_absent_tree_would_break_startup(self, tmp_path: Path) -> None:
        # The reason the filter above exists. A non-directory exclusion is
        # treated as a glob pattern, and an absolute glob is rejected outright.
        with pytest.raises((NotImplementedError, ValueError)):
            Config(
                "registry.main:create_app",
                factory=True,
                reload=True,
                reload_excludes=[str(tmp_path / "does-not-exist")],
            )

    def test_excluded_trees_do_not_trigger_a_reload(self, tmp_path: Path) -> None:
        _tree(tmp_path, "tests", "registry", "docs")
        config = Config(
            "registry.main:create_app",
            factory=True,
            reload=True,
            reload_excludes=[str(tmp_path / "tests")],
        )
        matches = FileFilter(config)

        assert matches(tmp_path / "registry" / "main.py") is True
        assert matches(tmp_path / "tests" / "unit" / "test_anything.py") is False
        assert matches(tmp_path / "tests" / "conftest.py") is False
        # Not a Python file, so it was never a reload trigger to begin with.
        assert matches(tmp_path / "docs" / "README.md") is False

    def test_a_relative_exclusion_would_match_nothing(self, tmp_path: Path) -> None:
        # Pinning the trap, not the behaviour we want: the filter compares an
        # exclusion against the parents of an absolute path.
        config = Config("registry.main:create_app", factory=True, reload=True, reload_excludes=["tests"])
        matches = FileFilter(config)

        assert matches(tmp_path / "tests" / "unit" / "test_anything.py") is True


class TestApiArgv:
    def test_shutdown_is_bounded(self) -> None:
        # Unbounded is the default, and it waits for a stream that never ends.
        argv = _api_argv()

        assert "--timeout-graceful-shutdown" in argv
        assert argv[argv.index("--timeout-graceful-shutdown") + 1] == str(API_SHUTDOWN_TIMEOUT_S)

    def test_the_test_suite_is_excluded(self) -> None:
        argv = _api_argv()

        excluded = {argv[i + 1] for i, arg in enumerate(argv) if arg == "--reload-exclude"}
        assert any(value.endswith("/tests") for value in excluded), excluded
