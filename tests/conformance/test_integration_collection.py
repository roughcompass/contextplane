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

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from integration_control import (  # noqa: E402 - resolved via the sys.path line above
    BROKER_ENDPOINT_VARIABLE,
    CONTROL_ENVIRONMENT_VARIABLE,
    Broker,
    BrokerServer,
    issue,
    new_sequence_secret,
)
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

# The fixture-repository builder is shared with the unsealed recipe-boundary
# controls rather than restated here. Two builders would drift, and the point
# of both sets is that they run the *same* recipe against the same synthetic
# tree, differing only in whether the run is sealed.
from tests.unit.test_integration_runner import (  # noqa: E402 - resolved via the sys.path line above
    build_fixture_repository,
    make_test_integration,
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


# --------------------------------------------------------------------------
# The sealed path, at the recipe boundary
# --------------------------------------------------------------------------
#
# Three recipe-boundary controls already exist beside the runner's unit tests.
# Every one of them runs *unsealed*, and the campaign runs sealed, so the path
# that actually carries a measured run had no end-to-end control in either
# direction. The cases below are that pair.
#
# They live in the conformance tier rather than beside the unsealed three
# because they reach a real database: the unit tier is pure Python and carries
# no provider, and a control that has to be skipped there is not a control.
#
# The trap these were written against is worth stating, because a careless
# version of this file would look more rigorous than the unsealed three while
# proving less. A fixture suite of trivial tests that never open a connection
# can be sealed, brokered and green while proving only that the marker
# plumbing works -- a wrong URL, an unreachable server, or a silently dropped
# variable would all still pass. So the fixture suite below *consumes* its
# assignment: it requests `pg_container`, asserts the URL it received is the
# one the broker assigned, connects to it, and checks the server agrees it is
# that database and that the schema is the migrated one.

_ASSIGNED_URL = "CONTEXTPLANE_TEST_DATABASE_URL"
_SEALED_MARKER = "CONTEXTPLANE_INTEGRATION_SEALED_RUN"

#: Mirrors the shipped `tests/integration/conftest.py` fixture, and imports the
#: real `runner_worker_assignment` rather than restating it -- a restatement
#: would keep passing after the shipped one changed, which is the defect this
#: whole file is about. The unsealed branch yields a sentinel instead of
#: provisioning, so "the fallback was reached" is observable from the parent
#: without standing up a second server inside a control.
SEALED_CONFTEST = """
import os
import pathlib

import pytest

from integration_assignment import BrokerHandoffError, runner_worker_assignment

RECEIPT = pathlib.Path(__file__).resolve().parent / "receipt.txt"


@pytest.fixture(scope="session")
def pg_container():
    try:
        assignment = runner_worker_assignment(os.environ)
    except BrokerHandoffError as refused:
        # Recorded so the parent can assert on the exception *class* rather
        # than on a non-zero exit. A red for the wrong reason looks like
        # evidence and is not.
        RECEIPT.write_text("raised=%s|%s" % (type(refused).__name__, refused))
        raise
    if assignment is None:
        RECEIPT.write_text("unsealed-provider-path")
        yield "unsealed-provider-path"
        return
    yield assignment.database_url
"""

#: The database-touching half. Everything here is an assertion about the
#: assignment, not about pytest: that the fixture handed over the assigned URL,
#: that the server behind it agrees it is that database, and that it carries the
#: migrated schema rather than being an empty database that merely accepts
#: connections.
ASSIGNED_SUITE = {
    "test_assignment.py": """
import asyncio
import os
import pathlib

RECEIPT = pathlib.Path(__file__).resolve().parent / "receipt.txt"


def test_the_worker_uses_the_database_it_was_assigned(pg_container):
    assigned = os.environ.get("CONTEXTPLANE_TEST_DATABASE_URL")
    assert pg_container == assigned

    import asyncpg

    async def interrogate():
        connection = await asyncpg.connect(assigned)
        try:
            return (
                await connection.fetchval("select current_database()"),
                await connection.fetchval(
                    "select count(*) from information_schema.tables where table_schema = 'public'"
                ),
            )
        finally:
            await connection.close()

    database, tables = asyncio.run(interrogate())
    assert database == assigned.rsplit("/", 1)[-1]
    assert tables > 0
    RECEIPT.write_text(
        "consumed=%s|tables=%d|sealed=%s" % (database, tables, os.environ.get("CONTEXTPLANE_INTEGRATION_SEALED_RUN"))
    )
""",
    "test_trivial.py": "def test_a_second_node_exists() -> None:\n    assert True\n",
}


def _provider() -> str:
    """The provider the ambient environment selects, defaulting as the runner does."""
    return os.environ.get("CONTEXTPLANE_TEST_PG") or "devstack"  # config: intentional - selects the test server


def _bound_control(**overrides: object) -> dict[str, object]:
    bound: dict[str, object] = {
        "controller_id": "conformance-controller",
        "lease_id": "conformance-lease",
        "sequence_id": "conformance-sequence",
        "child_sequence": 1,
        "mode": "hard-gate",
        "role": "measured",
        "worker_count": 1,
        "provider": _provider(),
        "expected_commit": "a" * 40,
        "host_digest": "host",
        "schema_fingerprint": "schema",
        "collection_digest": "collection",
        "command_digest": "command",
    }
    bound.update(overrides)
    return bound


@contextmanager
def _sealed_sequence(tmp_path: Path) -> Iterator[dict[str, str]]:
    """A controller's half: one authenticated single-use control, served.

    Yields the environment a measured child is invoked with. `PYTHONPATH`
    reaches the real `scripts/`, which is what lets the fixture tree resolve
    the provider and template modules -- their repository root is computed
    from their own location, so a copy under the fixture tree would look for
    Alembic and the local cluster inside a temporary directory.
    """
    secret = new_sequence_secret()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=_bound_control())
    broker = Broker(secret=secret, consumed_root=tmp_path / "consumed")
    # A Unix socket path is length-limited well below what a pytest tmp_path
    # reaches, and the failure is an unrelated OSError.
    endpoint = Path(tempfile.mkdtemp()) / "broker.sock"
    with BrokerServer(broker=broker, path=endpoint, expectations={"sequence_id": "conformance-sequence"}):
        yield {
            "PYTHONPATH": str(_SCRIPTS),
            "CONTEXTPLANE_TEST_PG": _provider(),
            CONTROL_ENVIRONMENT_VARIABLE: str(control.path),
            BROKER_ENDPOINT_VARIABLE: str(endpoint),
        }


def _receipt(root: Path) -> str:
    path = root / "tests" / "integration" / "receipt.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_a_sealed_target_hands_the_worker_the_database_the_broker_assigned(tmp_path: Path) -> None:
    """The sealed positive control, and the half that was missing entirely.

    Green alone is not the claim. The claim is that a database-touching test
    *ran* and consumed the assignment, which is why the node count and the
    receipt are both asserted: a suite that never ran passes by having nothing
    to fail, and a suite that ran without opening a connection passes while
    proving only that the marker reached the child.
    """
    root = tmp_path / "repo"
    root.mkdir()
    build_fixture_repository(root, suite=ASSIGNED_SUITE, conftest=SEALED_CONFTEST)

    with _sealed_sequence(tmp_path) as environment:
        result = make_test_integration(root, env=environment)

    assert result.returncode == 0, result.stdout + result.stderr
    # The denominator, not just the verdict: a lost node would otherwise leave
    # a shorter suite reporting green.
    assert "collected 2 nodes" in result.stdout
    assert "'passed': 2" in result.stdout

    receipt = _receipt(root)
    assert receipt.startswith("consumed="), receipt
    consumed, tables, sealed = (field.split("=", 1)[1] for field in receipt.split("|"))
    # The worker reached the database it was assigned, and the server agreed.
    assert consumed.startswith("cp_worker_"), consumed
    # A clone of the migrated template rather than an empty database that
    # merely accepts a connection.
    assert int(tables) > 0
    # The marker reached the child, which is the other direction the previous
    # holder recorded as unpinned at this boundary.
    assert sealed == "1"


def test_the_same_sealed_run_goes_red_when_the_assignment_is_withheld(tmp_path: Path) -> None:
    """The discriminator, applied to the control above rather than asserted about.

    Same suite, same recipe, marker still set -- and no controller, so nothing
    provisions and no URL is minted. If the positive above were vacuous, this
    would still pass: a suite that never opens a connection cannot notice that
    it was given no database. It must go red, and it must go red *by name*.

    Asserted on the exception class rather than on the exit status, because
    make reports its own 2 for any failed recipe and a red for the wrong
    reason looks like evidence. The class is the shipped one -- the fixture
    conftest imports `runner_worker_assignment`, so a restatement cannot drift
    away from it.
    """
    root = tmp_path / "repo"
    root.mkdir()
    build_fixture_repository(root, suite=ASSIGNED_SUITE, conftest=SEALED_CONFTEST)

    result = make_test_integration(
        root,
        env={
            "PYTHONPATH": str(_SCRIPTS),
            "CONTEXTPLANE_TEST_PG": _provider(),
            # Set directly rather than by a controller: the runner only ever
            # sets this beside an assignment, so withholding the URL alone is
            # exactly the state the worker must refuse.
            _SEALED_MARKER: "1",
        },
    )

    assert result.returncode != 0, result.stdout + result.stderr
    receipt = _receipt(root)
    assert receipt.startswith("raised=BrokerHandoffError|"), receipt
    assert _ASSIGNED_URL in receipt


def test_an_unsealed_target_still_reaches_the_ordinary_provider_path(tmp_path: Path) -> None:
    """The other direction, which shares one code path with the sealed one.

    Only the sealed half was pinned at this boundary, and in-process at that.
    An unsealed `make test-integration` is what every developer and every other
    lane runs, so a fallback that stopped being reachable would break far more
    than a measured run -- and would do it silently, since the sealed controls
    would stay green.
    """
    root = tmp_path / "repo"
    root.mkdir()
    build_fixture_repository(root, suite=ASSIGNED_SUITE, conftest=SEALED_CONFTEST)

    result = make_test_integration(root, env={"PYTHONPATH": str(_SCRIPTS), "CONTEXTPLANE_TEST_PG": _provider()})

    # The database-touching node fails without an assignment, which is correct
    # and not what is under test here; the claim is that the fixture reached
    # the provider branch rather than raising the sealed refusal.
    assert _receipt(root) == "unsealed-provider-path", result.stdout + result.stderr
