#!/usr/bin/env python3
"""The worker's half of the integration run contract.

A worker discloses what it did through a private append-only event stream, not
through its stdout. Parsing pytest's prose would make the aggregation a
function of pytest's formatting, and the runner this serves exists to make a
run's outcome reproducible across versions of everything it does not own.

Split out of the runner rather than left beside it, and the seam is the
contract rather than the line count. The runner is the parent: it qualifies an
invocation, collects, schedules, dispatches and reconciles. This module is the
half that executes inside somebody else's pytest process, loaded there by name
as a plugin, holding no opinion about scheduling and no access to the parent's
state. The two communicate through one append-only file and two environment
variable names, all three defined here because the disclosing side owns the
disclosure format.

That boundary is also why the plugin lives in its own module at all: a worker
loads it with `-p integration_reporter`, and a module whose import pulls in the
parent's argument parsing, subprocess handling and process-group teardown is a
much larger surface to admit into every worker than the reporter needs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from integration_scheduler import NodeOutcome

if TYPE_CHECKING:
    from io import TextIOBase

    import pytest

#: How the parent tells a worker where to disclose, and who it is disclosing
#: as. Both must be present for this module to register anything: absent them
#: it is being imported for its definitions rather than run as a worker.
EVENTS_PATH_VARIABLE: Final = "CONTEXTPLANE_INTEGRATION_EVENTS"
WORKER_ID_VARIABLE: Final = "CONTEXTPLANE_INTEGRATION_WORKER_ID"

# Highest rank wins when several reports arrive for one node. A node that errors
# in teardown after passing its call is an error: the ranking exists so that the
# worst thing that happened to a node is what gets disclosed, rather than
# whichever phase happened to report last.
_OUTCOME_RANK: Final = {
    NodeOutcome.PASSED: 0,
    NodeOutcome.SKIPPED: 1,
    NodeOutcome.FAILED: 2,
    NodeOutcome.ERROR: 3,
}

_REPORTER_PLUGIN_NAME: Final = "contextplane-worker-reporter"


class WorkerReporter:
    """Emits exactly one start and one terminal event per node.

    Sequence numbers are contiguous from 1 within this worker, because the
    parent treats a gap as a lost event and a lost event is indistinguishable
    from a node that failed silently.

    Every record is flushed as it is written. A worker that is killed for
    overrunning its interval must leave behind what it had already disclosed --
    an event stream that only survives a clean exit tells the parent nothing
    about the run that actually needed explaining.
    """

    def __init__(self, *, worker_id: str, stream: TextIOBase) -> None:
        self._worker_id = worker_id
        self._stream = stream
        self._sequence = 0
        self._started: set[str] = set()
        self._result: dict[str, NodeOutcome] = {}

    def _emit(self, *, node: str, outcome: NodeOutcome | None, started: bool) -> None:
        self._sequence += 1
        record = {
            "worker_id": self._worker_id,
            "sequence": self._sequence,
            "node": node,
            "outcome": outcome.value if outcome is not None else None,
            "started": started,
        }
        self._stream.write(json.dumps(record, sort_keys=True) + "\n")
        self._stream.flush()

    def _promote(self, node: str, candidate: NodeOutcome) -> None:
        current = self._result.get(node)
        if current is None or _OUTCOME_RANK[candidate] > _OUTCOME_RANK[current]:
            self._result[node] = candidate

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        if nodeid in self._started:
            return
        self._started.add(nodeid)
        self._emit(node=nodeid, outcome=None, started=True)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed:
            # A failure outside the call phase is a fixture or teardown problem,
            # which is an error rather than a failing assertion.
            self._promote(report.nodeid, NodeOutcome.FAILED if report.when == "call" else NodeOutcome.ERROR)
        elif report.skipped:
            self._promote(report.nodeid, NodeOutcome.SKIPPED)
        elif report.when == "call":
            self._promote(report.nodeid, NodeOutcome.PASSED)

    def close(self) -> None:
        self._stream.close()

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        outcome = self._result.pop(nodeid, None)
        if outcome is None:
            # Deliberately silent. A node that started and produced no report
            # stays undisclosed, and the parent's reconciliation names it
            # missing -- inventing a terminal event here would convert a lost
            # result into a reported one.
            return
        self._emit(node=nodeid, outcome=outcome, started=False)


def pytest_configure(config: pytest.Config) -> None:
    """Register the reporter when this module is loaded as a worker plugin.

    Absent the two variables this module is being imported for its definitions
    rather than run as a worker, so it registers nothing.
    """
    path = os.environ.get(EVENTS_PATH_VARIABLE)  # config: intentional - the parent addresses its worker by environment
    worker_id = os.environ.get(WORKER_ID_VARIABLE)  # config: intentional - the parent names its worker by environment
    if not path or not worker_id:
        return
    stream = Path(path).open("a", encoding="utf-8")
    config.pluginmanager.register(WorkerReporter(worker_id=worker_id, stream=stream), _REPORTER_PLUGIN_NAME)


def pytest_unconfigure(config: pytest.Config) -> None:
    reporter = config.pluginmanager.get_plugin(_REPORTER_PLUGIN_NAME)
    if reporter is not None:
        config.pluginmanager.unregister(reporter)
        reporter.close()
