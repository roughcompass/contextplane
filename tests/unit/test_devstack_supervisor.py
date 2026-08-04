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

import argparse
from pathlib import Path

import pytest
from uvicorn.config import Config
from uvicorn.supervisors.watchfilesreload import FileFilter

from scripts.devstack.config import Ports
from scripts.devstack.pg_provider import PostgresUnavailableError
from scripts.devstack.supervisor import (
    API_SHUTDOWN_TIMEOUT_S,
    Supervisor,
    reload_exclude_args,
    services,
)


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


class TestProcessLedger:
    """The record of what is running has to outlive a failed shutdown.

    Three orphaned API processes were found holding the stack's ports with
    nothing tracking them, and the sequence that produced them is the one pinned
    here: `down` removed the state file before confirming the children had
    exited, so `status` had nothing left to report and `down` had nothing left to
    signal. The processes answered health checks for hours.
    """

    def test_survivors_reports_only_the_living(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        supervisor = Supervisor(tmp_path, Ports())
        supervisor.write_state({"api": 111, "obs": 222})

        monkeypatch.setattr("scripts.devstack.supervisor._pid_alive", lambda pid: pid == 111)

        assert supervisor.survivors() == {"api": 111}

    def test_down_keeps_the_record_when_something_survives(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from scripts.devstack import cli

        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        supervisor = Supervisor(tmp_path, Ports())
        supervisor.write_state({"api": 111})
        monkeypatch.setattr(Supervisor, "stop_all", lambda self: ["api"])
        monkeypatch.setattr(Supervisor, "survivors", lambda self: {"api": 111})
        monkeypatch.setattr(cli, "resolve_local", lambda: (_ for _ in ()).throw(PostgresUnavailableError("none")))

        assert cli.cmd_down(argparse.Namespace()) == 0

        assert supervisor.state_path.is_file(), "a surviving process must stay tracked"
        assert "still running after shutdown" in capsys.readouterr().out

    def test_down_clears_the_record_once_everything_is_gone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.devstack import cli

        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        supervisor = Supervisor(tmp_path, Ports())
        supervisor.write_state({"api": 111})
        monkeypatch.setattr(Supervisor, "stop_all", lambda self: ["api"])
        monkeypatch.setattr(Supervisor, "survivors", lambda self: {})
        monkeypatch.setattr(cli, "resolve_local", lambda: (_ for _ in ()).throw(PostgresUnavailableError("none")))

        assert cli.cmd_down(argparse.Namespace()) == 0
        assert not supervisor.state_path.exists()


class TestReclaim:
    def test_nothing_is_signalled_without_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.devstack import cli

        monkeypatch.setattr(cli, "port_holders", lambda port: [cli.PortHolder(pid=999, age="1:00", command="x")])
        monkeypatch.setattr(cli.os, "kill", lambda *a: pytest.fail("signalled without --reclaim"))

        assert cli._offer_reclaim(Ports(), ["api"], reclaim=False) is False

    def test_nothing_is_signalled_without_a_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The flag asks for permission; it does not grant it. A port can be held
        # by something the developer very much wants to keep.
        from scripts.devstack import cli

        monkeypatch.setattr(cli, "port_holders", lambda port: [cli.PortHolder(pid=999, age="1:00", command="x")])
        monkeypatch.setattr("builtins.input", lambda: "n")
        monkeypatch.setattr(cli.os, "kill", lambda *a: pytest.fail("signalled after a refusal"))

        assert cli._offer_reclaim(Ports(), ["api"], reclaim=True) is False

    def test_an_unidentifiable_holder_is_not_offered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "Something is on this port, shall I kill it" is not an answerable
        # question, so it is not asked.
        from scripts.devstack import cli

        monkeypatch.setattr(cli, "port_holders", lambda port: [])
        monkeypatch.setattr("builtins.input", lambda: pytest.fail("asked about an unnamed process"))

        assert cli._offer_reclaim(Ports(), ["api"], reclaim=True) is False
