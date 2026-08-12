"""Collection is sealed: the measured set is the committed set, always.

A performance number means nothing unless the run that produced it executed
every integration test and no others. That property is not enforced by the
timing code; it is enforced by the runner refusing to let anything narrow, widen,
or reorder collection, and by the digest that pins what was collected.

This gate exists because the failure is silent in both directions. A selector
that trims collection makes the run faster and the number better, and nothing in
the timing path can tell an honest speedup from a smaller test set. A plugin
re-enabled through an inherited environment variable can add reruns, so a flaky
node passes on its second attempt and the run reports green having measured
something no developer will reproduce.

So the checks here are about what the runner *refuses*, not what it does. Each
one is a channel that has to be closed for the digest to mean anything:

- selectors, markers, keywords, deselections and shard options in argv;
- `PYTEST*` environment channels inherited from whoever invoked Make;
- plugin autoload, which is how rerun and flaky behaviour arrives uninvited;
- trailing pytest arguments, which is the front door for all of the above.

The digest is checked for sensitivity rather than for a pinned value. Pinning a
literal here would fail on every legitimate test addition and teach whoever hits
it to update the constant, which is the opposite of the property wanted: what
matters is that the digest moves when the set moves and holds when it does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from run_integration_tests import (  # noqa: E402 - resolved via the sys.path line above
    QualificationError,
    _parse_args,
    build_child_environment,
    collect,
    collection_command,
    collection_digest,
    forbidden_arguments,
    forbidden_variables,
    parse_collection,
    qualify,
    worker_command,
)

# A minimal, deliberately fake ambient environment. Nothing here is read from
# the real one: the point is an environment with no forbidden channel in it.
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}


# --- nothing may narrow or widen the set --------------------------------------


@pytest.mark.parametrize(
    "argument",
    ["-k", "-m", "--deselect", "--last-failed", "--maxfail", "--stepwise", "--reruns", "--flaky"],
)
def test_a_selector_or_retry_flag_is_refused(argument: str) -> None:
    """Each of these changes the measured set or the number of attempts."""
    assert forbidden_arguments([argument]), f"{argument!r} passed as an ordinary argument"
    with pytest.raises(QualificationError):
        qualify(CLEAN_ENV, [argument])


@pytest.mark.parametrize(
    "argument",
    ["tests/integration/test_something.py", "tests/integration/test_x.py::test_y", "-x"],
)
def test_a_positional_selector_is_refused_by_the_argument_parser(argument: str) -> None:
    """A bare path narrows collection as effectively as `-k` does.

    It is refused one layer below qualification: the runner's parser accepts a
    worker count and nothing else, so an unrecognised argument of any shape
    exits rather than being passed through to pytest. Asserted here through the
    parser rather than through `forbidden_arguments`, which screens flags — a
    test pointed at the wrong layer would report this channel open when it is
    closed, or closed when somebody later adds a passthrough.
    """
    with pytest.raises(SystemExit) as caught:
        _parse_args([argument])
    assert caught.value.code != 0


def test_no_trailing_pytest_arguments_are_accepted() -> None:
    """The front door for every other override; closed rather than filtered."""
    with pytest.raises(QualificationError):
        qualify(CLEAN_ENV, ["--", "-x"])


@pytest.mark.parametrize(
    "variable",
    ["PYTEST", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"],
)
def test_an_inherited_pytest_channel_is_refused(variable: str) -> None:
    """`PYTEST_ADDOPTS` alone can re-add every option refused above."""
    polluted = {**CLEAN_ENV, variable: "-k smoke"}
    assert forbidden_variables(polluted), f"{variable} was not treated as a channel"
    with pytest.raises(QualificationError):
        qualify(polluted, [])


def test_a_clean_invocation_qualifies() -> None:
    """The refusals mean nothing if the permitted path is also closed."""
    qualify(CLEAN_ENV, [])


# --- the child environment is built, not inherited ----------------------------


def test_the_child_environment_drops_inherited_pytest_channels() -> None:
    """Every inherited `PYTEST*` channel is gone; the one that remains was set here.

    `PYTEST_DISABLE_PLUGIN_AUTOLOAD` also starts with `PYTEST`, so a blanket
    prefix assertion would fail on the very variable that closes the plugin
    channel. It is excluded by name rather than by prefix for that reason.
    """
    built = build_child_environment({**CLEAN_ENV, "PYTEST_ADDOPTS": "-k smoke", "PYTEST_PLUGINS": "x"})
    inherited = [key for key in built if key.startswith("PYTEST") and key != "PYTEST_DISABLE_PLUGIN_AUTOLOAD"]
    assert not inherited, inherited


def test_the_child_environment_disables_plugin_autoload() -> None:
    """Autoload is how rerun and flaky plugins arrive without being asked for."""
    built = build_child_environment(dict(CLEAN_ENV))
    assert built.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"


# --- collection is dynamic, whole-directory, and via sys.executable -----------


def test_collection_runs_the_current_interpreter_as_a_module() -> None:
    """`sys.executable -m pytest` pins which interpreter measured the run."""
    command = collection_command()
    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "pytest"]


def test_collection_names_the_integration_directory_and_no_single_test() -> None:
    command = collection_command()
    assert any(part.endswith("tests/integration") or part == "tests/integration" for part in command), command
    assert not any("::" in str(part) for part in command), command


def test_a_worker_command_carries_only_the_nodes_it_was_assigned() -> None:
    """No selector reaches a worker beyond the node IDs it was handed.

    `-m` appears in this command as `python -m pytest` and is not the marker
    flag, so the screen is applied to the pytest arguments after that prefix
    rather than to every dash-led token.
    """
    command = worker_command(["tests/integration/test_a.py::test_one"])
    assert command[1:3] == ["-m", "pytest"]
    assert "tests/integration/test_a.py::test_one" in command
    assert not forbidden_arguments(command[3:])


# --- the digest pins the set, and moves only with it --------------------------


def test_the_digest_is_stable_for_the_same_set() -> None:
    nodes = ["tests/integration/test_a.py::test_one", "tests/integration/test_b.py::test_two"]
    assert collection_digest(nodes) == collection_digest(list(nodes))


def test_the_digest_is_order_independent() -> None:
    """Two workers collecting the same set in different orders agree."""
    first = ["tests/integration/test_a.py::test_one", "tests/integration/test_b.py::test_two"]
    assert collection_digest(first) == collection_digest(list(reversed(first)))


def test_a_removed_node_changes_the_digest() -> None:
    """The whole point: a trimmed set cannot present itself as the committed one."""
    full = ["tests/integration/test_a.py::test_one", "tests/integration/test_b.py::test_two"]
    assert collection_digest(full) != collection_digest(full[:1])


def test_an_added_node_changes_the_digest() -> None:
    full = ["tests/integration/test_a.py::test_one"]
    assert collection_digest(full) != collection_digest([*full, "tests/integration/test_c.py::test_three"])


def test_an_empty_collection_is_refused_where_a_real_run_meets_it(tmp_path: Path) -> None:
    """A zero-test run exits 0 and satisfies every outcome assertion made about it.

    The guard is in `collect()`, not in the digest helper: a digest of the empty
    set is a well-defined value, and refusing it there would not stop a run that
    never called it. Exercised against a real empty integration root so the
    check cannot pass on a mocked collector that was never wired.
    """
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    with pytest.raises(QualificationError):
        collect(CLEAN_ENV, cwd=tmp_path)


# --- parsing refuses a partial or malformed report ----------------------------


def test_parsing_ignores_pytest_chatter_and_keeps_node_ids() -> None:
    stdout = (
        "============ test session starts ============\n"
        "tests/integration/test_a.py::test_one\n"
        "tests/integration/test_b.py::test_two\n"
        "============ 2 tests collected ============\n"
    )
    assert parse_collection(stdout) == (
        "tests/integration/test_a.py::test_one",
        "tests/integration/test_b.py::test_two",
    )


def test_parsing_an_empty_report_yields_nothing_rather_than_guessing() -> None:
    assert parse_collection("") == ()
