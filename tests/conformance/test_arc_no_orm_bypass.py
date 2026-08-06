"""ARC write-path conformance gate: mutations stay inside two owned surfaces.

Every ARC write goes through `registry/arc/service/` or
`registry/arc/workers/`. That is not a stylistic preference — it is the only
reason the guarantees elsewhere in this subsystem hold. Receipts are atomic
with their audit rows because one service writes both in one transaction;
challenges are single-use because one service holds the lock; content is
encrypted because one service owns the envelope. A router that issued its
own `UPDATE` would bypass all of that while still looking like ordinary code.

Workers are a second owned surface, not a hole in the first one. Everything
under `service/` acts on behalf of an authenticated request and authorizes
against its request context; a background worker runs on a schedule with no
request and nothing to authorize against, so forcing it through that same
API would mean inventing a context that names no one, or quietly skipping a
check the service layer would otherwise require. A worker that owns its own
bounded, idempotent transaction against a table the service layer also
writes (draining an outbox, expiring overdue rows, deleting long-stale ones)
is what a schedule-driven mutation actually looks like, not a bypass of the
request-driven one.

Reviews catch this unreliably: the offending line is usually short,
plausible, and in a file about something else. So it is a gate.

The rule: inside `registry/arc/`, a mutating SQL statement or an ORM write
may appear only under `registry/arc/service/` or `registry/arc/workers/`.
Anywhere else in the package — models, schemas, types — it is a failure.

Negative fixtures matter as much as the real assertions: they prove the
walker actually detects what it claims to, and that neither carve-out
happens to be exempting a directory that never had a real mutation in it to
begin with.

Both walkers over-approximate on purpose — a text search for a SQL verb or
an ORM method name cannot know what a string or a call site actually means
— and both need the same precision guard for it: a module that imports
nothing from `sqlalchemy` cannot execute SQL or hold a session at all, so
it is skipped rather than flagged for a false positive. `find_orm_writes`
had this from the start (`.add()` on a plain `set` reads the same as
`session.add()`); `find_sql_mutations` did not, until an ordinary English
word — `"truncated"`, a legitimate closed-vocabulary status value in a
module with no SQL capability whatsoever — collided with the verb
`TRUNCATE` as a substring. The fix is the same one already proven out for
the ORM half: skip files `_touches_sqlalchemy` says cannot write, and prove
that both directions — the precision test below still lets a real planted
violation in a `sqlalchemy`-touching module through, and the walker itself
still matches the substring; only the file-level skip changed.
"""

from __future__ import annotations

import ast
from pathlib import Path

# registry/arc/ — the package this gate governs.
ARC_ROOT = Path(__file__).parent.parent.parent / "registry" / "arc"

# The two subtrees permitted to mutate: request-driven writes, and the
# schedule-driven writes a background worker makes with no request to
# authorize against.
SERVICE_ROOT = ARC_ROOT / "service"
WORKERS_ROOT = ARC_ROOT / "workers"

# SQL verbs that change state. `SELECT ... FOR UPDATE` is deliberately absent:
# it takes a lock without writing, and locking is exactly what several ARC
# read paths must do.
_MUTATING_SQL_VERBS: frozenset[str] = frozenset(
    {"INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE", "ALTER TABLE", "DROP TABLE"}
)

# SQLAlchemy ORM constructs that write.
_ORM_WRITE_CALLS: frozenset[str] = frozenset({"add", "add_all", "delete", "merge", "bulk_save_objects"})

# Statement builders that produce a mutation.
_ORM_WRITE_BUILDERS: frozenset[str] = frozenset({"insert", "update", "delete"})


def _arc_python_files() -> list[Path]:
    return sorted(p for p in ARC_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _is_in_service_layer(path: Path) -> bool:
    return SERVICE_ROOT in path.parents or path.parent == SERVICE_ROOT


def _is_in_workers_layer(path: Path) -> bool:
    return WORKERS_ROOT in path.parents or path.parent == WORKERS_ROOT


def _is_in_a_permitted_write_surface(path: Path) -> bool:
    """Either owned surface: the request-driven one or the schedule-driven one."""
    return _is_in_service_layer(path) or _is_in_workers_layer(path)


def _string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string literal in the tree, with its line number.

    Walks constants rather than only `text(...)` arguments: a mutation
    assembled into a local variable and passed to `execute` later would
    otherwise slip past, and that is a natural way to write it.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
    return found


def find_sql_mutations(tree: ast.AST) -> list[tuple[int, str]]:
    """Lines whose string literals contain a mutating SQL verb."""
    hits: list[tuple[int, str]] = []
    for lineno, value in _string_constants(tree):
        upper = value.upper()
        for verb in _MUTATING_SQL_VERBS:
            if verb in upper:
                hits.append((lineno, verb.strip()))
                break
    return hits


def find_orm_writes(tree: ast.AST) -> list[tuple[int, str]]:
    """Lines calling a SQLAlchemy write method or mutation builder."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            # `session.add(...)`, `session.delete(...)` — the receiver name is
            # not checked, because a helper holding the session under another
            # name writes just the same.
            if func.attr in _ORM_WRITE_CALLS:
                hits.append((node.lineno, f".{func.attr}()"))
            elif func.attr in _ORM_WRITE_BUILDERS:
                hits.append((node.lineno, f".{func.attr}()"))
        elif isinstance(func, ast.Name) and func.id in _ORM_WRITE_BUILDERS:
            # `insert(Table)`, `update(Table)` imported directly.
            hits.append((node.lineno, f"{func.id}()"))
    return hits


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_sql_mutation_outside_the_arc_service_layer() -> None:
    violations: list[str] = []

    for path in _arc_python_files():
        if _is_in_a_permitted_write_surface(path):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        if not _touches_sqlalchemy(tree):
            # Same precision guard `test_no_orm_write_outside_the_arc_service_layer`
            # already applies: a module with no sqlalchemy import has no
            # session, no engine, and no `text()` to call, so it cannot
            # execute SQL at all — a string literal that merely contains a
            # SQL verb as a substring (an English word like "truncated") is
            # not a mutation. See the module docstring.
            continue
        for lineno, verb in find_sql_mutations(tree):
            violations.append(f"{path.relative_to(ARC_ROOT.parent.parent)}:{lineno}: {verb}")

    assert not violations, (
        "ARC mutations must live in registry/arc/service/ or registry/arc/workers/ — "
        "every atomicity, single-use, and encryption guarantee in this subsystem "
        "depends on a write staying inside one of those two owned surfaces:\n" + "\n".join(violations)
    )


def _touches_sqlalchemy(tree: ast.AST) -> bool:
    """Whether a module could hold a SQLAlchemy session at all.

    `find_orm_writes` deliberately ignores the receiver name, because a helper
    holding the session under another name writes just the same. The cost is
    that it cannot tell `session.add(row)` from `seen.add(digest)` on a plain
    `set`, so a module doing pure in-memory work gets flagged for a method
    name it shares with the ORM.

    A module that imports nothing from SQLAlchemy cannot perform an ORM write:
    it has no session type to construct and no way to annotate one it was
    handed, and `mypy --strict` over this tree forbids the unannotated
    parameter that would be the loophole. Skipping those files keeps the
    receiver-agnostic strictness everywhere a write is actually reachable,
    rather than buying precision with an allowlist entry — which would exempt
    the whole file forever, including code added to it later.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "sqlalchemy" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "sqlalchemy":
                return True
    return False


def test_no_orm_write_outside_the_arc_service_layer() -> None:
    violations: list[str] = []

    for path in _arc_python_files():
        if _is_in_a_permitted_write_surface(path):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        if not _touches_sqlalchemy(tree):
            continue
        for lineno, call in find_orm_writes(tree):
            violations.append(f"{path.relative_to(ARC_ROOT.parent.parent)}:{lineno}: {call}")

    assert not violations, "ARC ORM writes must live in registry/arc/service/ or registry/arc/workers/:\n" + "\n".join(
        violations
    )


def test_the_service_layer_is_actually_where_the_writes_are() -> None:
    """The gate above passes trivially if ARC never writes anywhere.

    This is the control: the service layer must contain real mutations, or
    the two tests above are asserting nothing about a subsystem that simply
    has no write path.
    """
    total = 0
    for path in _arc_python_files():
        if not _is_in_service_layer(path):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        total += len(find_sql_mutations(tree))

    assert total > 0, "no SQL mutations found in registry/arc/service/ — is this gate looking at the right tree?"


def test_the_workers_layer_is_actually_where_the_writes_are() -> None:
    """Same control as above, for the second carve-out.

    Without this, excluding `registry/arc/workers/` from the gate would be
    unverified by anything: it would pass just as trivially whether the
    directory holds real background-job mutations or turns out to be empty.
    """
    total = 0
    for path in _arc_python_files():
        if not _is_in_workers_layer(path):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        total += len(find_sql_mutations(tree))

    assert total > 0, "no SQL mutations found in registry/arc/workers/ — is this gate looking at the right tree?"


# ---------------------------------------------------------------------------
# Negative fixtures — the walker must actually detect
# ---------------------------------------------------------------------------


def test_the_sql_walker_detects_a_planted_mutation() -> None:
    """Without this, a walker that matched nothing would pass every file."""
    planted = ast.parse('sql = "INSERT INTO arc_receipts (receipt_id) VALUES (:rid)"\n')
    assert find_sql_mutations(planted)


def test_the_sql_walker_detects_a_mutation_in_any_case() -> None:
    """SQL is case-insensitive and real code is written both ways."""
    assert find_sql_mutations(ast.parse('sql = "update arc_receipts set integrity_state = :s"\n'))


def test_the_sql_walker_detects_a_mutation_bound_to_a_variable_first() -> None:
    """The natural way to sneak one past a shallower check."""
    source = 'stmt = "DELETE FROM arc_receipts WHERE receipt_id = :rid"\nawait session.execute(text(stmt))\n'
    assert find_sql_mutations(ast.parse(source))


def test_the_sql_walker_does_not_flag_a_plain_select() -> None:
    """Reads are the common case; flagging them would make the gate noise."""
    assert not find_sql_mutations(ast.parse('sql = "SELECT receipt_id FROM arc_receipts"\n'))


def test_the_sql_walker_does_not_flag_a_locking_read() -> None:
    """`SELECT ... FOR UPDATE` writes nothing, and several ARC read paths
    must take that lock. Flagging it would push callers toward *not*
    locking, which is the opposite of what this subsystem needs."""
    assert not find_sql_mutations(ast.parse('sql = "SELECT next_sequence FROM heads WHERE id = :i FOR UPDATE"\n'))


def test_the_orm_walker_detects_session_add() -> None:
    assert find_orm_writes(ast.parse("session.add(ArcReceipt())\n"))


def test_the_orm_walker_detects_a_mutation_builder() -> None:
    assert find_orm_writes(ast.parse("await session.execute(update(ArcReceipt).values(x=1))\n"))


def test_the_orm_walker_detects_a_bare_imported_builder() -> None:
    assert find_orm_writes(ast.parse("stmt = insert(ArcReceipt)\n"))


def test_the_orm_walker_does_not_flag_a_select() -> None:
    assert not find_orm_writes(ast.parse("await session.execute(select(ArcReceipt))\n"))


def test_a_module_importing_no_sqlalchemy_cannot_write_and_is_skipped() -> None:
    """The precision half of the gate.

    `find_orm_writes` cannot distinguish `session.add(row)` from
    `seen.add(digest)` on a plain `set`, by design. This is what stops that
    over-approximation from flagging a pure in-memory module for a method
    name it merely shares with the ORM.
    """
    pure = "seen: set[bytes] = set()\nseen.add(b'x')\n"
    assert find_orm_writes(ast.parse(pure)), "the walker still sees the .add() — that is expected"
    assert not _touches_sqlalchemy(ast.parse(pure)), "no sqlalchemy import, so the file must be skipped"


def test_a_module_importing_sqlalchemy_is_still_checked() -> None:
    """The strictness half: the skip must not become a way out.

    Both import spellings count, including a `TYPE_CHECKING`-only import,
    because annotating a session parameter is enough to hold one.
    """
    for source in (
        "import sqlalchemy\n",
        "from sqlalchemy.ext.asyncio import AsyncSession\n",
        "from sqlalchemy import text\n",
    ):
        assert _touches_sqlalchemy(ast.parse(source)), f"must be checked: {source!r}"


def test_a_module_importing_no_sqlalchemy_with_a_colliding_word_is_skipped() -> None:
    """The precision half for the SQL-verb walker (mirrors
    `test_a_module_importing_no_sqlalchemy_cannot_write_and_is_skipped`
    above, for the other walker).

    `find_sql_mutations` matches a SQL verb as a case-insensitive
    substring, so an ordinary English word collides with one: `"truncated"`
    is a legitimate closed-vocabulary status value (e.g. a parser warning
    code) and also contains `TRUNCATE`. A module with no sqlalchemy import
    has no session, no engine, and no `text()` to call — it cannot execute
    SQL at all — so it is skipped for the same reason the ORM half already
    skips a plain `set.add()`.
    """
    pure = 'CODE = "truncated"\n'
    assert find_sql_mutations(ast.parse(pure)), "the walker still matches the substring — that is expected"
    assert not _touches_sqlalchemy(ast.parse(pure)), "no sqlalchemy import, so the file must be skipped"


def test_a_module_touching_sqlalchemy_with_a_real_verb_is_still_caught() -> None:
    """The strictness half for the SQL-verb walker: the skip above must
    not become a way out for a module that actually can run SQL.
    """
    source = 'from sqlalchemy import text\nstmt = text("TRUNCATE TABLE arc_receipts")\n'
    tree = ast.parse(source)
    assert _touches_sqlalchemy(tree), "this module does import sqlalchemy and must still be checked"
    assert find_sql_mutations(tree), "a real mutating verb in a sqlalchemy-touching module must still be caught"
