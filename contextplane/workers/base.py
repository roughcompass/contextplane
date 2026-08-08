"""Shared scheduling glue for periodic background-worker jobs.

Every worker under `contextplane.workers` and `contextplane.arc.workers` follows the
same run_once/batch/commit shape: claim a bounded batch under its own
transaction, process it, and return a small result dataclass the caller can
log. What differed, job to job, was never that shape -- it was the six or
seven lines of scheduler scaffolding wrapped around the call: a
try/except that turns an unhandled exception into a logged WARNING instead of
a job APScheduler stops running, and an `add_job()` call repeating the same
four scheduling flags with a different job id. `register_periodic` is that
scaffolding, written once instead of once per job.

Why `replace_existing=True` is not cosmetic
--------------------------------------------
The scheduler's job store is durable in production (a row per job id,
persisted across restarts) so that a job registered before a deploy is still
registered after one, even if the process that registered it is gone. That
durability is exactly what makes `replace_existing=True` load-bearing rather
than a default nobody thought about: without it, a redeploy that changes a
job's interval, or removes a job from the source code entirely, would find
the *previous* registration already sitting in the job store and leave it
running. The job store would then be a second, silent source of truth about
what runs on an interval in this process -- one that disagrees with the code
that was just deployed. Every job registered through this helper pins
`replace_existing=True` (along with `max_instances=1` and `coalesce=True`, so
a slow pass delays the next tick rather than overlapping it) precisely so the
source code stays the only thing that decides what is scheduled.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from logging import Logger
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]


def register_periodic(
    scheduler: AsyncIOScheduler,
    run_once: Callable[[], Awaitable[Any]],
    *,
    job_id: str,
    interval_seconds: int,
    log: Logger,
    describe: Callable[[Any], str | None] | None = None,
) -> None:
    """Register `run_once` on `scheduler` as an interval job, the shared way.

    This replaces the hand-rolled `async def _job(): try: ... except
    Exception: log.warning(...)` closure every periodic worker used to get
    its own near-identical copy of. The wrapper built here:

    - Calls `run_once()` with no arguments. A worker whose `run_once` needs
      arguments (a batch size, for instance) is passed in already bound --
      `functools.partial(worker.run_once, batch_size=n)` or an equivalent
      zero-argument callable -- so this helper never needs to know a given
      worker's call signature beyond "returns an awaitable."
    - On exception: logs one WARNING (`"<job_id>: <exception>"`) and
      returns. Never re-raises. A background tick is the one place a
      failure is otherwise invisible -- nothing is on a request path to
      receive the error -- so the only acceptable outcome is "logged, and
      tried again on the next interval," never "this job stops running."
    - On success: if `describe` is given, it is called with the result.
      A non-`None` return is logged at INFO verbatim (it is expected to
      already be a formatted message, not a template); `None` means
      nothing about this run was worth a line -- an empty batch, most
      often. Omitting `describe` means no success-path logging at all,
      which is correct for workers that already log their own summary
      line internally; a second one here would just be noise.

    Every job registered this way gets the same four scheduling flags:
    `trigger="interval"`, `max_instances=1`, `coalesce=True`, and
    `replace_existing=True` (see the module docstring for why the last one
    is load-bearing rather than a default). A job that needs anything else
    -- keyword arguments passed through to the target, a cron trigger, a
    one-shot run at startup -- does not fit this helper and should keep
    calling `scheduler.add_job()` directly.
    """

    async def _job() -> None:
        try:
            result = await run_once()
        except Exception as exc:  # noqa: BLE001 - a swallowed tick must never raise -- see module docstring
            log.warning("%s: %s", job_id, exc)
            return
        if describe is not None:
            message = describe(result)
            if message:
                log.info(message)

    scheduler.add_job(
        _job,
        trigger="interval",
        seconds=interval_seconds,
        max_instances=1,
        coalesce=True,
        id=job_id,
        replace_existing=True,
    )


__all__ = ["register_periodic"]
