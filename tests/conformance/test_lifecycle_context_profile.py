"""The lifecycle profile is a selection, not a lifecycle the registry runs.

A caller naming where they are in a delivery lifecycle is describing themselves.
The registry compares that description against where governed material was
recorded as applying, and returns less. Three properties keep that from quietly
becoming something larger, and each is checked structurally here rather than
behaviourally, because each is about what the code *cannot* do.

**No lifecycle state is stored.** The moment a run, a stage, or a workflow has a
table, the registry owns a state machine it does not run: two systems then hold
an opinion about which stage a change is in, they disagree, and the registry's
copy is the one nobody updates. Stage stays caller data, and the absence of a
table is what makes that structural instead of a promise in a docstring.

**Selection reads and never writes.** A read path that acquired a write would
turn asking for context into advancing something, which is the progression side
effect the boundary exists to refuse.

**Both transports enforce one vocabulary.** Not two copies that agree today.
A kind accepted on one surface and refused on the other is not a visible
divergence -- because `kind` is part of a reference's collision scope, the
accepted-but-wrong spelling binds cleanly and then never joins, and the symptom
is an absence rather than an error.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from contextplane.api.mcp.tools.context import registry_resolve_context
from contextplane.api.schemas.context import ContextResolveRequest
from contextplane.context import lifecycle
from contextplane.context.lifecycle import LIFECYCLE_REFERENCE_KINDS

#: The one table whose name contains "run" and legitimately predates any of
#: this: catalog synchronisation runs, which are ingest bookkeeping and have
#: nothing to do with a delivery lifecycle. Named explicitly so the assertion
#: below can be a real prohibition rather than a substring search with a hole.
_UNRELATED_RUN_TABLE = "sync_runs"

_LIFECYCLE_SOURCE = pathlib.Path(inspect.getfile(lifecycle))


def _table_names() -> set[str]:
    import contextplane.main  # noqa: F401  -- importing the app registers every model
    from contextplane.storage.models import Base

    return set(Base.metadata.tables)


# -- no lifecycle state is stored ---------------------------------------------


def test_no_table_holds_workflow_stage_or_lifecycle_state() -> None:
    """Stage is data the caller supplies, so nothing here persists one.

    A substring match rather than an exact list, because the failure this
    prevents is a *new* table appearing -- and a new table would have a name
    nobody thought to add to a list.
    """
    offenders = {name for name in _table_names() if any(word in name for word in ("workflow", "stage", "lifecycle"))}

    assert offenders == set(), f"lifecycle state must not be stored, but these tables hold it: {sorted(offenders)}"


def test_the_only_run_table_is_the_unrelated_catalog_sync_one() -> None:
    """Checked separately so the prohibition on runs is not a substring hole.

    `sync_runs` is ingest bookkeeping. Any *other* table naming a run would mean
    the registry had started tracking delivery runs, which is the boundary this
    whole surface is drawn around.
    """
    run_tables = {name for name in _table_names() if "run" in name}

    assert run_tables <= {_UNRELATED_RUN_TABLE}, f"unexpected run-state table(s): {sorted(run_tables)}"


# -- selection reads and never writes -----------------------------------------


def test_the_selection_module_performs_no_write() -> None:
    """Proof that resolving context cannot advance anything.

    Read off the syntax tree rather than by running a resolution: a write on a
    path no test happens to exercise is exactly the one that would survive a
    behavioural check.
    """
    tree = ast.parse(_LIFECYCLE_SOURCE.read_text())
    written_through = {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"session", "_session", "connection", "conn"}
        and node.func.attr not in {"execute"}
    }

    assert written_through == set(), f"the selection path must not write, but it calls: {sorted(written_through)}"


def test_the_selection_module_builds_only_select_statements() -> None:
    """The other half of the same property, from the opposite direction.

    A write does not have to go through the session's own verbs -- an
    `insert()`/`update()`/`delete()` construct handed to `execute` is a write
    too, and it would pass the check above untouched.
    """
    tree = ast.parse(_LIFECYCLE_SOURCE.read_text())
    constructs = {
        node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert constructs & {"insert", "update", "delete"} == set(), "selection builds reads only"
    assert "select" in constructs, "a selection that issues no select is not reading anything"


# -- one closed vocabulary, shared by both transports -------------------------


def test_the_vocabulary_is_closed_at_the_ten_agreed_kinds() -> None:
    assert set(LIFECYCLE_REFERENCE_KINDS) == {
        "run",
        "stage",
        "work_item",
        "repository",
        "artifact",
        "action",
        "build",
        "deployment",
        "incident",
        "outcome",
    }


def test_both_transports_expose_the_lifecycle_profile() -> None:
    """A profile one transport accepts and the other ignores is a silent divergence.

    The agent-facing surface is the one that would quietly lose it: an MCP tool
    that dropped the argument would still answer, just without narrowing.
    """
    assert "lifecycle_references" in ContextResolveRequest.model_fields
    assert "lifecycle_references" in inspect.signature(registry_resolve_context).parameters


@pytest.mark.parametrize(
    "module",
    ["contextplane/api/schemas/context.py", "contextplane/api/mcp/tools/context.py"],
)
def test_neither_transport_keeps_its_own_copy_of_the_vocabulary(module: str) -> None:
    """Both must enforce through the shared normalizer, not a local literal.

    A transport that inlined the ten kinds would be correct on the day it was
    written and would then drift, and the drift is undetectable from either side
    on its own -- which is why this is checked as "imports the function" rather
    than "agrees with the set".
    """
    source = (pathlib.Path(__file__).parents[2] / module).read_text()

    assert "normalize_reference_kind" in source, f"{module} must enforce through the shared normalizer"


def test_the_profile_refuses_an_out_of_set_kind_rather_than_passing_it_through() -> None:
    """The negative control, stated at the boundary both write paths share.

    An unrecognised kind must be refused. Accepted, it would bind cleanly and
    then fail to join to the receipt citing the correct spelling for the same
    external id -- an absence, not an error, and absences get read as "not yet".
    """
    with pytest.raises(lifecycle.UnknownLifecycleReferenceKind):
        lifecycle.normalize_reference_kind("deploymnet")
