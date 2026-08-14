"""The native-provider target exists, is phony, and does something.

A Make target is easy to half-create. A name declared in `.PHONY` with no rule
behind it, or a rule with an empty recipe, both succeed instantly and print
nothing — and a caller reading the exit code cannot tell that from a contract
that ran and passed. Another repository's CI is going to invoke this target
without owning the file that defines it, so the target's existence and its
having actual work in it are properties worth pinning rather than assuming.

The recipe is checked for *what it names*, not for an exact string. Pinning the
literal command would fail on a whitespace change, which teaches the next
reader to loosen the assertion rather than to look at what broke.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.run_native_provider_contract import CONTRACT_PATH, ContractFailure, pytest_command, run

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPOSITORY_ROOT / "Makefile"
TARGET = "test-native-provider"


@pytest.fixture(scope="module")
def makefile() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def recipe_lines(makefile: str, target: str) -> list[str]:
    """The tab-indented body of a target, which is what Make will run.

    Make identifies a recipe by the leading tab specifically. Reading the block
    that way rather than by indentation-in-general is what keeps a comment or a
    continued prerequisite line from being mistaken for work.
    """
    lines = makefile.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"^{re.escape(target)}\s*:", line):
            body: list[str] = []
            for candidate in lines[index + 1 :]:
                if candidate.startswith("\t"):
                    body.append(candidate.lstrip("\t").strip())
                elif candidate.strip() == "":
                    continue
                else:
                    break
            return body
    return []


def test_the_target_is_declared_phony(makefile: str) -> None:
    """Without this, a file that happened to be named `test-native-provider`
    would make Make consider the target up to date and skip it entirely."""
    phony = re.search(r"^\.PHONY:(.*?)(?=^\S)", makefile, re.M | re.S)

    assert phony is not None
    assert TARGET in phony.group(1).split()


def test_the_target_exists(makefile: str) -> None:
    assert re.search(rf"^{re.escape(TARGET)}\s*:", makefile, re.M)


def test_the_target_has_a_non_empty_recipe(makefile: str) -> None:
    """A phony target with no recipe exits 0 without doing anything, which is
    the shape a caller cannot distinguish from a contract that passed."""
    assert recipe_lines(makefile, TARGET)


def test_the_recipe_names_the_focused_provider_contract(makefile: str) -> None:
    body = " ".join(recipe_lines(makefile, TARGET))

    assert "run_native_provider_contract.py" in body


def test_the_recipe_runs_through_the_known_interpreter(makefile: str) -> None:
    """Never a bare `python` off PATH, which is whatever the caller installed."""
    body = " ".join(recipe_lines(makefile, TARGET))

    assert "$(PYTHON)" in body


def test_the_contract_the_runner_names_is_a_file_that_exists() -> None:
    """The target reports this file's name; it had better be running it."""
    assert (REPOSITORY_ROOT / CONTRACT_PATH).is_file()


def test_the_runner_invokes_pytest_through_this_interpreter(tmp_path: Path) -> None:
    command = pytest_command(tmp_path / "report.xml")

    assert command[1:3] == ["-m", "pytest"]
    assert CONTRACT_PATH in command


def test_the_runner_asks_for_a_structured_report_rather_than_a_summary_line(tmp_path: Path) -> None:
    """ "How many skipped" has to be a number the runner can be sure of, and
    pytest's summary line is prose that changes between versions."""
    command = pytest_command(tmp_path / "report.xml")

    assert any(part.startswith("--junit-xml=") for part in command)


def test_the_runner_takes_no_selector_from_a_caller(tmp_path: Path) -> None:
    """Checked on the arguments pytest receives, not on the whole argv.

    ``-m`` appears twice over with two unrelated meanings: ``python -m pytest``
    selects a module to run, and ``pytest -m`` selects tests by marker. Only
    the second reselects the suite, so the interpreter's own flag is sliced off
    before the check rather than special-cased inside it.
    """
    pytest_arguments = pytest_command(tmp_path / "report.xml")[3:]

    assert not {"-k", "-m", "--deselect", "--last-failed", "--maxfail"} & set(pytest_arguments)


# --------------------------------------------------------------------------
# The gate fires, proved by driving real runs that must be refused
# --------------------------------------------------------------------------
#
# A passing contract proves the happy path and nothing about the refusals. Each
# case below hands the runner a real contract file with the shape it is
# supposed to reject and spends a real pytest process on it, because a refusal
# that was never observed failing is indistinguishable from one wired to
# nothing.


def write_contract(tmp_path: Path, body: str) -> str:
    path = tmp_path / "test_temporary_contract.py"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_a_contract_that_collects_nothing_is_refused(tmp_path: Path) -> None:
    """Zero collection exits 0 and looks like a fast, healthy contract. It is
    the failure mode with no symptoms."""
    contract = write_contract(tmp_path, "# no tests here at all\n")

    with pytest.raises(ContractFailure, match="zero tests collected"):
        run(contract=contract)


def test_a_skipping_contract_is_refused(tmp_path: Path) -> None:
    """The case this target exists for. In the full tier this skip is honest;
    here it means the provider could not be exercised, and reporting that as
    green is the outcome the target was created to prevent."""
    contract = write_contract(
        tmp_path,
        "import pytest\n\n\ndef test_provider_unavailable() -> None:\n" "    pytest.skip('no server on this host')\n",
    )

    with pytest.raises(ContractFailure, match="skipped"):
        run(contract=contract)


def test_a_failing_contract_is_refused(tmp_path: Path) -> None:
    contract = write_contract(tmp_path, "def test_broken() -> None:\n    assert False\n")

    with pytest.raises(ContractFailure, match="failed"):
        run(contract=contract)


def test_a_cleanup_failure_in_teardown_is_refused(tmp_path: Path) -> None:
    """The test itself passes; the fixture fails on the way out.

    This contract creates and drops real databases, so a teardown that failed
    while every assertion passed is the shape that leaks servers and poisons
    every later run on the host — and it is exactly the shape a pass/fail count
    taken from the test alone would call green.
    """
    contract = write_contract(
        tmp_path,
        "import pytest\n\n\n@pytest.fixture\ndef leaky():\n    yield 1\n"
        "    raise RuntimeError('could not drop the database')\n\n\n"
        "def test_passes_then_leaks(leaky: int) -> None:\n    assert leaky == 1\n",
    )

    with pytest.raises(ContractFailure, match="errored"):
        run(contract=contract)


def test_a_clean_contract_is_not_refused(tmp_path: Path) -> None:
    """The negative control. Without it, a runner that refused everything
    would satisfy all four cases above."""
    contract = write_contract(tmp_path, "def test_passes() -> None:\n    assert True\n")

    outcomes = run(contract=contract)

    assert (outcomes.passed, outcomes.skipped) == (1, 0)
