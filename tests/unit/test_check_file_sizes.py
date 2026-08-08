"""Unit tests for the repo-wide check_file_sizes gate script.

Each planted-violation test builds a scratch directory or a monkeypatched
copy of `ALLOWLIST`, so no real file needs to sit at a controlled size for
the gate's failure modes to be provable. Two failure modes matter equally
here, not just one: a gate that always exits green when nothing is oversized
is indistinguishable from a gate that never ran, and an allowlist that never
fails when a waived file has quietly stopped needing the waiver is
indistinguishable from a permission with no expiry.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the scripts directory is importable without installation.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_file_sizes as gate  # noqa: E402
from check_file_sizes import (  # noqa: E402
    _CEILING,
    _WARN_AT,
    ALLOWLIST,
    PERMANENT_EXEMPTIONS,
    AllowlistEntry,
    PermanentExemption,
    main,
)


def _write_lines(directory: Path, name: str, line_count: int) -> Path:
    """A file with exactly `line_count` newline characters, matching `wc -l`."""
    p = directory / name
    p.write_text("pass\n" * line_count, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Basic ceiling behavior (same shape as the ARC-only predecessor gate)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Bite proof (a): a planted violation outside the allowlist fails, by name.
# ---------------------------------------------------------------------------


def test_planted_violation_not_in_allowlist_fails_naming_the_file(tmp_path: Path, capsys) -> None:
    planted = _write_lines(tmp_path, "not_allowlisted.py", _CEILING + 1)
    assert main(["--paths", str(tmp_path)]) == 1
    out = capsys.readouterr()
    assert str(planted) in out.err


# ---------------------------------------------------------------------------
# Bite proof (b): a stale ALLOWLIST entry fails too, by both routes staleness
# can happen -- a path that no longer exists, and a file that has genuinely
# dropped under the ceiling. This is the same shape
# check_test_assertions.py's own stale-entry check uses, reused here rather
# than re-invented.
# ---------------------------------------------------------------------------


def test_stale_entry_nonexistent_path_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        gate,
        "ALLOWLIST",
        ALLOWLIST + (AllowlistEntry(path="contextplane/this/path/does/not/exist.py", reason="probe"),),
    )
    assert main([]) == 1
    out = capsys.readouterr()
    assert "this/path/does/not/exist.py" in out.err
    assert "no longer exists" in out.err


def test_stale_entry_file_dropped_under_ceiling_fails(monkeypatch, capsys) -> None:
    """A real file, comfortably under the ceiling today, named in a fake
    allowlist entry -- the "shrunk enough, waiver no longer needed" case,
    proven without waiting for a real allowlist entry to become stale."""
    monkeypatch.setattr(
        gate,
        "ALLOWLIST",
        ALLOWLIST + (AllowlistEntry(path="contextplane/arc/models.py", reason="probe: already under ceiling"),),
    )
    assert main([]) == 1
    out = capsys.readouterr()
    assert "arc/models.py" in out.err
    assert "under the 800-line ceiling" in out.err


def test_stale_entry_check_ignores_the_paths_flag(monkeypatch, tmp_path: Path) -> None:
    """A narrowed `--paths` must not hide a stale entry outside it -- the
    stale check re-verifies every ALLOWLIST entry against its own real path
    regardless of what scope this invocation was scanning."""
    monkeypatch.setattr(
        gate,
        "ALLOWLIST",
        ALLOWLIST + (AllowlistEntry(path="contextplane/this/path/does/not/exist.py", reason="probe"),),
    )
    # An unrelated, empty scope: nothing here would ever surface the entry
    # through the normal scan.
    assert main(["--paths", str(tmp_path)]) == 1


# ---------------------------------------------------------------------------
# An allowlisted file that is still over the ceiling passes cleanly, and is
# reported as allowlisted rather than silently invisible.
# ---------------------------------------------------------------------------


def test_allowlisted_file_still_over_ceiling_passes(monkeypatch, tmp_path: Path) -> None:
    planted = _write_lines(tmp_path, "waived.py", _CEILING + 100)
    monkeypatch.setattr(gate, "ALLOWLIST", (AllowlistEntry(path=str(planted), reason="probe: intentionally waived"),))
    assert main(["--paths", str(tmp_path)]) == 0


def test_exempt_file_is_never_a_violation_regardless_of_size(monkeypatch, tmp_path: Path) -> None:
    planted = _write_lines(tmp_path, "exempt.py", _CEILING * 5)
    monkeypatch.setattr(
        gate, "PERMANENT_EXEMPTIONS", (PermanentExemption(path=str(planted), reason="probe: permanently exempt"),)
    )
    monkeypatch.setattr(gate, "ALLOWLIST", ())
    assert main(["--paths", str(tmp_path)]) == 0


# ---------------------------------------------------------------------------
# Structural requirements on the two categories themselves.
# ---------------------------------------------------------------------------


def test_a_bare_allowlist_entry_with_no_reason_is_rejected() -> None:
    """AllowlistEntry.reason is a required constructor argument -- there is
    no call shape that produces a path with no reason."""
    import inspect

    sig = inspect.signature(AllowlistEntry)
    assert "reason" in sig.parameters
    assert sig.parameters["reason"].default is inspect.Parameter.empty


def test_permanent_exemptions_and_allowlist_are_disjoint() -> None:
    """The two categories must not name the same file -- that would leave it
    ambiguous whether the file is a permanent design decision or a drainable
    debt entry."""
    exempt_paths = {e.path for e in PERMANENT_EXEMPTIONS}
    allow_paths = {a.path for a in ALLOWLIST}
    assert exempt_paths.isdisjoint(allow_paths)


def test_every_current_entry_has_a_non_empty_reason() -> None:
    for e in PERMANENT_EXEMPTIONS:
        assert e.reason.strip(), f"{e.path} has an empty reason"
    for a in ALLOWLIST:
        assert a.reason.strip(), f"{a.path} has an empty reason"


def test_the_baseline_migration_is_a_permanent_exemption_not_a_ratchet_entry() -> None:
    """The curated DDL baseline is explicitly not a debt marker -- it is
    supposed to be this large forever, which is a different claim than
    "currently over, tracked for a future split"."""
    exempt_paths = {e.path for e in PERMANENT_EXEMPTIONS}
    allow_paths = {a.path for a in ALLOWLIST}
    assert any(p.endswith("0001_baseline_schema.py") for p in exempt_paths)
    assert not any(p.endswith("0001_baseline_schema.py") for p in allow_paths)


# ---------------------------------------------------------------------------
# The gate's own subject: the real repository, today.
# ---------------------------------------------------------------------------


def test_the_real_tree_passes_with_the_current_allowlist() -> None:
    """Proof that the shipped tree, scanned under the current ALLOWLIST and
    PERMANENT_EXEMPTIONS, is green -- not just that the gate's logic is
    correct in the abstract."""
    assert main([]) == 0


def test_the_arc_service_tree_carries_no_allowlist_or_exemption_entries() -> None:
    """Keeps the ARC-scoped strictness this gate grew out of from quietly
    loosening under the repo-wide generalisation: no file under
    contextplane/arc/service/ may appear in either category. If one
    ever needs to, that is a deliberate, reviewed act belonging in the same
    change that adds it -- not a default this test assumes away."""
    arc_prefix = "contextplane/arc/service/"
    for e in PERMANENT_EXEMPTIONS:
        assert not e.path.startswith(arc_prefix), f"{e.path} must not be exempt -- ARC service tree stays unwaived"
    for a in ALLOWLIST:
        assert not a.path.startswith(arc_prefix), f"{a.path} must not be allowlisted -- ARC service tree stays unwaived"
