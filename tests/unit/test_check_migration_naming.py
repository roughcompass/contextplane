"""Unit tests for the check_migration_naming gate script.

Each test plants a migration filename the gate should or should not flag,
in a scratch directory, so no real migration file is required. The
planted-violation tests are the ones that matter most: a gate that always
exits green when nothing matches is indistinguishable from a gate that never
ran, so each failure mode here is proven to actually fire.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the scripts directory is importable without installation.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_migration_naming import main  # noqa: E402


def _write(directory: Path, name: str, content: str = "def upgrade() -> None: ...\n") -> Path:
    p = directory / name
    p.write_text(content, encoding="utf-8")
    return p


def test_a_phase_numbered_filename_is_flagged(tmp_path: Path) -> None:
    """The exact failure mode this gate exists to prevent: a new migration
    named for when it shipped rather than what it does."""
    _write(tmp_path, "0002_phase9_something.py")
    assert main(["--paths", str(tmp_path)]) == 1


def test_an_lmm_prefixed_filename_is_flagged(tmp_path: Path) -> None:
    """The retired prefix reappearing in a new migration's filename is
    exactly the half-finished rename this gate exists to catch."""
    _write(tmp_path, "0002_lmm_something_new.py")
    assert main(["--paths", str(tmp_path)]) == 1


def test_a_behavior_named_filename_passes(tmp_path: Path) -> None:
    _write(tmp_path, "0002_add_capability_search_index.py")
    assert main(["--paths", str(tmp_path)]) == 0


def test_the_baseline_filename_itself_passes(tmp_path: Path) -> None:
    """Not exempted by name — it simply matches neither forbidden pattern."""
    _write(tmp_path, "0001_baseline_schema.py")
    assert main(["--paths", str(tmp_path)]) == 0


def test_init_file_is_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "__init__.py")
    assert main(["--paths", str(tmp_path)]) == 0


def test_empty_directory_exits_zero(tmp_path: Path) -> None:
    assert main(["--paths", str(tmp_path)]) == 0


def test_a_missing_scope_fails_rather_than_passing_silently(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert main(["--paths", str(missing)]) == 1


def test_the_real_versions_directory_passes() -> None:
    """The gate's own subject: the real repository, today."""
    package = Path(__file__).resolve().parents[2] / "contextplane" / "storage" / "migrations" / "versions"
    assert main(["--paths", str(package)]) == 0
