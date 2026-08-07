"""Mechanised mutation proof for `RevisionIntegrityService.assess`'s five
verification axes.

The TDD states the rule this file exists to enforce literally: "Mutation
tests that remove any axis must fail." A test file that comments out a
check and asserts something fails is only meaningful if it proves *that
specific axis* was load-bearing -- eyeballing a hand-edited copy once and
moving on is exactly the kind of proof this repository's own precedent
(`scripts/check_arc_approval_writers.py`'s own planted-violation test,
`scripts/check_file_sizes.py`'s planted-oversized-file test) rejects in
favor of a mechanised plant/fail/remove/pass cycle that runs on every CI
invocation, not once by hand.

**What this file actually does, for each of the five axes.** `registry/arc/
service/integrity.py` brackets every axis's decision in a `# mutation-axis:
<name>` / `# end-mutation-axis: <name>` comment pair around an always-
uniform two-line guard (`if <result> is ...: return <result>`) -- see that
module's own docstring for why the guard, not the helper's body, is the
mutated surface. For each axis, this file:

1. reads the module's *real, on-disk* source once;
2. mechanically replaces that one axis's bracketed two-line guard with a
   single `pass` at the same indentation -- a real edit to the real file,
   not a copy read into a sandboxed import, so a mismatched sentinel or a
   guard that quietly moved to a different file cannot pass by accident;
3. runs `tests/unit/test_arc_integrity.py` in a fresh subprocess against
   that mutated file and asserts it now fails, where it passed cleanly
   before any mutation ran;
4. restores the file's exact original bytes, in a `finally` that runs even
   if the assertion above raises, and asserts the restored bytes are
   byte-identical to what was read in step 1;
5. re-runs the same test file once more against the restored source and
   asserts it is clean again -- the "remove, pass" half of the cycle, not
   merely "the tree looks the same."

A subprocess (not an in-process re-import) is what makes step 3 trustworthy
under a mutation that changes the file `test_arc_integrity.py` itself
already imported once in this test process's own collection pass --
Python caches modules by name, so a second in-process `import` of a
mutated `integrity.py` would silently keep serving the first, unmutated
module object. `sys.executable` is the exact interpreter already running
this suite, so the subprocess is the same virtualenv, no ambient
`DATABASE_URL` or other global export required -- these are unit tests,
they open no database connection.

**Byte-identical restoration matters operationally, not just cosmetically.**
An earlier mutation-proof attempt elsewhere in this phase lost real work to
exactly this kind of edit-and-restore cycle; this file treats "restored
correctly" as something to prove on every run, not something to eyeball
once after writing it.
"""

from __future__ import annotations

import pathlib
import re
import subprocess  # noqa: S404 - fixed argv below (sys.executable + this repo's own test path), no caller input
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_INTEGRITY_MODULE = _REPO_ROOT / "registry" / "arc" / "service" / "integrity.py"
_UNIT_TEST_TARGET = "tests/unit/test_arc_integrity.py"

# The exact five axis names `integrity.py`'s own sentinel comments use --
# transcribed here, not derived from the file, so a sentinel accidentally
# renamed or dropped in that file fails this file's own manifest-
# completeness test below rather than silently shrinking the axis list both
# files agree on.
AXES = (
    "source_status",
    "cached_state",
    "projection_evidence",
    "operational_chain",
    "durable_checkpoint",
)


def _run_unit_tests() -> tuple[int, int, str]:
    """Runs the real unit test file in a fresh subprocess (see the module
    docstring for why this must not be an in-process import) and returns
    `(failed_count, passed_count, combined_output)`.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", _UNIT_TEST_TARGET, "-q", "--no-header"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    failed = 0
    passed = 0
    failed_match = re.search(r"(\d+) failed", output)
    passed_match = re.search(r"(\d+) passed", output)
    if failed_match:
        failed = int(failed_match.group(1))
    if passed_match:
        passed = int(passed_match.group(1))
    return failed, passed, output


def _neutralise_axis(source: str, axis: str) -> str:
    """Replace *axis*'s bracketed two-line guard with a single `pass` at
    the same indentation as the opening sentinel comment -- syntactically
    valid regardless of the guard's own exact shape, and a mechanical
    stand-in for "comment out this check" that a bare `#`-per-line could
    not achieve for a multi-line construct.
    """
    start_marker = f"# mutation-axis: {axis}"
    end_marker = f"# end-mutation-axis: {axis}"
    if start_marker not in source or end_marker not in source:
        msg = f"axis {axis!r} sentinel markers not found in {_INTEGRITY_MODULE}"
        raise AssertionError(msg)
    start = source.index(start_marker)
    end = source.index(end_marker)
    line_start = source.rfind("\n", 0, start) + 1
    indent = source[line_start:start]
    end_line_end = source.index("\n", end) + 1
    return source[:line_start] + f"{indent}pass  # mutated: {axis} axis removed\n" + source[end_line_end:]


def test_every_axis_has_exactly_one_sentinel_pair_in_the_real_module() -> None:
    """The manifest-completeness check: before mutating anything, prove
    every axis this file claims to cover actually has a well-formed marker
    pair in the real file, exactly once. A sentinel renamed, duplicated, or
    dropped in `integrity.py` fails here first, at collection-adjacent time,
    rather than silently making a later mutation test vacuous (its own
    `start_marker not in source` guard would raise, but this test names the
    defect on its own, independent of that one running at all).
    """
    source = _INTEGRITY_MODULE.read_text(encoding="utf-8")
    for axis in AXES:
        start_marker = f"# mutation-axis: {axis}"
        end_marker = f"# end-mutation-axis: {axis}"
        assert source.count(start_marker) == 1, f"expected exactly one {start_marker!r} in integrity.py"
        assert source.count(end_marker) == 1, f"expected exactly one {end_marker!r} in integrity.py"
        assert source.index(start_marker) < source.index(end_marker)


def test_the_clean_unit_suite_passes_before_any_mutation_is_attempted() -> None:
    """The baseline every per-axis test below compares against. If this
    does not pass, every "removing the axis makes it fail" claim afterward
    is meaningless."""
    failed, passed, output = _run_unit_tests()
    assert failed == 0, f"the unmutated suite already fails:\n{output}"
    assert passed > 0, f"the unmutated suite collected no passing tests:\n{output}"


def _assert_axis_is_load_bearing(axis: str) -> None:
    """The full plant/fail/remove/pass cycle for one axis, against the
    real, on-disk module -- see the module docstring for why each step is
    shaped the way it is.
    """
    original = _INTEGRITY_MODULE.read_text(encoding="utf-8")
    baseline_failed, baseline_passed, baseline_output = _run_unit_tests()
    assert baseline_failed == 0, f"baseline must be clean before mutating {axis!r}:\n{baseline_output}"

    try:
        mutated = _neutralise_axis(original, axis)
        assert mutated != original, f"neutralising {axis!r} produced no change"
        _INTEGRITY_MODULE.write_text(mutated, encoding="utf-8")

        mutated_failed, mutated_passed, mutated_output = _run_unit_tests()
        assert mutated_failed >= 1, (
            f"removing axis {axis!r} left the unit suite fully green -- this axis has no test "
            f"behind it, or the mutation did not change the behavior a test depends on:\n{mutated_output}"
        )
        assert mutated_passed < baseline_passed, (
            f"removing axis {axis!r} did not reduce the passing count "
            f"({mutated_passed} vs baseline {baseline_passed}):\n{mutated_output}"
        )
    finally:
        # Restore before any further assertion so a failure above still
        # leaves the tree clean -- the `finally` runs whether the mutated
        # run's own asserts passed or raised.
        _INTEGRITY_MODULE.write_text(original, encoding="utf-8")

    restored = _INTEGRITY_MODULE.read_text(encoding="utf-8")
    assert restored == original, f"restoring axis {axis!r} did not reproduce the original bytes exactly"

    restored_failed, restored_passed, restored_output = _run_unit_tests()
    assert restored_failed == 0, f"the suite is not clean again after restoring axis {axis!r}:\n{restored_output}"
    assert restored_passed == baseline_passed, (
        f"the restored suite's passing count ({restored_passed}) does not match the pre-mutation "
        f"baseline ({baseline_passed}) for axis {axis!r}:\n{restored_output}"
    )


def test_removing_the_source_status_axis_makes_the_suite_fail() -> None:
    _assert_axis_is_load_bearing("source_status")


def test_removing_the_cached_state_axis_makes_the_suite_fail() -> None:
    _assert_axis_is_load_bearing("cached_state")


def test_removing_the_projection_evidence_axis_makes_the_suite_fail() -> None:
    _assert_axis_is_load_bearing("projection_evidence")


def test_removing_the_operational_chain_axis_makes_the_suite_fail() -> None:
    _assert_axis_is_load_bearing("operational_chain")


def test_removing_the_durable_checkpoint_axis_makes_the_suite_fail() -> None:
    _assert_axis_is_load_bearing("durable_checkpoint")


def test_the_tree_is_clean_after_every_axis_has_been_mutated_and_restored() -> None:
    """The last word: whatever ran above (in any order pytest chooses),
    the file on disk right now must still be exactly what a fresh read
    produces no differently from every other test in this file's own
    reads -- i.e. no axis's `finally` silently failed to restore. Compares
    against a second, independent read rather than a cached string, so a
    partially-written file (a crash mid-`write_text`) would show up as a
    syntax error in this file's own `ast.parse`, not merely as a string
    mismatch against something already in memory.
    """
    import ast

    source = _INTEGRITY_MODULE.read_text(encoding="utf-8")
    ast.parse(source, filename=str(_INTEGRITY_MODULE))
    for axis in AXES:
        assert f"# mutation-axis: {axis}" in source
        assert f"# end-mutation-axis: {axis}" in source
        assert "# mutated:" not in source
