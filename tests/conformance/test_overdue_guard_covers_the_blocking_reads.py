"""Which reads must refuse while blocking propagation is overdue, derived rather than listed.

E3-T8. This replaces a prose list in `context/arms.py` that had been wrong three
times, and it exists because the third time was found the same way as the first
two: somebody went looking for the *set* and found the list did not describe it.

**The rule.** A read must be guarded exactly when it serves a column a
*blocking* derivative handler rewrites. Two kinds register `blocking=True`:

- `receipt_link` — `UPDATE context_receipt_items SET item_key = :marker` and
  the same on `context_receipt_exclusions`. A read returning either table's rows
  before propagation lands serves a key the propagation is withdrawing.
- `claim_derivative` — refuses `rebuild` outright, so an erasure that should
  have withdrawn a claim has not, and the claim is still servable.

**What the old list got wrong, in both directions.** It named
`ContextReceiptService.get` as serving `item_key`: it does not — that returns the
`context_receipts` header, and items and exclusions are separate tables, so a
guard there could not fire. It omitted `arms_for` entirely, which is correct but
was never stated, so the next reader had to re-derive it. And it correctly named
`exclusions_for`, which was genuinely unguarded — while
`service/memory/derivative_handlers.py` simultaneously described that same
surface as *"a deliberate answer recorded at the arms rather than an omission"*.

Two hand-maintained lists disagreeing about one surface is how it stayed
unguarded. This file is the answer to "can the set be derived", and the answer
is: the *mandatory* half can.

**What is deliberately not derived.** A guard that is present but not required
by the rule is not a failure here. `canonical_arm`'s entry in `arms.py` records
the opposite case — a guard that *cannot fire* is refused rather than added —
but a read may still be guarded on judgement about a kind that is not blocking
today. Asserting the set both ways would turn a lower bound into a straitjacket,
and the failure this task exists to prevent is a missing guard, not a spare one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).parent.parent.parent / "contextplane"

#: The guard, and the local wrappers that reach it. Names rather than a
#: substring, because these are matched against *call nodes*: the first version
#: of this file asked whether the source contained "pending_overdue" and got a
#: yes from `exclusions_for`'s own docstring, which mentions the guard while
#: explaining it. A check that reads prose as evidence of behaviour is the
#: failure this file exists to end, and it managed to commit it twice.
_GUARD_CALLS: frozenset[str] = frozenset({"pending_overdue", "_refuse_if_overdue"})

#: Tables a blocking derivative handler rewrites. Derived by reading the
#: handlers rather than restated: `_blocking_tables` below extracts them, and
#: this is the expectation it is checked against, so a new blocking handler
#: fails here rather than silently widening the set nobody re-derived.
_EXPECTED_BLOCKING_TABLES: frozenset[str] = frozenset({"context_receipt_items", "context_receipt_exclusions"})

#: Modules whose reads serve those tables and must therefore ask the guard.
#: A path, not a list of functions: the unit that either reaches the guard or
#: does not is the module, and a per-function list is the thing that went stale.
_MUST_GUARD: frozenset[str] = frozenset({"context/receipts.py"})

#: Reads over the same tables that are *writers*, not serving paths. A writer
#: cannot serve a withdrawn key to anybody, and blocking a write while
#: propagation is late would stall the very queue that is behind.
_WRITERS: frozenset[str] = frozenset({"context/derivative_handlers.py"})

#: Declares the tables and reads nothing. The ORM module is mapped-column
#: definitions with no session and no query, so there is no read to guard.
_DECLARES: frozenset[str] = frozenset({"context/models_receipt.py"})

_TABLE_RE = re.compile(r"\bcontext_receipt_(?:items|exclusions)\b")


def _blocking_tables() -> set[str]:
    """The tables the blocking handlers actually rewrite, read out of them.

    Extracted rather than assumed, so that a handler gaining or losing
    `blocking=True` moves this set and fails the pin below instead of leaving
    this whole file checking a rule that stopped being the rule.
    """
    found: set[str] = set()
    for path in (_PACKAGE / "context" / "derivative_handlers.py",):
        source = path.read_text(encoding="utf-8")
        if "blocking=True" not in source:
            continue
        found |= set(_TABLE_RE.findall(source))
    return found


def test_the_blocking_handlers_still_rewrite_the_tables_this_rule_is_built_on() -> None:
    """The premise, checked before anything is derived from it.

    If a blocking handler starts rewriting a third table, every conclusion below
    is about a smaller set than the real one — and it would keep passing, which
    is the failure mode this whole file was written to end.
    """
    assert _blocking_tables() == set(_EXPECTED_BLOCKING_TABLES), (
        f"the blocking handlers rewrite {sorted(_blocking_tables())}, but this rule is built on "
        f"{sorted(_EXPECTED_BLOCKING_TABLES)}. A new one means a new module may need the guard."
    )


def _without_docstrings(source: str) -> str:
    """The module with every docstring removed.

    Necessary rather than fastidious: the first version of this check matched
    `arms.py`, which names both tables only while *explaining* this rule, and
    flagged it as an unclassified reader. A check that cannot tell a query from
    a sentence about queries would grow a second stale list of files that merely
    mention the subject -- which is the failure it was written to end.
    """
    tree = ast.parse(source)
    spans = {
        node.body[0].value.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    ends = {
        node.body[0].value.end_lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    drop: set[int] = set()
    for start, end in zip(sorted(spans), sorted(e for e in ends if e is not None), strict=False):
        drop |= set(range(start, end + 1))
    return "\n".join("" if number in drop else line for number, line in enumerate(source.splitlines(), start=1))


def _modules_touching(tables: frozenset[str]) -> set[str]:
    """Every module under `contextplane/` whose *code* names one of these tables."""
    hits: set[str] = set()
    for path in _PACKAGE.rglob("*.py"):
        if "__pycache__" in path.parts or "migrations" in path.parts:
            continue
        if _TABLE_RE.search(_without_docstrings(path.read_text(encoding="utf-8"))):
            hits.add(str(path.relative_to(_PACKAGE)))
    return hits


def test_every_module_serving_a_minimized_table_is_accounted_for() -> None:
    """No module reads these tables without this file having an opinion about it.

    The list that preceded this one went stale because a new reader could be
    added without anybody updating it. Here a new reader fails until it is
    classified as a serving path that must guard, or as a writer that must not.
    """
    touching = _modules_touching(_EXPECTED_BLOCKING_TABLES)
    unclassified = touching - _MUST_GUARD - _WRITERS - _DECLARES
    assert not unclassified, (
        f"these modules read a table a blocking handler minimizes and are on neither list: "
        f"{sorted(unclassified)}. Add each to _MUST_GUARD (a serving path, and then guard it) or "
        "to _WRITERS (it writes rather than serves, so a guard would stall the queue it waits on), "
        "or to _DECLARES (it defines the tables and runs no query)."
    )
    assert touching, "no module reads these tables, so this test checked nothing"


def _calls_the_guard(node: ast.AST) -> bool:
    """Whether this node actually *calls* the guard, docstrings notwithstanding."""
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in _GUARD_CALLS:
            return True
    return False


@pytest.mark.parametrize("module", sorted(_MUST_GUARD))
def test_a_module_serving_a_minimized_table_reaches_the_guard(module: str) -> None:
    """The property itself. `exclusions_for` was the read this caught."""
    tree = ast.parse((_PACKAGE / module).read_text(encoding="utf-8"))
    assert _calls_the_guard(tree), (
        f"{module} serves a table a blocking derivative handler minimizes and never calls "
        f"one of {sorted(_GUARD_CALLS)}. A read that can return a key the propagation is "
        "withdrawing has to refuse while that propagation is past due."
    )


def test_the_two_receipt_reads_that_need_no_guard_are_named_as_such() -> None:
    """The other half of the finding, pinned so it is not 'fixed' into a no-op.

    `get` returns the `context_receipts` header and `arms_for` returns
    `context_receipt_arms`. Neither table is rewritten by a blocking handler, so
    a guard on either could not fire — which is exactly what `canonical_arm`'s
    entry in `arms.py` refuses to add, and what E6-T4 built once and reverted.

    Asserted by reading the functions rather than the module, because the module
    *does* contain the guard now: a module-level check would pass whatever these
    two do.
    """
    tree = ast.parse((_PACKAGE / "context" / "receipts.py").read_text(encoding="utf-8"))
    reads = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in {"get", "arms_for", "exclusions_for"}
    }
    assert set(reads) == {"get", "arms_for", "exclusions_for"}, f"expected all three reads, found {sorted(reads)}"

    for name in ("get", "arms_for"):
        assert not _calls_the_guard(reads[name]), (
            f"ContextReceiptService.{name} now calls the guard, but it serves no column a blocking "
            "handler rewrites, so that guard cannot fire. A check that cannot trigger reads as "
            "protection and is not."
        )
    assert _calls_the_guard(reads["exclusions_for"]), (
        "ContextReceiptService.exclusions_for stopped calling the guard; it serves the `item_key` "
        "that `receipt_link` minimizes, which is the one read on this surface that needs it."
    )
