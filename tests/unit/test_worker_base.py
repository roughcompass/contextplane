"""Unit tests for `register_periodic`, the shared scheduler-registration helper.

Every periodic worker job used to get its own hand-rolled
`async def _job(): try: ... except Exception: log.warning(...)` closure plus
a repeated `add_job(..., max_instances=1, coalesce=True, replace_existing=True)`
call. `register_periodic` is that scaffolding written once; these tests pin
its two responsibilities down directly, without needing a real scheduler or
a real worker:

- the wrapper it builds never lets an exception from `run_once` escape, and
  always logs it at WARNING instead;
- the wrapper calls `describe(result)` on success and logs the result at
  INFO only when `describe` returns something non-`None`;
- the `add_job()` call it makes carries the pinned scheduling flags
  (`trigger="interval"`, `max_instances=1`, `coalesce=True`,
  `replace_existing=True`) and passes `job_id` / `interval_seconds` through
  unchanged.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from registry.workers.base import register_periodic


def _fake_scheduler() -> MagicMock:
    """A scheduler double whose `add_job` only records its call -- it never runs anything."""
    return MagicMock()


def _registered_job(scheduler: MagicMock) -> Any:
    """Return the callable passed as the job function to `add_job`.

    `register_periodic` always calls `add_job(job_fn, trigger=..., ...)`, so
    the job function is the first positional argument of the one recorded call.
    """
    args, _kwargs = scheduler.add_job.call_args
    return args[0]


# ---------------------------------------------------------------------------
# Exception handling: swallowed, never raised, logged at WARNING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exception_from_run_once_is_swallowed_not_raised() -> None:
    """A run_once that raises must not propagate out of the registered job."""
    scheduler = _fake_scheduler()

    async def _failing_run_once() -> int:
        raise RuntimeError("boom")

    register_periodic(
        scheduler,
        _failing_run_once,
        job_id="some_job",
        interval_seconds=60,
        log=logging.getLogger("test"),
    )

    job_fn = _registered_job(scheduler)
    # Must not raise -- this is the whole point of the wrapper.
    await job_fn()


@pytest.mark.asyncio
async def test_exception_from_run_once_is_logged_at_warning_with_job_id() -> None:
    """The WARNING carries the job id and the exception, so an operator can find it."""
    scheduler = _fake_scheduler()
    log = MagicMock(spec=logging.Logger)

    async def _failing_run_once() -> int:
        raise ValueError("bad state")

    register_periodic(
        scheduler,
        _failing_run_once,
        job_id="my_worker_job",
        interval_seconds=60,
        log=log,
    )

    job_fn = _registered_job(scheduler)
    await job_fn()

    log.warning.assert_called_once()
    args = log.warning.call_args.args
    assert args[0] == "%s: %s"
    assert args[1] == "my_worker_job"
    assert isinstance(args[2], ValueError)
    log.info.assert_not_called()


@pytest.mark.asyncio
async def test_describe_is_not_called_when_run_once_raises() -> None:
    """A failed run_once has no result -- describe must never see one."""
    scheduler = _fake_scheduler()
    describe = MagicMock(return_value="should never be logged")

    async def _failing_run_once() -> int:
        raise RuntimeError("boom")

    register_periodic(
        scheduler,
        _failing_run_once,
        job_id="some_job",
        interval_seconds=60,
        log=logging.getLogger("test"),
        describe=describe,
    )

    job_fn = _registered_job(scheduler)
    await job_fn()

    describe.assert_not_called()


# ---------------------------------------------------------------------------
# describe() on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_called_with_result_and_logged_at_info() -> None:
    """A non-None describe() return is logged at INFO, verbatim."""
    scheduler = _fake_scheduler()
    log = MagicMock(spec=logging.Logger)
    describe = MagicMock(return_value="job_x.run: processed=3")

    async def _run_once() -> str:
        return "the-result"

    register_periodic(
        scheduler,
        _run_once,
        job_id="job_x",
        interval_seconds=60,
        log=log,
        describe=describe,
    )

    job_fn = _registered_job(scheduler)
    await job_fn()

    describe.assert_called_once_with("the-result")
    log.info.assert_called_once_with("job_x.run: processed=3")
    log.warning.assert_not_called()


@pytest.mark.asyncio
async def test_describe_returning_none_logs_nothing() -> None:
    """A describe() that decides a result isn't noteworthy causes no INFO log."""
    scheduler = _fake_scheduler()
    log = MagicMock(spec=logging.Logger)
    describe = MagicMock(return_value=None)

    async def _run_once() -> int:
        return 0

    register_periodic(
        scheduler,
        _run_once,
        job_id="job_y",
        interval_seconds=60,
        log=log,
        describe=describe,
    )

    job_fn = _registered_job(scheduler)
    await job_fn()

    describe.assert_called_once_with(0)
    log.info.assert_not_called()


@pytest.mark.asyncio
async def test_no_describe_means_no_success_logging() -> None:
    """Omitting describe entirely means the wrapper stays silent on success."""
    scheduler = _fake_scheduler()
    log = MagicMock(spec=logging.Logger)

    async def _run_once() -> int:
        return 42

    register_periodic(
        scheduler,
        _run_once,
        job_id="job_z",
        interval_seconds=60,
        log=log,
    )

    job_fn = _registered_job(scheduler)
    await job_fn()

    log.info.assert_not_called()
    log.warning.assert_not_called()


# ---------------------------------------------------------------------------
# add_job(): pinned scheduling flags, job_id / interval passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_job_carries_pinned_scheduling_flags() -> None:
    """Every job registered this way gets the same interval-trigger shape.

    `replace_existing=True` is the load-bearing one -- without it, a durable
    job store would keep running a previous registration after a redeploy
    changes or removes this job.
    """
    scheduler = _fake_scheduler()

    async def _run_once() -> None:
        return None

    register_periodic(
        scheduler,
        _run_once,
        job_id="anything",
        interval_seconds=45,
        log=logging.getLogger("test"),
    )

    scheduler.add_job.assert_called_once()
    _args, kwargs = scheduler.add_job.call_args
    assert kwargs["trigger"] == "interval"
    assert kwargs["max_instances"] == 1
    assert kwargs["coalesce"] is True
    assert kwargs["replace_existing"] is True


@pytest.mark.asyncio
async def test_job_id_and_interval_seconds_pass_through_unchanged() -> None:
    """The `id` and `seconds` kwargs on add_job must match what the caller asked for."""
    scheduler = _fake_scheduler()

    async def _run_once() -> None:
        return None

    register_periodic(
        scheduler,
        _run_once,
        job_id="closure_refresh_evict",
        interval_seconds=3600,
        log=logging.getLogger("test"),
    )

    _args, kwargs = scheduler.add_job.call_args
    assert kwargs["id"] == "closure_refresh_evict"
    assert kwargs["seconds"] == 3600


@pytest.mark.asyncio
async def test_different_calls_use_their_own_job_id_and_interval() -> None:
    """Two independent registrations don't leak each other's id/interval."""
    scheduler = _fake_scheduler()

    async def _a() -> None:
        return None

    async def _b() -> None:
        return None

    register_periodic(scheduler, _a, job_id="job_a", interval_seconds=10, log=logging.getLogger("test"))
    register_periodic(scheduler, _b, job_id="job_b", interval_seconds=20, log=logging.getLogger("test"))

    first_kwargs = scheduler.add_job.call_args_list[0].kwargs
    second_kwargs = scheduler.add_job.call_args_list[1].kwargs
    assert first_kwargs["id"] == "job_a"
    assert first_kwargs["seconds"] == 10
    assert second_kwargs["id"] == "job_b"
    assert second_kwargs["seconds"] == 20
