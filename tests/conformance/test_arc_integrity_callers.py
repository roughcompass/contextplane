"""§6.3 read-path conformance gate: every named caller's own decision
function reaches `RevisionIntegrityService.assess`.

`RevisionIntegrityService` was built with no production caller at first,
and its own structural test (`tests/unit/test_arc_integrity.py`) proved
that absence by scanning four files for any reference to the class at
all. That
was the right test for that commit; it is the wrong test once wiring
lands, because "references the class" is satisfied by an import that is
never called. This file is what actually proves the call happens, at the
one place in each module the TDD names as that module's own §6.3 decision:

| Caller | Module | Decision entry point |
|---|---|---|
| activation | `arc/service/activation.py` | `ActivationService._evaluate` |
| mandatory corpus assembly | `arc/service/corpus.py` | `CorpusReader.assemble` |
| new context selection | `arc/service/selection.py` | `select_and_verify` |
| protected-action authorization | `arc/service/authorization.py` | `assert_protected_action_authorized` |

**Why one named entry point per file, not "every function in the file."**
Every one of these four files also carries functions that legitimately
never touch integrity: `selection.py`'s own `select()` is pure by
contract (see that function's own docstring on why a database call would
break its determinism guarantee) and is a *collaborator* `select_and_
verify` wraps, not a second decision path; `activation.py`'s `revoke()`
only ever reduces trust, so gating it on integrity would be backwards;
`authorization.py`'s scope/role gates (`assert_can_write_artifact` and
its siblings) decide whether an actor may touch ARC's own authoring
surface at all, a question with no revision in play. Naming the real
decision point directly, rather than sweeping every function in the file
and exempting the rest, keeps this gate honest about what it actually
checks instead of hiding behind a growing exemption list.

**Why this still catches a future caller, not just today's four call
sites.** The mechanism below (`entry_point_reaches_assess`) is a call-graph
walker, not a fixed line-number or string match: it follows local
(same-module) function/method calls transitively, so a decision that
delegates through several private helpers before (not) reaching `assess`
is still caught, matching `check_arc_approval_writers.py`'s own AST
call-site discipline and `test_arc_no_orm_bypass.py`'s own precision-then-
strictness testing shape. The tests in the second half of this file prove
the walker itself catches a planted violation -- direct, indirect, and
shaped like a brand-new module nobody has written yet -- against synthetic
sources, exactly the "not a test that merely confirms today's four call
sites exist" property this gate is required to have. Mutating the real,
on-disk production files in a test that runs on every CI invocation (the
`test_arc_integrity_mutation.py`/`test_arc_activation_mutation.py` style)
is deliberately not used here: that style exists to prove a *single-axis
guard* is load-bearing inside one already-known function, not to discover
whether some *other*, unnamed function in the file also needs checking --
a synthetic fixture proves the walker's own generality more directly and
without touching files this repo ships.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CONTEXTPLANE_ROOT = _REPO_ROOT / "contextplane"

#: The TDD's own four-row table, transcribed once. `test_arc_integrity_
#: callers_match_the_wired_reference_scan` cross-checks this against
#: `tests/unit/test_arc_integrity.py`'s own four-file list, so the two
#: files cannot silently drift onto different sets of "the four callers".
ENTRY_POINTS: dict[str, str] = {
    "arc/service/activation.py": "_evaluate",
    "arc/service/corpus.py": "assemble",
    "arc/service/selection.py": "select_and_verify",
    "arc/service/authorization.py": "assert_protected_action_authorized",
}


# ---------------------------------------------------------------------------
# The walker.
# ---------------------------------------------------------------------------


def _functions_by_name(tree: ast.AST) -> dict[str, ast.AST]:
    """Every function/method defined anywhere in *tree*, keyed by its bare
    name. Receiver- and class-agnostic on purpose -- see the module
    docstring for why that over-approximation is the right shape here,
    matching `test_arc_no_orm_bypass.py`'s own ORM-write walker.
    """
    functions: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions[node.name] = node
    return functions


def _calls_assess(node: ast.AST) -> bool:
    """Whether *node*'s own body (not anything it calls) contains a call
    shaped `<anything>.assess(...)`."""
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "assess"
        for n in ast.walk(node)
    )


def _local_call_names(node: ast.AST) -> set[str]:
    """Every function/method name *node*'s body calls -- `bare_name(...)`
    and `receiver.name(...)` alike, the receiver never checked (the same
    reason `find_orm_writes` in `test_arc_no_orm_bypass.py` does not check
    one either: a helper holding the collaborator under a different name
    calls it just the same).
    """
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func = n.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def entry_point_reaches_assess(source: str, entry_name: str) -> bool:
    """Whether *entry_name* reaches a call shaped `<something>.assess(...)`,
    directly or through any number of local (same-source) function/method
    calls -- a call-graph reachability search, not a single-line match.

    Raises `AssertionError` if *entry_name* is not defined anywhere in
    *source* at all, rather than silently returning `False`: a renamed or
    removed entry point is a defect in this gate's own expectations, not a
    caller that failed the check.
    """
    tree = ast.parse(source)
    functions = _functions_by_name(tree)
    if entry_name not in functions:
        msg = f"{entry_name!r} is not defined anywhere in this source"
        raise AssertionError(msg)

    seen: set[str] = set()
    queue = [entry_name]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        node = functions.get(name)
        if node is None:
            # A call to something not defined in this same source (a
            # cross-module helper, a stdlib method sharing the name) --
            # nothing further to walk down this branch.
            continue
        if _calls_assess(node):
            return True
        queue.extend(target for target in _local_call_names(node) if target not in seen)
    return False


# ---------------------------------------------------------------------------
# The real four callers.
# ---------------------------------------------------------------------------


def test_every_caller_entry_point_reaches_assess() -> None:
    for relative, entry_name in ENTRY_POINTS.items():
        source = (_CONTEXTPLANE_ROOT / relative).read_text(encoding="utf-8")
        assert entry_point_reaches_assess(source, entry_name), (
            f"{relative}::{entry_name} does not reach a call to `.assess(...)` -- the §6.3 decision this "
            "entry point makes is not integrity-checked"
        )


def test_arc_integrity_callers_match_the_wired_reference_scan() -> None:
    """`tests/unit/test_arc_integrity.py::test_every_wired_caller_references_
    revision_integrity_service` asserts these same four relative paths
    reference `RevisionIntegrityService` at all; this file asserts they
    each *call* `assess` at a named decision point. Cross-checking the
    path sets here catches the two files silently drifting onto different
    ideas of "the four callers.".
    """
    watched_name = "RevisionIntegrityService"
    for relative in ENTRY_POINTS:
        path = _CONTEXTPLANE_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        referenced = any(
            (isinstance(n, ast.Name) and n.id == watched_name)
            or (isinstance(n, ast.Attribute) and n.attr == watched_name)
            or (isinstance(n, ast.alias) and (n.name == watched_name or n.asname == watched_name))
            or (isinstance(n, ast.ImportFrom) and n.module is not None and n.module.rsplit(".", 1)[-1] == "integrity")
            for n in ast.walk(tree)
        )
        assert referenced, f"{relative} is in ENTRY_POINTS but never references RevisionIntegrityService/integrity"


# ---------------------------------------------------------------------------
# The walker's own precision and generality, proven against synthetic
# sources -- not the real files. See the module docstring for why.
# ---------------------------------------------------------------------------

_DIRECT_CALL = """
class Reader:
    def assemble(self, session, revision_id):
        return self._integrity.assess(session, revision_id, "corpus_assembly")
"""

_ONE_LEVEL_OF_INDIRECTION = """
class Reader:
    def assemble(self, session, revision_id):
        return self._check(session, revision_id)

    def _check(self, session, revision_id):
        return self._integrity.assess(session, revision_id, "corpus_assembly")
"""

_TWO_LEVELS_OF_INDIRECTION = """
class Reader:
    def assemble(self, session, revision_id):
        return self._step_one(session, revision_id)

    def _step_one(self, session, revision_id):
        return self._step_two(session, revision_id)

    def _step_two(self, session, revision_id):
        return self._integrity.assess(session, revision_id, "corpus_assembly")
"""

_NO_CHECK_AT_ALL = """
class Reader:
    def assemble(self, session, revision_id):
        return "served without a check"
"""

_INDIRECTION_THAT_NEVER_ARRIVES = """
class Reader:
    def assemble(self, session, revision_id):
        return self._step_one(session, revision_id)

    def _step_one(self, session, revision_id):
        return self._step_two(session, revision_id)

    def _step_two(self, session, revision_id):
        return "served without a check"
"""


def test_the_walker_passes_a_direct_call() -> None:
    assert entry_point_reaches_assess(_DIRECT_CALL, "assemble") is True


def test_the_walker_passes_one_level_of_local_indirection() -> None:
    assert entry_point_reaches_assess(_ONE_LEVEL_OF_INDIRECTION, "assemble") is True


def test_the_walker_passes_two_levels_of_local_indirection() -> None:
    """`corpus.py`'s own real shape: `assemble` -> `_drop_integrity_failed`
    -> `assess`. This is the synthetic proof the same two-hop shape is
    caught in general, not merely in that one file."""
    assert entry_point_reaches_assess(_TWO_LEVELS_OF_INDIRECTION, "assemble") is True


def test_the_walker_catches_a_direct_decision_with_no_check_at_all() -> None:
    assert entry_point_reaches_assess(_NO_CHECK_AT_ALL, "assemble") is False


def test_the_walker_catches_indirection_that_never_arrives_at_a_check() -> None:
    """The property item 3 asks for, proven mechanically rather than
    asserted: a decision that delegates through two private helpers and
    *still* never calls `assess` is caught, exactly as a real, careless
    fifth caller would be."""
    assert entry_point_reaches_assess(_INDIRECTION_THAT_NEVER_ARRIVES, "assemble") is False


def test_planting_and_then_removing_the_violation_is_a_clean_round_trip() -> None:
    """The "plant it, confirm it fails, remove it, confirm clean" cycle
    this task's own verification requires, run against the walker's two
    fixture sources directly: a would-be caller that fails the check, and
    the same caller with the check restored.
    """
    assert entry_point_reaches_assess(_NO_CHECK_AT_ALL, "assemble") is False
    assert entry_point_reaches_assess(_DIRECT_CALL, "assemble") is True


def test_a_hypothetical_fifth_caller_added_without_the_check_is_caught() -> None:
    """The property this whole file exists for: a brand-new module reaching
    a decision-shaped entry point without ever calling `assess` fails,
    exactly like a real fifth caller would -- this is not limited to
    catching regressions in the four files this gate already names.
    """
    hypothetical_new_caller = """
class NewReader:
    def resolve_for_serving(self, session, revision_id):
        return "served without ever asking RevisionIntegrityService"
"""
    assert entry_point_reaches_assess(hypothetical_new_caller, "resolve_for_serving") is False


def test_a_renamed_or_missing_entry_point_raises_rather_than_silently_passing() -> None:
    """A gate that answered "not found, so nothing to check" would pass
    trivially the moment an entry point was renamed -- exactly the failure
    mode a silently-widened gate produces elsewhere in this repo's own
    precedents (`check_file_sizes.py`'s allowlist, `check_arc_approval_
    writers.py`'s writer list). This must fail loudly instead.
    """
    with pytest.raises(AssertionError, match="not defined anywhere"):
        entry_point_reaches_assess(_DIRECT_CALL, "a_name_nothing_defines")
