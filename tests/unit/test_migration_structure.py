"""Unit tests — baseline migration module structure.

Verifies that the baseline migration module is importable without a live DB
connection, carries a single-revision chain (no down_revision — this is the
root and only revision), and exposes callable upgrade/downgrade hooks. Also
guards the one property a schema-diff proof cannot re-check on every run:
that fixed-window partition DDL does not silently start depending on the
system clock again.

Five migration-specific tests used to live here, each loading a distinct
phase-named migration module (0005, 0018, 0007, 0006, 0009) and asserting
either its revision metadata or the exact SQL its upgrade()/downgrade()
emitted. All five tested the shape of a step in a 47-revision chain that no
longer exists — 0018's table (`capability_annotations`) and 0009's
`workspace_shares`/`workspace_share_acceptances` are gone from the schema
entirely, and the others (0005, 0006, 0007) are folded into one CREATE TABLE
each in the baseline with no discrete ADD COLUMN / bind-parameterized-INSERT
step left to inspect. The one property worth carrying forward — that a
fixed-window partition helper's output must not vary with the day it runs on
— is reworked below against the baseline's shared helper.
"""

from __future__ import annotations

import datetime as _dt
import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

_MODULE_NAME = "contextplane.storage.migrations.versions.0001_baseline_schema"


def test_baseline_migration_importable() -> None:
    """The baseline module must be importable without a DB connection."""
    mod = importlib.import_module(_MODULE_NAME)
    assert mod.revision == "0001_baseline_schema"
    assert mod.down_revision is None
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_fixed_window_partition_bounds_do_not_depend_on_the_system_clock() -> None:
    """audit_log / audit_log_new / usage_events partition DDL must not vary
    with the day the migration happens to run on.

    Calls `_monthly_partition_bounds` with two different `datetime.date`
    "today" values patched underneath it and asserts the output is
    identical — proving the fixed-window helper never reads the clock at
    all, unlike the per-month helper the notifications/PII-log tables use.
    """
    mod = importlib.import_module(_MODULE_NAME)

    def _bounds_with_patched_today(patched_today: _dt.date) -> list[tuple[str, str, str]]:
        original_date = _dt.date

        class _StubDate(original_date):
            @classmethod
            def today(cls) -> _dt.date:
                return patched_today

        with patch.object(mod.datetime, "date", _StubDate):
            return list(mod._monthly_partition_bounds(mod._FIXED_PARTITION_START, mod._FIXED_PARTITION_COUNT))

    bounds_may = _bounds_with_patched_today(_dt.date(2026, 5, 1))
    bounds_jun = _bounds_with_patched_today(_dt.date(2026, 6, 1))

    assert bounds_may == bounds_jun, "fixed-window partition bounds must not depend on the system clock"
    assert bounds_may[0][0] == "2025_01", "the fixed origin must stay pinned to 2025-01"
    assert len(bounds_may) == 24


def test_current_month_partition_helper_does_read_the_clock() -> None:
    """The companion helper for notifications/PII-log tables is deliberately
    clock-dependent — it creates one pre-created partition for whatever
    month the migration runs in, unlike the fixed 24-month window above."""
    mod = importlib.import_module(_MODULE_NAME)

    may_bounds = mod._current_month_partition_bounds(_dt.date(2026, 5, 15))
    jun_bounds = mod._current_month_partition_bounds(_dt.date(2026, 6, 1))

    assert may_bounds[0] == "2026_05"
    assert jun_bounds[0] == "2026_06"
    assert may_bounds != jun_bounds


def test_upgrade_runs_without_a_real_database() -> None:
    """A structural smoke test: upgrade() issues only op.execute calls (or
    op.get_bind()-mediated ones covered elsewhere), never touches a real
    connection, and does not raise when every statement is captured rather
    than executed.

    This does not assert schema correctness — that is the job of the
    integration suite and the schema-diff proof — only that the function
    runs to completion against a mocked `op`, which catches a Python-level
    mistake (a typo'd constant name, a section run out of dependency order
    that raises before the DB would) without a container.
    """
    mod = importlib.import_module(_MODULE_NAME)

    executed: list[str] = []
    op_mock = MagicMock()
    op_mock.execute = MagicMock(side_effect=lambda s, *_a, **_k: executed.append(str(s)))

    with patch.object(mod, "op", op_mock):
        mod.upgrade()

    assert len(executed) > 100, "expected a large number of DDL statements from the full baseline"
    assert any("CREATE TABLE memory_claims" in s for s in executed)
    assert any("CREATE TABLE arc_receipts" in s for s in executed)
    assert not any("CREATE TABLE arc_content_deletion_verifications" in s for s in executed)


def test_the_revision_chain_is_unbroken_and_has_one_head() -> None:
    """A deleted migration that something still points at.

    Caught once by `make test-airgap`, which runs `alembic upgrade head` inside a
    container — a true signal, arriving after a Docker build, on the slowest job
    in CI. Two branches developed in parallel are enough to cause it: one adds a
    migration on top of another, the second removes the one underneath, and
    neither branch is broken until they meet.

    `walk_revisions` raises on a dangling `down_revision`, so building the map is
    the assertion. The head count is the other half: two heads is a fork alembic
    will refuse to upgrade past, and it is the same merge that produces it.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(Config(str(repo_root / "alembic.ini")))

    revisions = list(script.walk_revisions())
    heads = script.get_heads()

    assert len(heads) == 1, f"the migration chain has forked: {heads}"
    assert len(revisions) == len({r.revision for r in revisions})
