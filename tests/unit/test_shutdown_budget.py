"""The shutdown budget adds up, in the image and in the chart.

Teardown spends time in two places before the process may be killed: the pause
that lets endpoint removal propagate, and the server's own wait for open
connections. If the orchestrator's grace period is not larger than their sum,
the container is killed mid-teardown and every rollout silently discards queued
spans and any delivery attempt still in flight.

Nothing about that failure is visible at deploy time — the pod terminates, the
rollout proceeds, and the loss is only measurable in the traces that never
arrived. So the arithmetic is asserted here rather than described in a comment
next to three numbers that live in three different files.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_VALUES = _ROOT / "deploy" / "helm" / "values.yaml"
_DEPLOYMENT = _ROOT / "deploy" / "helm" / "templates" / "deployment-api.yaml"
_DOCKERFILE = _ROOT / "Dockerfile"


def _values() -> dict[str, object]:
    loaded = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _image_graceful_shutdown_seconds() -> int:
    """The --timeout-graceful-shutdown the image's CMD runs with."""
    cmd = next(line for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.startswith("CMD "))
    match = re.search(r'"--timeout-graceful-shutdown",\s*"(\d+)"', cmd)
    assert match is not None, f"the image no longer bounds its shutdown wait: {cmd}"
    return int(match.group(1))


def test_the_image_bounds_its_shutdown_wait() -> None:
    # Unbounded is the default, and the streaming endpoint means unbounded is
    # forever: an incomplete response is never an idle connection.
    assert _image_graceful_shutdown_seconds() > 0


def test_the_grace_period_covers_both_waits() -> None:
    values = _values()
    grace = values["terminationGracePeriodSeconds"]
    pre_stop = values["preStopSleepSeconds"]
    assert isinstance(grace, int)
    assert isinstance(pre_stop, int)

    required = pre_stop + _image_graceful_shutdown_seconds()
    assert grace > required, (
        f"terminationGracePeriodSeconds={grace} leaves no room for a "
        f"{pre_stop}s pre-stop pause plus a {_image_graceful_shutdown_seconds()}s "
        f"shutdown wait; the container would be killed mid-teardown"
    )


def test_the_deployment_uses_both_settings() -> None:
    # A value nothing reads is a value that reads as configured and is not.
    rendered = _DEPLOYMENT.read_text(encoding="utf-8")

    assert ".Values.terminationGracePeriodSeconds" in rendered
    assert ".Values.preStopSleepSeconds" in rendered
    assert "preStop" in rendered
