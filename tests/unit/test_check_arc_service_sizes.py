"""ARC-scoped regression pin for the (now repo-wide) file-sizes gate.

`scripts/check_arc_service_sizes.py` was generalised into
`scripts/check_file_sizes.py`, which scans `contextplane/` and
`contextplane/scripts/` in full rather than only `contextplane/arc/service/`. That
generalisation is the whole point of the change, but it must not be the
moment the ARC service tree's own strictness quietly loosens: this file
existed to pin exactly that tree at zero waivers, and it still does, now
against the gate that replaced it.

Each planted-violation test still plants a file of a controlled size in a
scratch directory, so no real ARC service module needs to be near the
ceiling for the gate's failure mode to be provable.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the scripts directory is importable without installation.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_file_sizes import _CEILING, _WARN_AT, ALLOWLIST, PERMANENT_EXEMPTIONS, main  # noqa: E402


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
    """The gate's own original subject, scanned on its own: the real
    contextplane/arc/service/ tree, today -- proof that the ceiling holds there
    specifically, independent of whatever else is going on in the rest of
    the repo-wide scope the gate now also covers."""
    package = Path(__file__).resolve().parents[2] / "contextplane" / "arc" / "service"
    assert main(["--paths", str(package)]) == 0


def test_no_arc_service_waivers_or_exemptions_are_currently_held() -> None:
    """Documents the gate's starting state for this tree specifically: the
    ceiling holds everywhere under contextplane/arc/service/ today without a
    waiver or a permanent exemption. The repo-wide generalisation added
    entries for files elsewhere in the tree; none of them may be under this
    prefix, or the ARC tree's own strictness has regressed."""
    arc_prefix = "contextplane/arc/service/"
    assert not any(e.path.startswith(arc_prefix) for e in PERMANENT_EXEMPTIONS)
    assert not any(a.path.startswith(arc_prefix) for a in ALLOWLIST)
