"""Re-export of the test-database provider, which lives under ``scripts/``.

The provider selects the source of the suite's Postgres — an external
``DATABASE_URL``, testcontainers, or a locally managed cluster — and hands back
a freshly migrated database. It is shared by the parent-side runner and this
suite's fixtures. It lives under ``scripts/`` because ``scripts/`` must not
import from ``tests/`` — the dependency runs the other way, the same direction
as this module's own implementation importing ``scripts.devstack``. This module
keeps the test-facing import path stable so callers need not know where the
implementation sits.
"""

from __future__ import annotations

from scripts.pg_provider import (
    admin_executor,
    build_broker,
    describe,
    devstack_available,
    docker_available,
    probe_capabilities,
    run_migrations,
    selected_mode,
    test_database,
)

__all__ = [
    "admin_executor",
    "build_broker",
    "describe",
    "devstack_available",
    "docker_available",
    "probe_capabilities",
    "run_migrations",
    "selected_mode",
    "test_database",
]
