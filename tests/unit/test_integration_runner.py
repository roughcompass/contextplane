"""Qualification, proved by running the real script in a real subprocess.

An in-process call to `qualify()` proves the function's logic and nothing
about the boundary that matters: whether an environment variable exported by
a caller actually reaches the runner and is actually refused. Several cases
here therefore spend a process, because the thing under test is the process
boundary itself. A synthetic assertion over a manifest would pass whether or
not the script rejects anything.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - these cases exist to cross a real process boundary
import sys
from pathlib import Path

import pytest

from scripts.run_integration_tests import (
    QualificationError,
    build_child_environment,
    collection_command,
    collection_digest,
    forbidden_arguments,
    forbidden_variables,
    parse_collection,
    qualify,
    worker_command,
)

RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "run_integration_tests.py"


def run_runner(env_overrides: dict[str, str], args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the real script with a deliberately minimal environment."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        **env_overrides,
    }
    return subprocess.run(
        [sys.executable, str(RUNNER), *(args or [])],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


# --------------------------------------------------------------------------
# Real subprocess: a caller cannot replace the runner or the interpreter
# --------------------------------------------------------------------------


@pytest.mark.parametrize("variable", ["PYTEST", "PYTHON"])
def test_replacing_the_runner_or_interpreter_is_refused_at_entry(variable: str) -> None:
    """`PYTEST=true` makes the suite "pass" instantly by running nothing."""
    result = run_runner({variable: "true"})

    assert result.returncode == 2
    assert variable in result.stderr
    assert "refusing to run" in result.stderr


@pytest.mark.parametrize(
    "variable,value",
    [
        ("PYTEST_ADDOPTS", "-k test_nothing"),
        ("PYTEST_PLUGINS", "evil_plugin"),
        ("PYTEST_TIMEOUT", "1"),
    ],
)
def test_pytest_option_channels_are_refused(variable: str, value: str) -> None:
    """These change what runs without ever appearing in argv."""
    result = run_runner({variable: value})

    assert result.returncode == 2
    assert variable in result.stderr


@pytest.mark.parametrize("variable", ["MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "MAKEOVERRIDES"])
def test_make_level_overrides_are_refused(variable: str) -> None:
    result = run_runner({variable: "PYTEST=true"})

    assert result.returncode == 2
    assert variable in result.stderr


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"])
def test_redirected_git_variables_are_refused(variable: str, tmp_path: Path) -> None:
    """Refused even when pointed at a clean, real, different repository — the
    provenance the controller records would describe that repository, not the
    one under measurement."""
    other = tmp_path / "other-repo"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)

    result = run_runner({variable: str(other / ".git")})

    assert result.returncode == 2
    assert variable in result.stderr


def test_a_preloaded_makefile_is_refused_and_its_name_is_retained(tmp_path: Path) -> None:
    """The `MAKEFILES` case, with a real readable makefile that really does
    try to replace both variables.

    This is the subtlest channel in the set: the preloaded file redefines
    `PYTEST` and `PYTHON` inside Make, so neither name ever appears in the
    environment. Only the presence of `MAKEFILES` itself gives it away, which
    is why the check is on the variable and not on what it points at.
    """
    preload = tmp_path / "evil.mk"
    preload.write_text("PYTEST = true\nPYTHON = true\n", encoding="utf-8")
    preload.chmod(0o644)

    result = run_runner({"MAKEFILES": str(preload)})

    assert result.returncode == 2
    assert "MAKEFILES" in result.stderr
    # The name is retained; the path it pointed at is not something to repeat.
    assert str(preload) not in result.stderr


def test_a_clean_invocation_is_not_refused() -> None:
    """The negative control. Without it, a script that refused everything
    would pass every test above."""
    result = run_runner({})

    assert "refusing to run" not in result.stderr


# --------------------------------------------------------------------------
# Qualification logic
# --------------------------------------------------------------------------


def test_presence_is_the_failure_even_though_the_child_would_be_clean() -> None:
    """The rule most likely to be softened by a later reader.

    Scrubbing the variable and continuing would produce a clean child and a
    passing gate — and an attempt that successfully hid itself is exactly what
    makes the evidence worthless. So the attempt is the failure.
    """
    tampered = {"PATH": "/usr/bin", "PYTEST": "true"}

    assert "PYTEST" not in build_child_environment(tampered)
    with pytest.raises(QualificationError, match="PYTEST"):
        qualify(tampered, [])


@pytest.mark.parametrize(
    "argument",
    ["-k", "-m", "--deselect", "--last-failed", "--maxfail", "--reruns", "-n", "--dist"],
)
def test_selector_and_rerun_arguments_are_refused(argument: str) -> None:
    with pytest.raises(QualificationError, match="forbidden argument"):
        qualify({}, [argument, "value"])


def test_the_equals_spelling_of_a_selector_is_also_refused() -> None:
    """`--maxfail 1` and `--maxfail=1` are the same instruction."""
    assert forbidden_arguments(["--maxfail=1"]) == ("--maxfail",)


def test_every_attempted_variable_is_reported_not_only_the_first() -> None:
    attempted = forbidden_variables({"PYTEST": "x", "MAKEFILES": "y", "PATH": "/usr/bin"})

    assert attempted == ("MAKEFILES", "PYTEST")


def test_unknown_pytest_channels_are_caught_by_the_family_rule() -> None:
    """No fixed list can enumerate pytest's option channels across versions."""
    assert forbidden_variables({"PYTEST_SOMETHING_NEW": "x"}) == ("PYTEST_SOMETHING_NEW",)


# --------------------------------------------------------------------------
# The child environment
# --------------------------------------------------------------------------


def test_the_child_environment_is_built_up_rather_than_filtered_down() -> None:
    """A variable nobody thought about is absent by default."""
    child = build_child_environment({"PATH": "/usr/bin", "SOMETHING_INVENTED_LATER": "x"})

    assert "SOMETHING_INVENTED_LATER" not in child
    assert child["PATH"] == "/usr/bin"


def test_plugin_autoload_is_disabled_in_the_child() -> None:
    """An installed plugin must not be able to join a measured run."""
    assert build_child_environment({})["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_hash_seed_is_pinned_so_the_collection_digest_describes_the_tree() -> None:
    assert build_child_environment({})["PYTHONHASHSEED"] == "0"


def test_the_provider_and_control_reach_the_child() -> None:
    child = build_child_environment(
        {"CONTEXTPLANE_TEST_PG": "devstack", "CONTEXTPLANE_INTEGRATION_CONTROL": "control-path"}
    )

    assert child["CONTEXTPLANE_TEST_PG"] == "devstack"
    assert child["CONTEXTPLANE_INTEGRATION_CONTROL"] == "control-path"


# --------------------------------------------------------------------------
# Sealed commands and the collection digest
# --------------------------------------------------------------------------


def test_pytest_is_always_invoked_through_this_interpreter() -> None:
    """Never a bare `pytest` off PATH, which is whatever the caller installed."""
    assert collection_command()[:3] == [sys.executable, "-m", "pytest"]
    assert worker_command(["a::b"])[:3] == [sys.executable, "-m", "pytest"]


def test_only_the_required_plugins_are_loaded() -> None:
    command = collection_command()

    assert "pytest_asyncio.plugin" in command
    assert "no:cacheprovider" in command


def test_the_collection_command_names_the_whole_integration_root() -> None:
    """Dynamic collection of everything, never a curated list."""
    assert "tests/integration" in collection_command()


def test_adding_one_test_changes_the_collection_digest() -> None:
    before = collection_digest(["a.py::test_one", "b.py::test_two"])
    after = collection_digest(["a.py::test_one", "b.py::test_two", "c.py::test_three"])

    assert before != after


def test_collection_order_does_not_change_the_digest() -> None:
    """pytest does not promise order across filesystems; the tree is the fact."""
    forward = collection_digest(["a.py::test_one", "b.py::test_two"])
    reversed_order = collection_digest(["b.py::test_two", "a.py::test_one"])

    assert forward == reversed_order


def test_collection_output_is_parsed_by_shape_not_by_prose() -> None:
    stdout = (
        "tests/integration/test_a.py::test_one\ntests/integration/test_a.py::test_two\n\n2 tests collected in 0.4s\n"
    )

    assert parse_collection(stdout) == (
        "tests/integration/test_a.py::test_one",
        "tests/integration/test_a.py::test_two",
    )


def test_summary_lines_are_not_mistaken_for_nodes() -> None:
    assert parse_collection("\n12 tests collected\n") == ()
