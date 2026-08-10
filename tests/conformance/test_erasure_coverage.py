"""What an erasure request reaches, and in which order — wired, not just written.

The erasure registry's own docstring promises that a new subsystem's coverage
is checkable in one list; this is the check. Membership alone is not enough,
for two separate ordering reasons:

- The derivative participant reads the source tables to find what the actor
  authored. It must run before anything that deletes those rows, or it finds
  nothing and silently schedules no propagation.
- The claims participant decides whether a claim has independent evidence by
  resolving session refs against events that must still exist, so claims must
  run strictly before session memory.

A wired-but-misplaced registration would pass a membership assertion while
producing selections that depend on which earlier erasure attempt happened to
fail — the exact nondeterminism the ordering rule exists to prevent.
"""

from __future__ import annotations

import logging

from contextplane.config import Settings


def _subsystems() -> tuple[str, ...]:
    from contextplane.main import create_app

    app = create_app(
        Settings(  # type: ignore[arg-type]
            database_url="postgresql+asyncpg://user:pass@localhost:9999/db",
            pgbouncer_url="postgresql+asyncpg://user:pass@localhost:9999/db",
            scheduler_jobstore_url="postgresql+asyncpg://user:pass@localhost:9999/db",
            scheduler_use_memory_jobstore=True,
            embedding_provider="stub",
            otlp_endpoint=None,
            log_format="json",
            log_level=logging.INFO,
        )
    )
    subsystems: tuple[str, ...] = app.state.erasure.subsystems
    return subsystems


def test_every_personal_data_subsystem_participates_in_order() -> None:
    """The exact tuple, not a subset: a new subsystem holding personal data must
    show up here deliberately, and an ordering change must be argued, not drift."""
    assert _subsystems() == (
        # First deliberately: it reads the source rows every participant below owns,
        # to schedule removal of every derivative built from them. Behind any of
        # them it reads tables already emptied, finds nothing, and schedules no
        # propagation at all — an erasure that reports success while the person's
        # words stay in every artefact derived from their records.
        "context_derivatives",
        "workspace",
        "claims",
        "session_memory",
        "embeddings",
        "usage",
        "signals",
        "receipts",
        "task_checkpoints",
    )
