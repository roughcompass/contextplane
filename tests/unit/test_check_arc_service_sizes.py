"""Unit tests for the check_arc_service_sizes gate script.

Each test plants a file of a controlled size in a scratch directory, so no
real service module needs to be near the ceiling for the gate's failure mode
to be provable. The planted-violation tests matter most: a gate that always
exits green when nothing is oversized is indistinguishable from a gate that
never ran.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the scripts directory is importable without installation.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_arc_service_sizes import _CEILING, _WARN_AT, ALLOWLIST, main  # noqa: E402


def _write_lines(directory: Path, name: str, line_count: int) -> Path:
    """A file with exactly `line_count` newline characters, matching `wc -l`."""
    p = directory / name
    p.write_text("pass\n" * line_count, encoding="utf-8")
    return p


def test_a_file_at_the_ceiling_is_flagged(tmp_path: Path) -> None:
    """The exact boundary this gate exists to enforce: `-lt 800` fails at
    800 exactly, so this gate must too."""
    _write_lines(tmp_path, "oversized.py", _CEILING)
    assert main(["--paths", str(tmp_path)]) == 1


def test_a_file_one_line_over_the_ceiling_is_flagged(tmp_path: Path) -> None:
    _write_lines(tmp_path, "oversized.py", _CEILING + 1)
    assert main(["--paths", str(tmp_path)]) == 1


def test_a_file_one_line_under_the_ceiling_passes(tmp_path: Path) -> None:
    _write_lines(tmp_path, "fine.py", _CEILING - 1)
    assert main(["--paths", str(tmp_path)]) == 0


def test_a_second_oversized_file_planted_alongside_a_compliant_one_still_fails(tmp_path: Path) -> None:
    """The gate scans the whole scope, not just the first file it sees."""
    _write_lines(tmp_path, "fine.py", 10)
    _write_lines(tmp_path, "also_oversized.py", _CEILING + 50)
    assert main(["--paths", str(tmp_path)]) == 1


def test_a_file_approaching_the_ceiling_still_passes(tmp_path: Path) -> None:
    """Above the warn threshold but below the ceiling: visible, not failing."""
    _write_lines(tmp_path, "approaching.py", _WARN_AT)
    assert main(["--paths", str(tmp_path)]) == 0


def test_empty_directory_exits_zero(tmp_path: Path) -> None:
    assert main(["--paths", str(tmp_path)]) == 0


def test_a_missing_scope_fails_rather_than_passing_silently(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert main(["--paths", str(missing)]) == 1


def test_the_real_arc_service_tree_passes() -> None:
    """The gate's own subject: the real repository, today -- proof that the
    split this gate ships alongside actually cleared the ceiling everywhere
    in scope, not just in the three files the split touched."""
    package = Path(__file__).resolve().parents[2] / "registry" / "arc" / "service"
    assert main(["--paths", str(package)]) == 0


def test_no_waivers_are_currently_held() -> None:
    """Documents the gate's starting state: the ceiling holds everywhere
    today without an exception. A future waiver is a deliberate addition,
    not a default this test assumes away."""
    assert ALLOWLIST == ()
