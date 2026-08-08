"""The coverage ratchet fails a run that is below the floor.

Not "coverage is currently 80-something" -- that assertion passes today and says
nothing about whether the gate would catch tomorrow's regression. This file
asserts the boundary itself, by running a real sub-floor measurement and
checking the exit code.

**The bug this exists to prevent, stated plainly.** Coverage rounds the total to
the configured precision before comparing it against `--cov-fail-under`, while
the failure banner prints the unrounded value. At the default precision of 0, a
run at 79.90% against a floor of 80 printed

    FAIL Required test coverage of 80% not reached. Total coverage: 79.90%

and then exited 0. Every regression landing in [79.50, 80.00) passed the ratchet
while announcing that it had not -- and a red banner on a green run teaches
everyone reading the log that the red text is noise.

**Why the probe is generated rather than pointed at the real tree.** A test that
measured this repository's own coverage would assert the number, which drifts by
design, and would say nothing about the comparison. The probe is a package with
a known statement count and a known uncovered fraction, so the total is chosen,
not observed -- which is the only way to sit a measurement exactly inside the
window where the old behaviour passed.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - runs this repo's own interpreter with a fixed argv; no caller input
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The floor the probe runs are measured against. Deliberately not read from the
#: Makefile: this file tests the comparison, and pinning the probe to the real
#: floor would make these tests move whenever the ratchet is raised.
_PROBE_FLOOR = 80

#: Statements in the generated probe, and how many go uncovered. 201 of 1000 is
#: 79.90% -- inside the window the old behaviour let through, and far enough
#: from the boundary that no rounding mode reaches the floor honestly.
_PROBE_STATEMENTS = 1000
_PROBE_UNCOVERED = 201


def _configured_precision() -> int:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    report = config.get("tool", {}).get("coverage", {}).get("report", {})
    return int(report.get("precision", 0))


def _write_probe(directory: Path, *, precision: int, uncovered: int = _PROBE_UNCOVERED) -> None:
    """A package whose coverage is a chosen number rather than a measured one.

    Everything at module level is executed by the import; the function is never
    called, so its body is exactly the uncovered remainder.
    """
    covered = _PROBE_STATEMENTS - uncovered - 1  # the `def` line is covered too
    body = "\n".join(f"    y{index} = {index}" for index in range(uncovered))
    (directory / "probe.py").write_text(
        "\n".join(f"x{index} = {index}" for index in range(covered)) + f"\ndef never_called():\n{body}\n"
    )
    (directory / "test_probe.py").write_text("import probe\n\n\ndef test_touch() -> None:\n    assert probe.x0 == 0\n")
    (directory / "coveragerc").write_text(f"[run]\n\n[report]\nprecision = {precision}\n")


def _run_probe(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_probe.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "--cov=probe",
            "--cov-report=term",
            f"--cov-config={directory / 'coveragerc'}",
            f"--cov-fail-under={_PROBE_FLOOR}",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )


# --- The setting is present ---------------------------------------------------


def test_the_repository_configures_a_precision_that_can_see_a_small_drop() -> None:
    """Zero precision is the broken configuration, and it is also the default --
    so the absence of this setting is the bug, not a neutral state."""
    assert _configured_precision() >= 2, (
        "coverage precision must be at least 2, or the ratchet rounds a sub-floor total up to the floor "
        "and passes a run it has just printed a failure for"
    )


# --- The gate actually gates --------------------------------------------------


def test_a_run_below_the_floor_exits_non_zero(tmp_path: Path) -> None:
    """The assertion the whole task exists for.

    79.90% against a floor of 80 must fail. Under the old configuration this
    exited 0.
    """
    _write_probe(tmp_path, precision=_configured_precision())

    result = _run_probe(tmp_path)

    assert result.returncode != 0, (
        "a measurably sub-floor run passed the coverage gate:\n" + result.stdout + result.stderr
    )
    assert "not reached" in result.stdout


def test_the_precision_setting_is_what_makes_it_fail(tmp_path: Path) -> None:
    """The witness for the test above.

    Re-runs the identical probe at precision 0 and asserts it passes. Without
    this, a green result from the previous test could come from anything --
    including a probe that was never really below the floor -- and the fix would
    be unfalsifiable.
    """
    _write_probe(tmp_path, precision=0)

    result = _run_probe(tmp_path)

    assert result.returncode == 0, (
        "the old configuration was expected to let this through; if it now fails, this file's "
        "explanation of the bug is wrong and should be rewritten rather than deleted"
    )
    assert "not reached" in result.stdout, "and it printed a failure while passing, which was the whole problem"


def test_the_gate_compares_what_it_prints(tmp_path: Path) -> None:
    """The property, rather than one instance of it.

    Under the fix the per-file total and the pass/fail line carry the same
    number. They disagreed before, which is why neither could be trusted.
    """
    _write_probe(tmp_path, precision=_configured_precision())

    result = _run_probe(tmp_path)

    assert "79.90%" in result.stdout, "the printed total must carry the decimals the comparison uses"
    assert "Total coverage: 79.90%" in result.stdout


@pytest.mark.parametrize("uncovered", [201, 205, 209])
def test_every_total_in_the_old_blind_window_now_fails(tmp_path: Path, uncovered: int) -> None:
    """The window was [79.50, 80.00), not a single point.

    A fix verified at one value inside it would leave the rest of the range
    untested, and the range is the defect. 201, 205 and 209 uncovered of 1000
    are 79.90%, 79.50% and 79.10% -- the top of the window, its floor, and one
    value the old behaviour rejected on its own, so a mistake that made every
    run fail would not read as success here either.
    """
    _write_probe(tmp_path, precision=_configured_precision(), uncovered=uncovered)

    result = _run_probe(tmp_path)

    assert result.returncode != 0, f"{uncovered} uncovered of {_PROBE_STATEMENTS} passed the gate"
