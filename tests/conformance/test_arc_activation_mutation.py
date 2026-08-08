"""Mechanised mutation proof for the ten named activation predicates
(`contextplane/arc/service/activation_predicates.py`).

Same mechanism as `test_arc_integrity_mutation.py`, which this file follows
directly (bracketed markers, mutation applied to the real on-disk module,
suite run in a subprocess, exact bytes restored in a `finally`, byte-
identity asserted) -- see that file's own module docstring for the full
rationale behind each step. This file targets the fixed `PREDICATE_ORDER`
names instead of the five integrity axes, and `tests/unit/test_arc_
activation.py` instead of `test_arc_integrity.py`.

**Why a subprocess, not an in-process re-import.** Python caches modules by
name, so a second in-process `import` of a mutated `activation_predicates.py`
would silently keep serving the first, unmutated module object -- an
in-process check would report every mutation as "caught" whether or not the
guard actually ran. `sys.executable` is the exact interpreter already
running this suite, so the subprocess is the same virtualenv, no ambient
`DATABASE_URL` or other global export required -- these are unit tests, they
open no database connection.

**Byte-identical restoration matters operationally, not just cosmetically.**
An earlier mutation-proof attempt elsewhere in this phase lost real work to
exactly this kind of edit-and-restore cycle; this file treats "restored
correctly" as something to prove on every run, not something to eyeball once
after writing it.
"""

from __future__ import annotations

import pathlib
import re
import subprocess  # noqa: S404 - fixed argv below (sys.executable + this repo's own test path), no caller input
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PREDICATES_MODULE = _REPO_ROOT / "contextplane" / "arc" / "service" / "activation_predicates.py"
_UNIT_TEST_TARGET = "tests/unit/test_arc_activation.py"

# The exact ten predicate names `activation_predicates.py`'s own sentinel
# comments use -- transcribed here, not derived from the file, so a
# sentinel accidentally renamed or dropped in that file fails this file's
# own manifest-completeness test below rather than silently shrinking the
# predicate list both files agree on. Matches `PREDICATE_ORDER`'s own
# fixed §2.2 sequence.
AXES = (
    "latest_version",
    "state_approved",
    "digest_chain",
    "baseline_current",
    "source_valid",
    "risk_reproducible",
    "observation_qualified",
    "projection_evidence_valid",
    "actor_separation",
    "operational_integrity",
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
    """Replace *axis*'s bracketed guard with a single `pass` at the same
    indentation as the opening sentinel comment -- syntactically valid
    regardless of the guard's own exact shape.
    """
    start_marker = f"# mutation-axis: {axis}"
    end_marker = f"# end-mutation-axis: {axis}"
    if start_marker not in source or end_marker not in source:
        msg = f"axis {axis!r} sentinel markers not found in {_PREDICATES_MODULE}"
        raise AssertionError(msg)
    start = source.index(start_marker)
    end = source.index(end_marker)
    line_start = source.rfind("\n", 0, start) + 1
    indent = source[line_start:start]
    end_line_end = source.index("\n", end) + 1
    return source[:line_start] + f"{indent}pass  # mutated: {axis} axis removed\n" + source[end_line_end:]


def test_every_predicate_has_exactly_one_sentinel_pair_in_the_real_module() -> None:
    """The manifest-completeness check: before mutating anything, prove
    every predicate this file claims to cover actually has a well-formed
    marker pair in the real file, exactly once."""
    source = _PREDICATES_MODULE.read_text(encoding="utf-8")
    for axis in AXES:
        start_marker = f"# mutation-axis: {axis}"
        end_marker = f"# end-mutation-axis: {axis}"
        assert source.count(start_marker) == 1, f"expected exactly one {start_marker!r} in activation_predicates.py"
        assert source.count(end_marker) == 1, f"expected exactly one {end_marker!r} in activation_predicates.py"
        assert source.index(start_marker) < source.index(end_marker)


def test_the_clean_unit_suite_passes_before_any_mutation_is_attempted() -> None:
    """The baseline every per-predicate test below compares against. If
    this does not pass, every "removing the predicate makes it fail" claim
    afterward is meaningless."""
    failed, passed, output = _run_unit_tests()
    assert failed == 0, f"the unmutated suite already fails:\n{output}"
    assert passed > 0, f"the unmutated suite collected no passing tests:\n{output}"


def _assert_predicate_is_load_bearing(axis: str) -> None:
    """The full plant/fail/remove/pass cycle for one predicate, against the
    real, on-disk module -- see the module docstring for why each step is
    shaped the way it is.
    """
    original = _PREDICATES_MODULE.read_text(encoding="utf-8")
    baseline_failed, baseline_passed, baseline_output = _run_unit_tests()
    assert baseline_failed == 0, f"baseline must be clean before mutating {axis!r}:\n{baseline_output}"

    try:
        mutated = _neutralise_axis(original, axis)
        assert mutated != original, f"neutralising {axis!r} produced no change"
        _PREDICATES_MODULE.write_text(mutated, encoding="utf-8")

        mutated_failed, mutated_passed, mutated_output = _run_unit_tests()
        assert mutated_failed >= 1, (
            f"removing predicate {axis!r} left the unit suite fully green -- this predicate has no test "
            f"behind it, or the mutation did not change the behavior a test depends on:\n{mutated_output}"
        )
        assert mutated_passed < baseline_passed, (
            f"removing predicate {axis!r} did not reduce the passing count "
            f"({mutated_passed} vs baseline {baseline_passed}):\n{mutated_output}"
        )
    finally:
        # Restore before any further assertion so a failure above still
        # leaves the tree clean -- the `finally` runs whether the mutated
        # run's own asserts passed or raised.
        _PREDICATES_MODULE.write_text(original, encoding="utf-8")

    restored = _PREDICATES_MODULE.read_text(encoding="utf-8")
    assert restored == original, f"restoring predicate {axis!r} did not reproduce the original bytes exactly"

    restored_failed, restored_passed, restored_output = _run_unit_tests()
    assert restored_failed == 0, f"the suite is not clean again after restoring predicate {axis!r}:\n{restored_output}"
    assert restored_passed == baseline_passed, (
        f"the restored suite's passing count ({restored_passed}) does not match the pre-mutation "
        f"baseline ({baseline_passed}) for predicate {axis!r}:\n{restored_output}"
    )


def test_removing_the_latest_version_predicate_makes_the_suite_fail() -> None:
    _assert_predicate_is_load_bearing("latest_version")


def test_removing_the_state_approved_predicate_makes_the_suite_fail() -> None:
    _assert_predicate_is_load_bearing("state_approved")


def test_removing_the_digest_chain_predicate_makes_the_suite_fail() -> None:
    _assert_predicate_is_load_bearing("digest_chain")


def test_removing_the_baseline_current_predicate_makes_the_suite_fail() -> None:
    _assert_predicate_is_load_bearing("baseline_current")


def test_removing_the_source_valid_predicate_makes_the_suite_fail() -> None:
    _assert_predicate_is_load_bearing("source_valid")


def test_removing_the_risk_reproducible_predicate_makes_the_suite_fail() -> None:
    _assert_predicate_is_load_bearing("risk_reproducible")


def test_removing_the_observation_qualified_predicate_makes_the_suite_fail() -> None:
    _assert_predicate_is_load_bearing("observation_qualified")


def test_removing_the_projection_evidence_valid_predicate_makes_the_suite_fail() -> None:
    _assert_predicate_is_load_bearing("projection_evidence_valid")


def test_removing_the_actor_separation_predicate_makes_the_suite_fail() -> None:
    _assert_predicate_is_load_bearing("actor_separation")


def test_removing_the_operational_integrity_predicate_makes_the_suite_fail() -> None:
    """The hard-wired predicate is not exempt: flipping its bracket to
    `pass` leaves `satisfied`/`reason_code` referenced before assignment
    inside `check_operational_integrity`, which is itself a real, caught
    failure -- proving this predicate's own bracket, not merely its
    surrounding function signature, is what the unit suite depends on.
    """
    _assert_predicate_is_load_bearing("operational_integrity")


def test_the_tree_is_clean_after_every_predicate_has_been_mutated_and_restored() -> None:
    """The last word: whatever ran above (in any order pytest chooses),
    the file on disk right now must still be exactly what a fresh read
    produces -- i.e. no predicate's `finally` silently failed to restore.
    """
    import ast

    source = _PREDICATES_MODULE.read_text(encoding="utf-8")
    ast.parse(source, filename=str(_PREDICATES_MODULE))
    for axis in AXES:
        assert f"# mutation-axis: {axis}" in source
        assert f"# end-mutation-axis: {axis}" in source
        assert "# mutated:" not in source
