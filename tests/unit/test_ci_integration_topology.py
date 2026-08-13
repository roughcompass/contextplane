"""The shape of the maintainer CI pipeline, asserted rather than assumed.

These are structural assertions over `.github/workflows/ci.yml`. They do not run
CI and they prove nothing about whether the suites pass -- what they defend is the
topology, which is the part that regresses silently. A duplicated tier costs only
wall-clock, so nothing goes red when it comes back; a provider left to resolve
itself still reports under one name; a completeness guarantee moved off the
nightly run leaves no trace at all until the path it covered has already rotted.

Every assertion here is written so that reverting the change it guards makes it
fail. That is the only reason to keep a test over a config file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

#: The events an ordinary pull request produces. The native-stack job must answer
#: the lifecycle question on these and must not re-run the whole suite on them.
_ORDINARY_EVENTS = ("pull_request", "push")

#: The events that carry completeness on the locally managed cluster, where the
#: cost lands on nobody's review cycle.
_COMPLETE_EVENTS = ("schedule", "workflow_dispatch")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    parsed: dict[str, Any] = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return parsed


@pytest.fixture(scope="module")
def jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = workflow["jobs"]
    return found


def _runs(job: dict[str, Any]) -> list[str]:
    return [str(step["run"]) for step in job.get("steps", []) if "run" in step]


def _conditioned_runs(job: dict[str, Any]) -> list[tuple[str, str]]:
    """Every `run` step paired with its `if`, empty string when unconditional."""
    return [(str(step["run"]), str(step.get("if", ""))) for step in job.get("steps", []) if "run" in step]


def _gates_on(condition: str, events: tuple[str, ...]) -> bool:
    """True when `condition` restricts a step to exactly `events`.

    Both spellings count: naming the events it runs on, or excluding the others.
    Written as a containment check rather than a string match on one phrasing so
    that reordering the clauses does not read as a topology change.
    """
    if not condition:
        return False
    positive = all(f"== '{event}'" in condition for event in events)
    excluded = tuple(e for e in (*_ORDINARY_EVENTS, *_COMPLETE_EVENTS) if e not in events)
    negative = all(f"!= '{event}'" in condition for event in excluded)
    return positive or negative


# ---------------------------------------------------------------------------
# The one complete tier a pull request runs, on a named provider


def test_the_container_provider_runs_one_complete_integration_tier(jobs: dict[str, Any]) -> None:
    """A pull request gets the whole tier exactly once, from the pinned provider."""
    integration = jobs["integration"]
    assert "make test-integration" in _runs(integration)

    unconditional = [run for run, condition in _conditioned_runs(integration) if not condition]
    assert (
        "make test-integration" in unconditional
    ), "the complete tier must run on an ordinary pull request, not behind an event gate"


def test_every_provider_is_named_and_never_resolved_by_the_runner(jobs: dict[str, Any]) -> None:
    """`auto` picks by what the runner happens to have.

    Two runners with different container support would then measure two providers
    and report both under one name, which is the one way a parity claim can be
    false while every job is green.
    """
    for name, job in jobs.items():
        provider = (job.get("env") or {}).get("CONTEXTPLANE_TEST_PG")
        assert provider != "auto", f"job {name!r} resolves its provider with 'auto'"
        for run in _runs(job):
            assert "CONTEXTPLANE_TEST_PG=auto" not in run, f"job {name!r} sets 'auto' inline"

    assert jobs["integration"]["env"]["CONTEXTPLANE_TEST_PG"] == "testcontainers"
    assert jobs["native-stack"]["env"]["CONTEXTPLANE_TEST_PG"] == "devstack"


def test_the_two_providers_are_actually_two(jobs: dict[str, Any]) -> None:
    """A parity claim needs two distinct members; one name twice is not parity."""
    container = jobs["integration"]["env"]["CONTEXTPLANE_TEST_PG"]
    native = jobs["native-stack"]["env"]["CONTEXTPLANE_TEST_PG"]
    assert container != native


# ---------------------------------------------------------------------------
# Conformance is its own job and does not queue behind the slowest tier


def test_conformance_does_not_wait_on_the_integration_tier(jobs: dict[str, Any]) -> None:
    """Nothing passes from one to the other, so the dependency was pure latency."""
    needs = jobs["conformance"]["needs"]
    needs = [needs] if isinstance(needs, str) else list(needs)
    assert "integration" not in needs, "conformance must not serialize behind the integration tier"
    assert "unit" in needs


def test_conformance_remains_a_separate_job(jobs: dict[str, Any]) -> None:
    """Folding it into another job would hide which gate failed."""
    assert "conformance" in jobs
    assert "make test-conformance" in _runs(jobs["conformance"])


# ---------------------------------------------------------------------------
# The locally managed cluster: focused on a pull request, complete on a schedule


def test_a_pull_request_gets_the_focused_native_lifecycle(jobs: dict[str, Any]) -> None:
    """The lifecycle target answers "does the no-container path work" on its own."""
    focused = [
        condition
        for run, condition in _conditioned_runs(jobs["native-stack"])
        if run.strip() == "make test-native-provider"
    ]
    assert focused, "the native-stack job must run the focused provider lifecycle"
    assert any(
        _gates_on(condition, _ORDINARY_EVENTS) or not condition for condition in focused
    ), "the focused lifecycle must run on an ordinary pull request"


def test_a_pull_request_does_not_run_the_complete_native_tier_a_second_time(jobs: dict[str, Any]) -> None:
    """The duplicate this topology exists to remove.

    The container tier above already ran every one of these nodes. Running them
    again on the locally managed cluster re-derived one bit -- that the native path
    works -- at the cost of the slowest job in the pipeline.
    """
    for run, condition in _conditioned_runs(jobs["native-stack"]):
        if run.strip() in {"make test-integration", "make test-conformance"}:
            assert _gates_on(condition, _COMPLETE_EVENTS), (
                f"{run.strip()!r} runs on a pull request in the native-stack job; the complete tier "
                "belongs on the scheduled and on-demand runs only"
            )


def test_the_scheduled_run_keeps_complete_native_coverage(jobs: dict[str, Any]) -> None:
    """Moved off the pull request, not dropped.

    Without this the no-container path rots silently, which is the stated reason
    this job exists at all.
    """
    native = _conditioned_runs(jobs["native-stack"])
    for target in ("make test-integration", "make test-conformance"):
        gated = [condition for run, condition in native if run.strip() == target]
        assert gated, f"{target!r} is missing from the native-stack job entirely"
        assert any(
            _gates_on(condition, _COMPLETE_EVENTS) for condition in gated
        ), f"{target!r} no longer runs on the scheduled or on-demand events"


def test_the_scheduled_and_on_demand_events_both_exist(workflow: dict[str, Any]) -> None:
    """The completeness guarantee above is only worth as much as its triggers."""
    # `on` parses as a bool key under the YAML 1.1 rules PyYAML follows.
    triggers = workflow.get("on") or workflow[True]
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers


# ---------------------------------------------------------------------------
# CI invokes the command surface and nothing else


def test_ci_invokes_make_targets_rather_than_inlining_the_runner(jobs: dict[str, Any]) -> None:
    """A pytest path or a worker count in a workflow file is a second source of truth.

    The inner runner seals collection and scheduling; a caller that passes its own
    selector or worker count runs one configuration while the committed default
    claims another. The workflow is not allowed to be that caller.
    """
    forbidden = ("pytest ", "-m pytest", "--workers", "-p no:", "--deselect", "-k ", "--last-failed", "--maxfail")
    for name, job in jobs.items():
        for run in _runs(job):
            for token in forbidden:
                assert token not in run, f"job {name!r} inlines {token!r} instead of invoking a make target"


def test_ci_timing_is_not_treated_as_release_acceptance(jobs: dict[str, Any]) -> None:
    """A shared runner cannot carry the absolute timing claim.

    Its neighbours are not controlled, so a number measured here says nothing
    about whether the target was met. The sealed outer controller produces that
    evidence on a known host, and nothing in this workflow may stand in for it.
    """
    forbidden = ("--require-target-met", "run_integration_performance_gate", "/usr/bin/time", "--durations")
    for name, job in jobs.items():
        for run in _runs(job):
            for token in forbidden:
                assert (
                    token not in run
                ), f"job {name!r} references {token!r}; CI timing is topology validation, not acceptance"
