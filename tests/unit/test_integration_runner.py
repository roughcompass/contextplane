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

# Imported bare, exactly as the runner imports it. `scripts.integration_scheduler`
# and `integration_scheduler` are two module objects at runtime, so an exception
# raised through one is not caught by the other's class.
from integration_control import (
    BROKER_ENDPOINT_VARIABLE,
    CONTROL_ENVIRONMENT_VARIABLE,
    ControlRejected,
)
from integration_scheduler import (
    FrozenHistory,
    HistoryKey,
    NodeOutcome,
    RunInvalid,
    balance,
)

from scripts.run_integration_tests import (
    QualificationError,
    authorize,
    build_child_environment,
    collection_command,
    collection_digest,
    committed_worker_count,
    dispatch,
    forbidden_arguments,
    forbidden_variables,
    parse_collection,
    parse_events,
    qualify,
    resolve_worker_count,
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


# --- dispatch, proved by running real workers over a real temporary suite -----
#
# The execution half is exactly where a runner can look healthy and do nothing:
# a parent that collects, reports a node count and exits 0 is indistinguishable
# from a passing suite until somebody checks whether a test ran. These cases
# therefore assert on outcomes that only exist if pytest actually executed the
# node, and one of them asserts a failing test is reported as failing.


_SUITE = """
import pytest


def test_one_passes():
    assert True


def test_two_passes():
    assert True


def test_three_fails():
    assert False


@pytest.mark.skip(reason="deliberate")
def test_four_skips():
    pass


@pytest.fixture
def broken():
    raise RuntimeError("fixture blew up")


def test_five_errors(broken):
    pass
"""


def _suite(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    module = tmp_path / "test_sample_suite.py"
    module.write_text(_SUITE, encoding="utf-8")
    names = (
        "test_one_passes",
        "test_two_passes",
        "test_three_fails",
        "test_four_skips",
        "test_five_errors",
    )
    return module, tuple(f"test_sample_suite.py::{name}" for name in names)


def _history(nodes: tuple[str, ...]) -> FrozenHistory:
    key = HistoryKey(
        source_collection_digest=collection_digest(nodes),
        provider="none",
        schema_fingerprint="test",
        host_digest="test",
        topology="workers=2",
    )
    return FrozenHistory(key=key, durations={})


def test_dispatch_actually_runs_the_nodes_and_reports_each_outcome(tmp_path: Path) -> None:
    """The case that would have caught a runner that collects and exits 0.

    Every assertion here is about a value that cannot exist unless pytest
    executed the node: a passing test, a failing one, a skip, and a fixture
    that raises. A collect-only runner satisfies none of them.
    """
    _, nodes = _suite(tmp_path)
    schedule = balance(nodes, workers=2, history=_history(nodes))

    outcomes, results = dispatch(
        schedule,
        {"PATH": os.environ["PATH"]},
        events_root=tmp_path / "events",
        cwd=tmp_path,
    )

    assert outcomes == {
        "test_sample_suite.py::test_one_passes": NodeOutcome.PASSED,
        "test_sample_suite.py::test_two_passes": NodeOutcome.PASSED,
        "test_sample_suite.py::test_three_fails": NodeOutcome.FAILED,
        "test_sample_suite.py::test_four_skips": NodeOutcome.SKIPPED,
        "test_sample_suite.py::test_five_errors": NodeOutcome.ERROR,
    }
    assert len(results) == 2
    assert any(result.returncode != 0 for result in results), "a worker holding a failing node must exit nonzero"


def test_every_worker_numbers_its_own_events_contiguously_from_one(tmp_path: Path) -> None:
    """A gap is how a lost result hides, so the numbering is asserted directly."""
    _, nodes = _suite(tmp_path)
    schedule = balance(nodes, workers=2, history=_history(nodes))
    events_root = tmp_path / "events"

    dispatch(schedule, {"PATH": os.environ["PATH"]}, events_root=events_root, cwd=tmp_path)

    for stream in sorted(events_root.glob("events-*.jsonl")):
        events = parse_events(stream.read_text(encoding="utf-8"))
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert len({event.worker_id for event in events}) == 1


def test_a_malformed_event_line_voids_the_run() -> None:
    """Tolerating an unreadable record would shorten the aggregation silently."""
    with pytest.raises(RunInvalid, match="malformed worker event on line 1"):
        parse_events('{"worker_id": "w1", "sequence": "not-a-number"}\n')


def test_the_worker_count_defaults_to_serial_when_none_has_been_committed(tmp_path: Path) -> None:
    """Serial is the only count whose correctness needs no measurement."""
    empty = tmp_path / "pyproject.toml"
    empty.write_text("[project]\nname = 'x'\n", encoding="utf-8")
    assert committed_worker_count(pyproject=empty) == 1


def test_a_committed_worker_count_is_read_from_the_tracked_file(tmp_path: Path) -> None:
    committed = tmp_path / "pyproject.toml"
    committed.write_text("[tool.contextplane.integration]\nworkers = 4\n", encoding="utf-8")
    assert committed_worker_count(pyproject=committed) == 4


def test_a_nonsense_committed_worker_count_is_refused(tmp_path: Path) -> None:
    """Rather than silently falling back, which would hide a bad commit."""
    committed = tmp_path / "pyproject.toml"
    committed.write_text("[tool.contextplane.integration]\nworkers = 0\n", encoding="utf-8")
    with pytest.raises(QualificationError, match="positive integer"):
        committed_worker_count(pyproject=committed)


# --- the child's side of the control -------------------------------------------


def test_an_authorized_child_takes_its_worker_count_from_the_control() -> None:
    assert resolve_worker_count({"worker_count": 8}, requested=None) == 8


def test_an_authorized_child_refuses_a_worker_count_from_argv() -> None:
    """Ignoring it would let one configuration run while evidence claimed another."""
    with pytest.raises(ControlRejected, match="cannot be given to an authorized child"):
        resolve_worker_count({"worker_count": 8}, requested=2)


def test_a_control_binding_an_unusable_worker_count_is_refused() -> None:
    with pytest.raises(ControlRejected, match="unusable worker count"):
        resolve_worker_count({"worker_count": 0}, requested=None)


def test_an_unsealed_invocation_needs_no_control() -> None:
    """A developer running the suite by hand has no controller and needs none."""
    assert authorize({}) is None


def test_a_control_with_no_broker_to_authenticate_it_is_refused(tmp_path: Path) -> None:
    """Half-configured fails closed rather than downgrading to unauthorized."""
    with pytest.raises(ControlRejected, match="is not authorization"):
        authorize({CONTROL_ENVIRONMENT_VARIABLE: str(tmp_path / "control.json")})


def test_a_broker_with_no_control_to_present_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ControlRejected, match="is not authorization"):
        authorize({BROKER_ENDPOINT_VARIABLE: str(tmp_path / "broker.sock")})
