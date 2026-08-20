"""Naming what failed, which the integration runner could not do for its whole life.

The runner reconciles every node and printed one line: `2587 nodes reconciled
({'failed': 1, ...})`. That is the count and nothing else. Finding the test meant
re-running the whole tier locally, because pytest writes its failure summary to
**stdout** while the runner forwarded only worker **stderr** — so a CI log for a
failing run contained eight copies of an asyncio deprecation warning and no
reason for the failure it was reporting.

Two functions, both pure and both about output rather than about running
anything. They live here rather than in the runner because the runner is at its
800-line ceiling, and a reporting helper is the part of it that has the least to
do with sealing a run.

Both are deliberately bounded. A worker that failed a hundred nodes should make
the log longer, not turn it into the whole suite.
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from typing import Final

#: Where pytest starts describing what went wrong. Both headers are stable parts
#: of its terminal output, and matching either means a worker that died during
#: setup is reported as fully as one that failed inside a test.
FAILURE_HEADERS: Final = (
    "=================================== FAILURES",
    "==================================== ERRORS",
)

#: Characters of a failing worker's stdout to forward. Roughly a dozen
#: tracebacks: enough for the run that fails one node and for the run that fails
#: a related handful, and short of the run that fails everything.
MAX_FORWARDED_CHARACTERS: Final = 20_000


def failure_section(stdout: str) -> str:
    """The part of a worker's stdout a reader needs, or nothing at all.

    Empty for a worker that passed, so a green run forwards nothing and a red one
    forwards exactly the tracebacks. Sliced from the summary header rather than
    forwarded whole, because a passing worker's stdout is thousands of progress
    dots and forwarding it would bury the thing it was added to surface.
    """
    for header in FAILURE_HEADERS:
        marker = stdout.find(header)
        if marker != -1:
            return stdout[marker : marker + MAX_FORWARDED_CHARACTERS]
    return ""


def unsuccessful_lines(unsuccessful: Mapping[str, str]) -> list[str]:
    """One line per failing node, then one command that reproduces the first.

    Takes plain labels rather than the scheduler's enum, so this module imports
    nothing from the runner it reports for and can be exercised on its own.

    The reproduction hint is not decoration. The node id is a pytest selector and
    the tier needs a database, so the two facts a reader needs next are the id and
    the environment variable that gives them one — and a reader who has to look
    the second one up is a reader who runs the whole tier instead.
    """
    if not unsuccessful:
        return []
    lines = [f"integration runner: {label.upper()} {node}" for node, label in sorted(unsuccessful.items())]
    first = next(iter(sorted(unsuccessful)))
    lines.append(
        "integration runner: reproduce one of these with\n"
        f"  CONTEXTPLANE_TEST_PG=testcontainers python -m pytest '{first}' -q"
    )
    return lines


def worker_output(streams: Iterable[tuple[str, str]]) -> Iterator[str]:
    """Everything from the workers a reader of a failed run needs, and nothing else.

    Takes `(stderr, stdout)` per worker. Stderr goes through as it always has;
    stdout contributes only its failure section, which is the half that was
    missing -- pytest writes warnings to stderr and assertions to stdout, so
    forwarding only the first produced logs full of deprecation notices and empty
    of reasons.
    """
    for stderr, stdout in streams:
        if stderr.strip():
            yield stderr
        section = failure_section(stdout)
        if section:
            yield section


#: Outcomes a reconciled run may hold without being a failure. A skip is a
#: disclosed outcome, not a lost one: modules opt out on an absent credential or
#: an unreachable stack, and counting that as unsuccessful would leave the target
#: unable to exit zero anywhere, CI included -- the zero-test pass with its sign
#: flipped. What catches a run going shorter than its suite in silence is
#: untouched: an empty collection is fatal and an undisclosed node is `MISSING`
#: and voids the run by name, both upstream of here.
DISCLOSED_WITHOUT_FAILING: Final = frozenset({"passed", "skipped"})


def report_outcomes(outcomes: Mapping[str, str]) -> int:
    """Print the reconciliation summary and return the exit code.

    Takes node-to-label rather than the scheduler's enum so the whole reporting
    tail is testable without a run. The summary line is unchanged; what is new
    below it is the names, because the count alone is what this printed for its
    whole life and a CI log reading `{'failed': 1}` out of 2587 leaves a reader
    with no way to find the test.
    """
    unsuccessful = {node: label for node, label in outcomes.items() if label not in DISCLOSED_WITHOUT_FAILING}
    counts = Counter(outcomes.values())
    print(f"integration runner: {len(outcomes)} nodes reconciled ({dict(sorted(counts.items()))})")
    for line in unsuccessful_lines(unsuccessful):
        print(line, file=sys.stderr)
    return 1 if unsuccessful else 0


__all__ = [
    "DISCLOSED_WITHOUT_FAILING",
    "FAILURE_HEADERS",
    "MAX_FORWARDED_CHARACTERS",
    "failure_section",
    "report_outcomes",
    "unsuccessful_lines",
    "worker_output",
]
