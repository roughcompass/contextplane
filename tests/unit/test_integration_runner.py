"""Qualification, proved by running the real script in a real subprocess.

An in-process call to `qualify()` proves the function's logic and nothing
about the boundary that matters: whether an environment variable exported by
a caller actually reaches the runner and is actually refused. Several cases
here therefore spend a process, because the thing under test is the process
boundary itself. A synthetic assertion over a manifest would pass whether or
not the script rejects anything.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess  # noqa: S404 - these cases exist to cross a real process boundary
import sys
import tempfile
import time
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
    _CHILD_ALLOWLIST,
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
from tests.integration.conftest import BrokerHandoffError, runner_worker_assignment

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


@pytest.mark.parametrize("variable", ["MAKEFLAGS", "MFLAGS"])
def test_the_two_channels_make_always_exports_are_ignored_when_empty(variable: str) -> None:
    """Make sets both on every recipe, empty when nobody asked for anything.

    Refusing them on presence would make a Makefile-invoked runner refuse
    every invocation -- a target that can never run, which fails as
    uselessly as one that can never fail.
    """
    assert forbidden_variables({variable: ""}) == ()


@pytest.mark.parametrize("variable", ["MAKEFLAGS", "MFLAGS"])
def test_those_same_channels_are_refused_the_moment_they_carry_anything(variable: str) -> None:
    """The narrowing asks whether there is a message, never what it says.

    Reading the contents to decide whether they look harmless is the
    inference the presence rule exists to forbid; emptiness is not that
    question.
    """
    assert forbidden_variables({variable: "PYTHON=true"}) == (variable,)
    assert forbidden_variables({variable: " --jobserver-fds=3,4 -j"}) == (variable,)


@pytest.mark.parametrize("variable", ["GNUMAKEFLAGS", "MAKEOVERRIDES", "MAKEFILES"])
def test_the_channels_make_does_not_always_export_stay_refused_when_empty(variable: str) -> None:
    """Make does not set these unprompted, so their presence is the caller's
    doing whatever the value is.

    `MAKEFILES` is the one that must never be narrowed: it preloads a makefile
    that can redefine the interpreter without any of these names ever carrying
    the evidence, and it is the only channel here with no second detector.
    """
    assert forbidden_variables({variable: ""}) == (variable,)


def test_an_environment_override_survives_the_narrowing_by_a_different_detector() -> None:
    """The one row the narrowing gives up ground on, checked rather than assumed.

    `PYTHON=true make` leaves `MAKEFLAGS` empty, so the make-channel rule no
    longer catches it. The override is caught anyway because the overridden
    name is itself forbidden -- and a command-line override is caught three
    ways, since make sets `MAKEOVERRIDES` and fills `MAKEFLAGS` as well.
    """
    assert forbidden_variables({"MAKEFLAGS": "", "PYTHON": "true"}) == ("PYTHON",)
    assert forbidden_variables({"MAKEFLAGS": "PYTHON=true", "MAKEOVERRIDES": "x", "PYTHON": "true"}) == (
        "MAKEFLAGS",
        "MAKEOVERRIDES",
        "PYTHON",
    )


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
    """The negative control: without it, a script that refused everything would
    pass every case above.

    It watches for the runner getting *past* qualification rather than for the
    run finishing. Since the runner began executing what it collects, a clean
    invocation launches the whole integration tier, so waiting for exit would
    make this case a full-suite run wearing a unit test's name -- which is how
    it started timing out. Reaching the collection line is the affirmative
    signal that qualification passed, and it is stronger than the old
    absence-of-a-string assertion.
    """
    progress = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    with progress:
        pass
    log = Path(progress.name)
    process = subprocess.Popen(
        [sys.executable, str(RUNNER)],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            # Its stdout is a file here, so Python would block-buffer the
            # progress line and this poll would never see a line the runner had
            # already written.
            "PYTHONUNBUFFERED": "1",
        },
        stdout=log.open("w"),
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            if "collected" in log.read_text(encoding="utf-8"):
                break
            if process.poll() is not None:
                break
            time.sleep(0.25)
        printed = log.read_text(encoding="utf-8")
    finally:
        # The group, not the process: the runner has pytest children by now, and
        # leaving them behind would hold the provider for the next test.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait(timeout=30)
        log.unlink(missing_ok=True)

    assert "collected" in printed, f"runner never reached collection; printed {printed!r}"
    assert "refusing to run" not in printed


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


# --------------------------------------------------------------------------
# Broker manifest URL handoff, from the worker's side
# --------------------------------------------------------------------------
#
# The dangerous case here is the *absent* variable, not the malformed one. A
# worker that fell through to provisioning would produce a green run measuring
# a topology nobody chose, and its timing would look like every other worker's.
# So the tests below are mostly about what the worker refuses to do.


def test_an_ordinary_invocation_is_not_a_runner_worker() -> None:
    """A developer running the suite directly still picks its own database."""
    assert runner_worker_assignment({"CONTEXTPLANE_TEST_PG": "devstack"}) is None


def test_a_dispatched_worker_consumes_the_url_it_was_assigned() -> None:
    assignment = runner_worker_assignment(
        {
            "CONTEXTPLANE_INTEGRATION_WORKER_ID": "w0",
            "CONTEXTPLANE_TEST_DATABASE_URL": "postgresql+asyncpg://h/db",
            "CONTEXTPLANE_INTEGRATION_BROKER_MANIFEST_DIGEST": "a" * 64,
        }
    )

    assert assignment is not None
    assert (assignment.worker_id, assignment.database_url) == ("w0", "postgresql+asyncpg://h/db")


@pytest.mark.parametrize(
    "missing",
    ["CONTEXTPLANE_TEST_DATABASE_URL", "CONTEXTPLANE_INTEGRATION_BROKER_MANIFEST_DIGEST"],
)
def test_a_worker_dispatched_without_its_assignment_refuses_rather_than_provisioning(missing: str) -> None:
    """There is deliberately no fallback. Standing up a second server inside a
    measured run is the failure this whole handoff exists to prevent, and it is
    the one that leaves no trace in the timing."""
    environment = {
        "CONTEXTPLANE_INTEGRATION_WORKER_ID": "w0",
        "CONTEXTPLANE_TEST_DATABASE_URL": "postgresql+asyncpg://h/db",
        "CONTEXTPLANE_INTEGRATION_BROKER_MANIFEST_DIGEST": "a" * 64,
    }
    del environment[missing]

    with pytest.raises(BrokerHandoffError, match=missing):
        runner_worker_assignment(environment)


def test_a_worker_missing_both_names_both_of_them() -> None:
    """One error naming everything absent, rather than one round trip per
    variable through a 45-minute sequence."""
    with pytest.raises(BrokerHandoffError) as raised:
        runner_worker_assignment({"CONTEXTPLANE_INTEGRATION_WORKER_ID": "w0"})

    assert "CONTEXTPLANE_TEST_DATABASE_URL" in str(raised.value)
    assert "CONTEXTPLANE_INTEGRATION_BROKER_MANIFEST_DIGEST" in str(raised.value)


def test_an_empty_assignment_counts_as_absent() -> None:
    """An exported-but-empty variable is the shape a shell produces when the
    value it meant to pass was itself unset."""
    with pytest.raises(BrokerHandoffError):
        runner_worker_assignment(
            {
                "CONTEXTPLANE_INTEGRATION_WORKER_ID": "w0",
                "CONTEXTPLANE_TEST_DATABASE_URL": "",
                "CONTEXTPLANE_INTEGRATION_BROKER_MANIFEST_DIGEST": "a" * 64,
            }
        )


def test_the_digest_channel_reaches_the_worker() -> None:
    """The handoff is unusable if the runner drops the variable on the way in;
    the allowlist is built up rather than filtered, so absence is the default."""
    assert "CONTEXTPLANE_INTEGRATION_BROKER_MANIFEST_DIGEST" in _CHILD_ALLOWLIST


# --------------------------------------------------------------------------
# The two negative controls that justify wiring the canonical target
# --------------------------------------------------------------------------
#
# The defect this runner replaced collected 2253 nodes and returned 0 without
# executing any of them, and it survived because every check on it was a green
# run. A green run certifies nothing here. So before `make test-integration`
# routes through this runner, both directions are demonstrated: a real failure
# must come back red, and a node that never reported must come back red as
# *invalid* rather than being quietly dropped from the aggregation.


def test_a_real_test_failure_comes_back_red(tmp_path: Path) -> None:
    """First control: the suite genuinely failing must fail the runner.

    Asserted on values that cannot exist unless pytest executed the node — a
    FAILED outcome and a worker that exited nonzero. A collect-only runner
    produces neither.
    """
    _, nodes = _suite(tmp_path)
    schedule = balance(nodes, workers=2, history=_history(nodes))

    outcomes, results = dispatch(schedule, {"PATH": os.environ["PATH"]}, events_root=tmp_path / "events", cwd=tmp_path)

    unsuccessful = {node: outcome for node, outcome in outcomes.items() if outcome is not NodeOutcome.PASSED}
    assert unsuccessful, "a suite containing a failure must leave something unsuccessful"
    # The same mapping main() applies: anything not PASSED is a red run.
    assert (1 if unsuccessful else 0) == 1
    assert any(result.returncode != 0 for result in results)


def test_a_node_that_never_reported_makes_the_run_invalid_rather_than_green(tmp_path: Path) -> None:
    """Second control, and the one the original defect would have passed.

    A node is scheduled that the suite does not contain, so no worker can ever
    disclose an outcome for it. The run must be rejected as invalid — not
    reported as green over the nodes that did report. An aggregation that
    silently narrows to whatever came back is indistinguishable from a healthy
    run of a smaller suite, which is the whole failure mode here.
    """
    _, nodes = _suite(tmp_path)
    phantom = "test_sample_suite.py::test_this_node_does_not_exist"
    scheduled = (*nodes, phantom)
    schedule = balance(scheduled, workers=2, history=_history(scheduled))

    with pytest.raises(RunInvalid, match="never disclosed"):
        dispatch(schedule, {"PATH": os.environ["PATH"]}, events_root=tmp_path / "events", cwd=tmp_path)


def test_a_wholly_passing_suite_comes_back_green(tmp_path: Path) -> None:
    """The positive control the pair needs.

    Without it, a runner that reported every run red would satisfy both cases
    above, and wiring it would replace a gate that never fails with one that
    never passes.
    """
    module = tmp_path / "test_all_green.py"
    module.write_text(
        "def test_one() -> None:\n    assert True\n\n\ndef test_two() -> None:\n    assert True\n", encoding="utf-8"
    )
    nodes = ("test_all_green.py::test_one", "test_all_green.py::test_two")
    schedule = balance(nodes, workers=2, history=_history(nodes))

    outcomes, results = dispatch(schedule, {"PATH": os.environ["PATH"]}, events_root=tmp_path / "events", cwd=tmp_path)

    assert set(outcomes.values()) == {NodeOutcome.PASSED}
    assert all(result.returncode == 0 for result in results)


# --------------------------------------------------------------------------
# Real `make test-integration`: the recipe, not the runner it invokes
# --------------------------------------------------------------------------
#
# Everything above proves the runner. None of it proves the target, and the
# target is what every other gate and every measured child actually calls. A
# recipe that invoked pytest directly would pass every test in this file while
# running an entirely different suite, so the boundary these cases cross is
# the Makefile, not the script.
#
# The recipe is read out of the shipped Makefile rather than restated here. A
# copy would keep passing after somebody changed the real one, which is the
# failure this whole file is about.

MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


#: The recipe the canonical target will carry once a worker can obtain the
#: database it was assigned. Held as a literal rather than read out of the
#: Makefile because the Makefile does not carry it yet -- see the test at the
#: end of this section, which pins that gap so it cannot be forgotten.
RUNNER_RECIPE: list[str] = ["\t$(PYTHON) scripts/run_integration_tests.py"]


def shipped_integration_recipe() -> list[str]:
    """The `test-integration` recipe lines exactly as the Makefile carries them."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("test-integration:"))
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line)
    assert recipe, "the shipped test-integration target has no recipe"
    return recipe


def build_fixture_repository(
    root: Path, *, suite: dict[str, str], conftest: str | None = None, recipe: list[str] | None = None
) -> None:
    """A miniature repository the shipped recipe can be run against.

    The real tier cannot serve as a control: it takes minutes, needs a
    provider, and is not wholly passing on every host, so "green" and "red"
    would both be unreadable. The runner resolves its repository root from its
    own location, so copying it here points the identical script at a suite
    whose outcome is chosen rather than discovered.
    """
    (root / "scripts").mkdir(parents=True)
    for source in [SCRIPTS / "run_integration_tests.py", *SCRIPTS.glob("integration_*.py")]:
        shutil.copy2(source, root / "scripts" / source.name)

    # `pythonpath` is what lets a worker load the reporter by module name, the
    # same way the real repository does.
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["scripts"]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    # Every variable the real Makefile defines for this recipe, so a recipe
    # that referred to a different one would still run rather than expanding to
    # nothing. An undefined variable makes make fail with a shell error, which
    # is a red these cases would happily accept while proving nothing -- the
    # target could be checked against a recipe that never ran at all.
    #
    # `?=` rather than an override on the command line: passing PYTHON= to make
    # is one of the tampering attempts the runner refuses, so setting it that
    # way would test the refusal instead of the recipe.
    preamble = f"PYTHON ?= {sys.executable}\nPYTEST ?= $(PYTHON) -m pytest\nTEST_ROOT := tests\n"
    (root / "Makefile").write_text(
        preamble + "\ntest-integration:\n" + "\n".join(recipe or RUNNER_RECIPE) + "\n",
        encoding="utf-8",
    )

    integration = root / "tests" / "integration"
    integration.mkdir(parents=True)
    if conftest is not None:
        (integration / "conftest.py").write_text(conftest, encoding="utf-8")
    for name, body in suite.items():
        (integration / name).write_text(body, encoding="utf-8")


def make_test_integration(root: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the target the way a developer or a measured child does."""
    return subprocess.run(
        ["make", "test-integration"],
        cwd=str(root),
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


PASSING_SUITE = {
    "test_passing.py": ("def test_one() -> None:\n    assert True\n\n\ndef test_two() -> None:\n    assert True\n"),
}


def test_the_target_is_green_when_every_test_passes(tmp_path: Path) -> None:
    """The positive control, and it is not the easy half of the pair.

    A gate that fails universally satisfies both negative controls below --
    wiring the target to a runner that refuses everything would trade a
    never-fails gate for a never-passes one, which is the same defect with its
    sign flipped. Red is evidence only where green is reachable.
    """
    build_fixture_repository(tmp_path, suite=PASSING_SUITE)

    result = make_test_integration(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 nodes reconciled" in result.stdout
    assert "'passed': 2" in result.stdout


def test_the_target_is_red_when_a_test_fails(tmp_path: Path) -> None:
    """The first negative control: an ordinary failure survives the runner.

    The runner aggregates outcomes out of a private event stream rather than
    pytest's exit status, so "a failing test still fails the target" is a real
    claim about the reconciliation and not a restatement of how pytest works.
    """
    build_fixture_repository(
        tmp_path,
        suite={**PASSING_SUITE, "test_failing.py": "def test_broken() -> None:\n    assert False\n"},
    )

    result = make_test_integration(tmp_path)

    # Non-zero rather than a specific code: make reports its own exit 2 for any
    # failed recipe, so the runner's 1 and its 2 arrive here as one number. The
    # controller judges children the same way, on zero versus non-zero.
    assert result.returncode != 0, result.stdout + result.stderr
    assert "'failed': 1" in result.stdout


# A node the worker never runs and never reports, while exiting cleanly. It is
# deselected only when a worker id is present, so the parent still collects and
# schedules it -- which is the state the reconciler exists to catch.
#
# Killing the worker would also reach a red, and that is exactly why it is the
# wrong induction: the run would die in the process-group teardown without the
# undisclosed-outcome guard ever being consulted. A red for the wrong reason
# looks like evidence and is not.
VANISHING_CONFTEST = """
import os


def pytest_collection_modifyitems(items):
    if not os.environ.get("CONTEXTPLANE_INTEGRATION_WORKER_ID"):
        return
    for index, item in enumerate(items):
        if item.name == "test_vanishes":
            del items[index]
            return
"""


def test_the_target_is_red_when_a_node_never_reports(tmp_path: Path) -> None:
    """The second negative control, and the one that matters most.

    A lost node with no guard behind it does not produce a failure -- it
    produces a *shorter denominator*, a green run over fewer tests than the
    suite contains. That is the defect this runner replaced, so the target
    must go red naming the undisclosed node rather than passing what remains.
    """
    build_fixture_repository(
        tmp_path,
        suite={**PASSING_SUITE, "test_vanishing.py": "def test_vanishes() -> None:\n    assert True\n"},
        conftest=VANISHING_CONFTEST,
    )

    result = make_test_integration(tmp_path)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "run invalid" in result.stderr
    assert "never disclosed" in result.stderr
    assert "test_vanishes" in result.stderr
    # The node was collected and scheduled before it went missing. Without
    # this, a run that lost a node and reported the rest would be
    # indistinguishable from a smaller suite that passed.
    assert "collected 3 nodes" in result.stdout


def test_the_shipped_target_does_not_yet_invoke_the_runner(tmp_path: Path) -> None:
    """The gap between the runner and the target, pinned so it stays visible.

    The three cases above prove the runner behaves correctly behind a real
    recipe. They deliberately do not prove the shipped target uses it, because
    right now it does not: the runner tells a worker its identity but never
    which database it was assigned, and the worker fixture refuses to touch a
    server without one. Wiring it in that state errors every
    database-touching test in the tier -- a gate that can never pass, on the
    target every other lane merges against.

    So this asserts the absence rather than leaving it silent. An absence
    nothing checks is indistinguishable from an oversight, and this one is a
    decision. When parent-side assignment lands, this test fails and is
    deleted in the same commit that wires the recipe -- which is the point:
    the wiring cannot be done without someone reading why it was not done
    before.
    """
    recipe = "\n".join(shipped_integration_recipe())

    assert "run_integration_tests.py" not in recipe
    assert "$(PYTEST)" in recipe

    # And the reason is recorded next to the recipe, not only here. A pinned
    # absence whose justification lives in one file is a step from a pinned
    # absence nobody can justify at all.
    target_comment = MAKEFILE.read_text(encoding="utf-8").split("test-integration:")[0]
    assert "which database it was assigned" in target_comment


# -- parent-side assignment ------------------------------------------------
#
# The parent's half of the broker contract. The worker's half already refuses
# to run without an assigned URL and a manifest digest; these cases prove the
# parent hands it exactly those, spelled the way the child environment admits.


def test_the_assignment_variables_are_the_ones_the_child_allowlist_admits() -> None:
    """The two halves must agree on the names, not merely on the idea.

    This is the guard for a defect that was live until parent-side assignment
    was built: the broker's own `worker_environment()` emits the digest as
    `CONTEXTPLANE_BROKER_MANIFEST_DIGEST`, which is *not* in the runner's child
    allowlist. Nothing ever caught it because the two halves had never run in
    one process -- the parent side was the missing piece.

    The failure it would have produced is worse than a mismatch. The allowlist
    filters rather than raises, so the digest is dropped silently and the
    worker then fails closed reporting a missing digest -- naming the broker
    for a fault that is not in the broker. This asserts membership so a rename
    on either side goes red here instead of in a child.
    """
    from integration_assignment import ASSIGNED_URL_VARIABLE, MANIFEST_DIGEST_VARIABLE
    from run_integration_tests import _CHILD_ALLOWLIST

    assert ASSIGNED_URL_VARIABLE in _CHILD_ALLOWLIST
    assert MANIFEST_DIGEST_VARIABLE in _CHILD_ALLOWLIST


def test_every_variable_the_broker_hands_a_worker_survives_the_child_allowlist() -> None:
    """The broker's own mapping, checked against the filter it has to pass.

    The case above pins the assignment module's two constants. This pins the
    *other* producer: `BrokerManifest.worker_environment()` builds a mapping
    for a worker directly, and its digest key was misspelled for the whole
    time the two halves had never run in one process. Asserting the constants
    would not have caught that, because the broker does not use them.

    Membership rather than equality: the allowlist admits plenty this mapping
    does not set. What must never happen again is a key the filter drops in
    silence, since the worker then fails closed naming the broker for a fault
    that is not in the broker.
    """
    from pg_run_broker import BrokerManifest
    from run_integration_tests import _CHILD_ALLOWLIST

    manifest = BrokerManifest(run_id="run123")
    manifest.assign("w1", "postgresql://localhost/one", "cp_one")

    dropped = set(manifest.worker_environment("w1")) - set(_CHILD_ALLOWLIST)
    assert not dropped, f"the child allowlist would silently drop {sorted(dropped)}"


def test_a_worker_is_told_its_url_and_the_digest_and_nothing_else() -> None:
    from integration_assignment import ASSIGNED_URL_VARIABLE, MANIFEST_DIGEST_VARIABLE, Assignment

    assignment = Assignment(worker_id="gw0", database_url="postgresql://h/db_gw0", database_name="db_gw0")
    environment = assignment.environment("d" * 64)

    assert environment == {
        ASSIGNED_URL_VARIABLE: "postgresql://h/db_gw0",
        MANIFEST_DIGEST_VARIABLE: "d" * 64,
    }


def test_an_unassigned_worker_is_refused_by_name() -> None:
    """Refused at the parent, naming the worker.

    The worker side would fail closed on this anyway, but it would do so from
    inside a child as a missing-variable error, which does not say which
    worker went unassigned.
    """
    from integration_assignment import Assignment, AssignmentError, Assignments

    assignments = Assignments(
        manifest_digest="d" * 64,
        by_worker={"gw0": Assignment(worker_id="gw0", database_url="postgresql://h/a", database_name="a")},
    )

    with pytest.raises(AssignmentError, match="gw1"):
        assignments.environment("gw1")


def test_assignment_evidence_carries_no_url() -> None:
    """Evidence is published; a URL is a credential."""
    import json

    from integration_assignment import Assignment, Assignments

    assignments = Assignments(
        manifest_digest="d" * 64,
        by_worker={
            "gw0": Assignment(
                worker_id="gw0",
                database_url="postgresql://user:secret@host:5545/registry_gw0",
                database_name="registry_gw0",
            )
        },
    )

    evidence = json.dumps(assignments.as_evidence())

    assert "postgresql://" not in evidence
    assert "secret" not in evidence
    assert "registry_gw0" in evidence


@pytest.mark.parametrize(
    ("worker_ids", "expected"),
    [((), "at least one worker"), (("gw0", "gw0"), "unique")],
)
def test_a_sequence_refuses_a_worker_list_it_cannot_assign(worker_ids: tuple[str, ...], expected: str) -> None:
    """Refused before a single database is created.

    A duplicate ID would have two workers sharing one database and reporting
    independent timings for it, which is a measurement of nothing.
    """
    from integration_assignment import AssignmentError, assign_workers

    created: list[str] = []

    class _Broker:
        run_id = "run"

        def database_name(self, kind: str, label: str) -> str:
            return f"{kind}_{label}"

        def clone_database(self, name: str, *, template: str, control: str | None = None) -> str:
            created.append(name)
            return name

    with pytest.raises(AssignmentError, match=expected):
        assign_workers(_Broker(), "postgresql://h/postgres", worker_ids, template="tmpl")  # type: ignore[arg-type]

    assert created == []


def test_each_clone_gets_its_own_control_because_a_control_is_consumed_once() -> None:
    """One control per clone, never one control reused across them.

    `verify_and_consume` consumes a control exactly once, so a single control
    minted for the whole assignment authenticates the first clone and is
    rejected for every clone after it. That failure only appears against a
    real broker, which is why it is pinned here against a fake that records
    what it was handed rather than against a live server.
    """
    from integration_assignment import assign_workers

    handed: list[str | None] = []

    class _Broker:
        run_id = "run"

        def database_name(self, kind: str, label: str) -> str:
            return f"{kind}_{label}"

        def clone_database(self, name: str, *, template: str, control: str | None = None) -> str:
            handed.append(control)
            return name

    assign_workers(
        _Broker(),  # type: ignore[arg-type]
        "postgresql://h/postgres",
        ("gw0", "gw1", "gw2"),
        template="tmpl",
        mint_control=lambda child: f"control-{child}",
    )

    assert handed == ["control-1", "control-2", "control-3"]
    assert len(set(handed)) == len(handed), "a control reused across clones is rejected after the first"
