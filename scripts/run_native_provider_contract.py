#!/usr/bin/env python3
"""Run the focused provider lifecycle contract, where a skip is a failure.

The contract file this runs is allowed to skip when a host genuinely cannot
supply a PostgreSQL server — that is an honest fact about the host rather than
a defect in the broker, and the file says so. This target exists for the
opposite situation: somebody has asked for a specific provider and wants to
know that the provider's whole lifecycle actually works. Under that question a
skip is the worst possible outcome, because it is indistinguishable from a pass
in every summary line and every CI badge, and the thing it silently stopped
checking is the thing the target was run to check.

So this refuses three outcomes the ordinary suite tolerates:

- **Zero collection.** A file that collected nothing exits 0 and looks like a
  fast, healthy contract. It is the failure mode with no symptoms.
- **Any skip.** Including a skip the file itself chose. If the provider cannot
  supply a server, the answer to "does this provider work here" is no, not
  "unknown, reported as green".
- **Any non-pass**, which includes a teardown error. Cleanup failure matters
  here more than almost anywhere else: this contract creates and drops real
  databases, and one that leaks them poisons every later run on the host.

Outcomes are read from pytest's JUnit XML rather than its summary line. The
summary is prose that changes between versions; the XML is a structured record
with a per-case outcome, and "how many skipped" has to be a number this script
can be sure of.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # noqa: S404 - invoking the sealed pytest child is this script's purpose
import sys
import tempfile
import xml.etree.ElementTree as ElementTree  # noqa: S405 - parses only pytest's own report, written by this process into a private temp directory
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parent.parent

#: The focused contract. Named here rather than passed in: a target that could
#: be pointed at a different file would report this file's name while running
#: something else.
CONTRACT_PATH: Final = "tests/integration/test_pg_provider_lifecycle.py"

_REQUIRED_PLUGINS: Final = ("pytest_asyncio.plugin", "pytest_timeout")


class ContractFailure(RuntimeError):
    """The provider lifecycle did not demonstrably pass."""


@dataclass(frozen=True)
class Outcomes:
    """Per-case counts, as the structured report records them."""

    total: int
    failures: int
    errors: int
    skipped: int

    @property
    def passed(self) -> int:
        return self.total - self.failures - self.errors - self.skipped

    def failings(self) -> tuple[str, ...]:
        """Every reason this run does not demonstrate a working provider."""
        reasons: list[str] = []
        if self.total == 0:
            reasons.append(
                "zero tests collected; a contract that collected nothing exits 0 and is "
                "indistinguishable from a fast, healthy one"
            )
        if self.skipped:
            reasons.append(
                f"{self.skipped} test(s) skipped; a skip here means the provider could not be "
                "exercised, which is an answer of 'no' rather than a pass"
            )
        if self.failures:
            reasons.append(f"{self.failures} test(s) failed")
        if self.errors:
            reasons.append(f"{self.errors} test(s) errored, which includes a cleanup failure in teardown")
        return tuple(reasons)


def child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment the pytest child runs under.

    Autoload is off. The plugins this run needs are named explicitly, and with
    autoload left on pytest registers `pytest_asyncio` twice — once from its
    entry point and once from the `-p` flag — and aborts before it writes a
    report. Turning it off is also the property worth having on its own: the
    set of plugins that touched a contract run is the set named here, not
    whatever happens to be installed alongside it.
    """
    child = dict(environ)
    child["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    child["PYTHONHASHSEED"] = "0"
    return child


def pytest_command(report_path: Path, *, contract: str = CONTRACT_PATH) -> list[str]:
    """The exact argv. Through this interpreter, never a bare `pytest`."""
    return [
        sys.executable,
        "-m",
        "pytest",
        contract,
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        *[argument for plugin in _REQUIRED_PLUGINS for argument in ("-p", plugin)],
        f"--junit-xml={report_path}",
    ]


def parse_report(path: Path) -> Outcomes:
    """Read counts from the structured report.

    Absent means pytest never got far enough to write one, which is itself a
    failure to demonstrate anything — not a zero-count success.
    """
    if not path.is_file():
        msg = f"no test report at {path}; pytest did not run far enough to produce one"
        raise ContractFailure(msg)
    root = ElementTree.parse(path).getroot()  # noqa: S314 - pytest's own output, not caller input
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        msg = f"test report at {path} names no test suite"
        raise ContractFailure(msg)
    return Outcomes(
        total=sum(int(suite.get("tests", 0)) for suite in suites),
        failures=sum(int(suite.get("failures", 0)) for suite in suites),
        errors=sum(int(suite.get("errors", 0)) for suite in suites),
        skipped=sum(int(suite.get("skipped", 0)) for suite in suites),
    )


def run(contract: str = CONTRACT_PATH) -> Outcomes:
    """Run the contract and return its outcomes, or raise with every reason."""
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "provider-contract.xml"
        completed = subprocess.run(  # noqa: S603 - fixed argv, no caller input
            pytest_command(report, contract=contract),
            cwd=str(REPOSITORY_ROOT),
            env=child_environment(os.environ),  # config: intentional - forwards the caller's provider selection
            check=False,
        )
        outcomes = parse_report(report)

    reasons = list(outcomes.failings())
    # A nonzero exit with no per-case reason still fails: a collection error or
    # an internal pytest fault produces exactly that shape, and treating it as
    # a pass because no individual test failed is how a broken run reads green.
    if completed.returncode != 0 and not reasons:
        reasons.append(f"pytest exited {completed.returncode} with no failing case, which points at a collection error")
    if reasons:
        msg = "the provider lifecycle contract did not pass: " + "; ".join(reasons)
        raise ContractFailure(msg)
    return outcomes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_native_provider_contract.py",
        description="Run the focused provider lifecycle contract; a skip is a failure.",
    )
    parser.parse_args(argv)
    try:
        outcomes = run()
    except ContractFailure as error:
        print(f"native provider contract: {error}", file=sys.stderr)
        return 1
    print(f"native provider contract: {outcomes.passed} passed, 0 skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
