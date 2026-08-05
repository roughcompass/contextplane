"""Shared fixtures for performance tests.

All perf tests require a live Postgres container, which is provided by the
session-scoped ``pg_container`` fixture in ``tests/conftest.py`` (shared
across the whole test suite).

The ``perf``/``slow`` marks used throughout this directory are registered
once, in ``pyproject.toml``'s ``[tool.pytest.ini_options] markers``, rather
than re-registered here -- a second registration doesn't change behavior,
it just gives the two `addinivalue_line` calls a second source of truth to
drift from the first.

CI gate
-------
Perf tests are marked ``@pytest.mark.perf`` and ``@pytest.mark.slow``.  To
skip them in normal unit-only CI runs, add ``-m "not perf"`` to the pytest
invocation.  To run only perf: ``pytest tests/perf/ -m perf --timeout=300``.

SLO targets
----------------------------------
- Reverse traversal (depth=5, 100-node graph): p95 < 300 ms.
- Blast-radius (depth=5, 1000-node graph, cache-hit path): p95 < 1 s.
- PII scanner (64 KB input): p95 < 50 ms (no DB required).
"""

from __future__ import annotations
