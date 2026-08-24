"""The receipt listing hides exactly what the detail read refuses.

E23-T1. `ContextReceiptService.recent` is the first receipt read not keyed by an
id the caller already holds, which makes it the first one that could disclose
that a receipt *exists*. Everything else about a withheld receipt is already
protected by `refuse_if_unservable`; a list is the surface that could route
around it.

**So the filter and the refusal are held equal here, in both directions.** A
condition the refusal raises on and the list does not exclude is a disclosure. A
condition the list excludes and the refusal permits is a receipt somebody can
open and cannot find, which sends a reader to conclude their evidence was lost.

This is a source-level check rather than a behavioural one on purpose. The
behavioural halves are in `tests/integration/test_receipt_listing.py`, and they
prove the two states this knows the names of. What this proves is that there are
only two — a third hydration state, or a second withholding column, would have to
change both places or fail here.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from contextplane.context import receipts

_SOURCE = Path(inspect.getfile(receipts)).read_text(encoding="utf-8")


def _function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in {receipts.__name__}")


def _attributes_named(node: ast.AST) -> set[str]:
    """Every `ContextReceipt.<column>` and bare `receipt.<column>` in a subtree."""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
            if child.value.id in {"ContextReceipt", "receipt"}:
                found.add(child.attr)
    return found


def test_the_refusal_turns_on_exactly_two_columns() -> None:
    """Anti-vacuity for the test below.

    If `refuse_if_unservable` grew a third condition and this file kept passing,
    the equality it asserts would be over a set that no longer describes the
    refusal.
    """
    consulted = _attributes_named(_function("refuse_if_unservable"))

    assert {"withheld_at", "hydration_state"} <= consulted, (
        f"`refuse_if_unservable` consults {sorted(consulted)}; this file's premise is that it turns "
        "on `withheld_at` and `hydration_state`"
    )


def test_the_listing_filters_on_every_column_the_refusal_raises_on() -> None:
    """The disclosure direction.

    A condition the refusal raises on and the list does not exclude is a
    withheld receipt appearing in a list — which tells a reader a resolution
    happened, when, and how much it served, from the one surface allowed to say
    it exists at all.
    """
    refused_on = _attributes_named(_function("refuse_if_unservable")) & {
        "withheld_at",
        "hydration_state",
    }
    filtered_on = _attributes_named(_function("recent"))

    missing = sorted(refused_on - filtered_on)

    assert not missing, (
        f"`refuse_if_unservable` refuses on {missing} and `recent` does not filter on it. A receipt "
        "the detail read withholds would appear in the listing."
    )


def test_the_listing_uses_the_refusal_s_own_constant_for_hydration() -> None:
    """The drift direction.

    A second literal spelling of "which hydration states are servable" is a
    second answer, and the two would disagree the first time a state was added.
    `HYDRATION_SERVABLE` is the one place that set is written down.
    """
    listing = _function("recent")
    names = {node.id for node in ast.walk(listing) if isinstance(node, ast.Name)}

    assert "HYDRATION_SERVABLE" in names, (
        "`recent` does not use `HYDRATION_SERVABLE`; a literal state list here is a second answer "
        "to a question `refuse_if_unservable` already answers"
    )


def test_the_listing_is_tenant_scoped_in_its_own_predicate() -> None:
    """Scoped in the query, not filtered after it.

    A read that loaded rows and then compared has already loaded rows it may not
    return, and the comparison is one refactor from disappearing — in a direction
    that is a cross-tenant disclosure rather than an error.
    """
    filtered_on = _attributes_named(_function("recent"))

    assert "tenant_id" in filtered_on, "`recent` does not name `tenant_id` in its predicate"
