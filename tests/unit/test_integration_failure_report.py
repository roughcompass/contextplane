"""Naming what failed, and not naming anything when nothing did.

These exist because the thing they test was absent for the runner's whole life
and its absence was invisible: a green run and a run that reported `{'failed': 1}`
produced logs that differed by one dictionary. The tests that matter here are the
ones proving a failing run says which node, and that a passing run stays quiet.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from integration_failure_report import (  # noqa: E402
    DISCLOSED_WITHOUT_FAILING,
    MAX_FORWARDED_CHARACTERS,
    failure_section,
    report_outcomes,
    unsuccessful_lines,
    worker_output,
)

_PASSING_STDOUT = "collected 300 items\n" + "." * 300 + "\n300 passed in 12.3s\n"
_FAILING_STDOUT = (
    "collected 300 items\n" + "." * 299 + "F\n"
    "=================================== FAILURES ===================================\n"
    "____________________ test_the_receipt_records_what_was_served __________________\n"
    "E       assert 0.62 >= 0.70\n"
    "=========================== short test summary info ============================\n"
)


class TestTheFailureSection:
    def test_a_passing_worker_forwards_nothing(self) -> None:
        """Otherwise every green run pastes thousands of progress dots into the
        log, and the thing this was added to surface is buried by it."""
        assert failure_section(_PASSING_STDOUT) == ""

    def test_a_failing_worker_forwards_the_assertion(self) -> None:
        section = failure_section(_FAILING_STDOUT)
        assert "assert 0.62 >= 0.70" in section
        assert "test_the_receipt_records_what_was_served" in section

    def test_the_progress_dots_are_not_forwarded_with_it(self) -> None:
        """Sliced from the summary header, not from the top of the output."""
        assert "." * 50 not in failure_section(_FAILING_STDOUT)

    def test_a_worker_that_died_in_setup_is_reported_as_fully_as_one_that_failed(self) -> None:
        """Errors and failures print under different headers, and a run that only
        matched the first would go silent on exactly the collection-time breakage
        that is hardest to diagnose."""
        errored = (
            "==================================== ERRORS ====================================\nE   fixture blew up\n"
        )
        assert "fixture blew up" in failure_section(errored)

    def test_the_forwarded_section_is_bounded(self) -> None:
        """A worker that failed a hundred nodes should make the log longer, not
        turn it into the whole suite."""
        enormous = _FAILING_STDOUT + "x" * (MAX_FORWARDED_CHARACTERS * 3)
        assert len(failure_section(enormous)) <= MAX_FORWARDED_CHARACTERS


class TestWorkerOutput:
    def test_stderr_still_goes_through(self) -> None:
        """The half that always worked. A change that surfaced stdout and lost
        stderr would trade one blind spot for another."""
        assert list(worker_output([("a warning\n", _PASSING_STDOUT)])) == ["a warning\n"]

    def test_both_halves_are_forwarded_for_a_failing_worker(self) -> None:
        lines = list(worker_output([("a warning\n", _FAILING_STDOUT)]))
        assert "a warning\n" in lines
        assert any("assert 0.62" in line for line in lines)

    def test_a_clean_worker_contributes_nothing(self) -> None:
        assert list(worker_output([("   \n", _PASSING_STDOUT)])) == []


class TestReportOutcomes:
    def test_a_clean_run_exits_zero_and_names_nobody(self, capsys: Any) -> None:
        code = report_outcomes({"tests/integration/test_a.py::one": "passed"})
        captured = capsys.readouterr()
        assert code == 0
        assert "1 nodes reconciled" in captured.out
        assert captured.err == ""

    def test_a_skip_is_disclosed_rather_than_unsuccessful(self, capsys: Any) -> None:
        """Modules opt out on an absent credential. Counting that as a failure
        would leave this target unable to exit zero anywhere, CI included."""
        assert report_outcomes({"tests/integration/test_a.py::one": "skipped"}) == 0
        assert capsys.readouterr().err == ""
        assert DISCLOSED_WITHOUT_FAILING == frozenset({"passed", "skipped"})

    def test_a_failing_run_names_the_node(self, capsys: Any) -> None:
        """The whole point. `{'failed': 1}` out of 2587 left a reader with no way
        to find the test, and pytest's own summary never reached the log."""
        code = report_outcomes(
            {
                "tests/integration/test_a.py::one": "passed",
                "tests/integration/test_b.py::two": "failed",
            }
        )
        captured = capsys.readouterr()
        assert code == 1
        assert "FAILED tests/integration/test_b.py::two" in captured.err
        assert "test_a.py" not in captured.err

    def test_a_failing_run_prints_a_command_that_reproduces_it(self, capsys: Any) -> None:
        """A node id and a tier that needs a database are two facts, and a reader
        who has to look the second one up runs the whole tier instead."""
        report_outcomes({"tests/integration/test_b.py::two": "failed"})
        err = capsys.readouterr().err
        assert "CONTEXTPLANE_TEST_PG=testcontainers" in err
        assert "tests/integration/test_b.py::two" in err

    def test_every_failing_node_is_named_and_not_only_the_first(self, capsys: Any) -> None:
        report_outcomes({f"tests/integration/test_{i}.py::t": "failed" for i in range(3)})
        err = capsys.readouterr().err
        assert all(f"test_{i}.py" in err for i in range(3))

    def test_an_error_outcome_is_unsuccessful_too(self, capsys: Any) -> None:
        """`error` and `failed` are different pytest outcomes and both mean the
        run did not succeed; a check written against one label would pass a suite
        whose fixtures all blew up."""
        assert report_outcomes({"tests/integration/test_b.py::two": "error"}) == 1
        assert "ERROR tests/integration/test_b.py::two" in capsys.readouterr().err

    def test_the_summary_counts_every_outcome(self, capsys: Any) -> None:
        report_outcomes(
            {"a::1": "passed", "b::2": "passed", "c::3": "skipped", "d::4": "failed"},
        )
        assert "4 nodes reconciled" in capsys.readouterr().out


def test_nothing_is_printed_for_an_empty_unsuccessful_set() -> None:
    """The guard that keeps a green run's log unchanged from what it was."""
    assert unsuccessful_lines({}) == []
