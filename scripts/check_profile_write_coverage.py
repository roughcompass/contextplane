#!/usr/bin/env python
"""Fail the build when an entity writer does not resolve through profile validation.

The profile declares which entity types exist and which properties they carry.
That is only governance if every path that writes an entity consults it, and the
number of such paths is not fixed — a new service, a new worker, a new ingest
connector each add one. A rule enforced by everyone remembering to call something
is a rule with a half-life, so this gate enumerates the writers structurally and
fails when one appears that neither validates nor says why it does not have to.

**Two ways to be compliant, and the second one is checked, not asserted.**

A writer either routes through `contextplane/entities/validation.py`, or it
writes only *fixed control keys* the system owns rather than caller-supplied
properties — `lifecycle`, `shared_with_tenants`, the interface pair. The second
kind is registered with the exact keys it may write, and the gate verifies the
module writes those and nothing else. A path allowlist alone would let a
registered module quietly start writing caller data under an entry that was
honest when somebody wrote it.

**A dynamic key is never allowlistable.** `Attribute(key=key)` inside a loop over
a caller's dict is the signature of a writer handling arbitrary properties, and
that is exactly what has to be validated. Only a key this gate can resolve to a
string literal or a module-level string constant counts as fixed.

**Stale entries fail too.** A registered module that no longer writes entities is
a rule protecting nothing, and leaving it in place makes the registry read as
larger coverage than it has.

Run it directly, or through `make lint`:

    python scripts/check_profile_write_coverage.py
    python scripts/check_profile_write_coverage.py --explain
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The tables whose rows carry profile-governed entity data.
_GOVERNED_TABLES: tuple[str, ...] = ("entities", "attributes")

#: Migrations describe the schema rather than write through it, and the test
#: tiers construct rows deliberately to exercise the writers this gate protects.
_EXCLUDED_PARTS: frozenset[str] = frozenset({"migrations"})

#: The ORM classes whose construction is an entity-data write.
_ROW_CLASSES: frozenset[str] = frozenset({"Entity", "Attribute"})

#: The module a compliant writer resolves through.
_VALIDATION_MODULE = "contextplane.entities.validation"

#: The service method that reaches the validator on a writer's behalf. A module
#: calling this is validated even though it never names the validation module —
#: `SchemaService` holds the validator and every entity write in the catalog area
#: goes through it.
_VALIDATING_CALL = "validate_entity_attributes"

_SQL_WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(" + "|".join(_GOVERNED_TABLES) + r")\b",
    re.IGNORECASE,
)

VALIDATED = "validated"
FIXED_KEYS = "fixed-keys"


@dataclasses.dataclass(frozen=True)
class Entry:
    """One registered entity writer and the basis on which it is permitted."""

    path: str
    kind: str
    reason: str
    keys: frozenset[str] = frozenset()


#: Every module that writes entity data, and why it is allowed to.
#:
#: Adding a path here is a deliberate, reviewed act. Before adding one, ask
#: whether the writer handles caller-supplied properties: if it does, it belongs
#: in the `VALIDATED` kind and needs the validation call, not an entry excusing
#: it. `FIXED_KEYS` is for a module maintaining a control key the system owns and
#: a caller cannot name.
REGISTRY: tuple[Entry, ...] = (
    Entry(
        path="contextplane/service/catalog/entity.py",
        kind=VALIDATED,
        reason=(
            "The general entity write path — REST, MCP, capability APIs, sync and internal callers "
            "all land here. It writes whatever properties a caller supplies, so it validates against "
            "the tenant's bound profile on create and on update alike."
        ),
    ),
    Entry(
        path="contextplane/service/catalog/attribute_writes.py",
        kind=VALIDATED,
        reason=(
            "The promotion writer, which lands an accepted claim's attribute in the canonical graph. "
            "Its key comes from the claim rather than from a person, which is precisely why it has to "
            "be checked against the entity type's declared properties and not only against the predicate "
            "vocabulary."
        ),
    ),
    Entry(
        path="contextplane/service/catalog/lifecycle.py",
        kind=FIXED_KEYS,
        keys=frozenset({"lifecycle"}),
        reason=(
            "Maintains the lifecycle state machine's own attribute. The value is a state this service "
            "computed, not a property a caller named, and the profile has nothing to say about it."
        ),
    ),
    Entry(
        path="contextplane/service/governance/visibility.py",
        kind=FIXED_KEYS,
        keys=frozenset({"shared_with_tenants"}),
        reason=(
            "Writes the sharing ACL the visibility chokepoint reads. It is authorization state rather "
            "than entity data, and routing it through a validator would make the policy kernel depend "
            "on the profile it is asked about."
        ),
    ),
    Entry(
        path="contextplane/service/catalog/interface_storage.py",
        kind=FIXED_KEYS,
        keys=frozenset({"interface_source", "interface_canonical"}),
        reason=(
            "Stores an interface's source and canonical documents under two fixed keys. The payload is "
            "an interface contract, whose shape the interface family governs at compile time rather "
            "than the entity type's property list."
        ),
    ),
)


@dataclasses.dataclass(frozen=True)
class Violation:
    """One way the tree disagrees with the registry."""

    path: str
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.code} — {self.detail}"


@dataclasses.dataclass(frozen=True)
class WriterFacts:
    """What one module does with entity data, as read off its syntax tree."""

    writes: bool
    validates: bool
    fixed_keys: frozenset[str]
    has_dynamic_key: bool


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, so a key named by constant resolves."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    return constants


def read_writer(path: Path, source: str) -> WriterFacts:
    """Read one module: does it write entity data, does it validate, which keys does it write?"""
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a file that will not parse fails elsewhere
        return WriterFacts(writes=False, validates=False, fixed_keys=frozenset(), has_dynamic_key=False)

    constants = _module_constants(tree)
    writes_orm = False
    fixed: set[str] = set()
    dynamic = False
    validates = False

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _VALIDATION_MODULE:
            validates = True
        if isinstance(node, ast.Import):
            validates = validates or any(alias.name == _VALIDATION_MODULE for alias in node.names)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == _VALIDATING_CALL:
                validates = True
            if isinstance(func, ast.Name) and func.id in _ROW_CLASSES:
                writes_orm = True
                resolved, is_dynamic = _key_of(node, constants)
                fixed |= resolved
                dynamic = dynamic or is_dynamic

    writes_sql = bool(_SQL_WRITE.search(source))
    return WriterFacts(
        writes=writes_orm or writes_sql,
        validates=validates,
        fixed_keys=frozenset(fixed),
        has_dynamic_key=dynamic,
    )


def _key_of(call: ast.Call, constants: dict[str, str]) -> tuple[set[str], bool]:
    """The attribute key one row construction writes, and whether it is dynamic.

    An `Entity(...)` carries no key at all — it is the row the attributes hang
    off — so it contributes neither. Only a resolvable literal or module constant
    counts as fixed; anything else is a caller-supplied property by construction.
    """
    for keyword in call.keywords:
        if keyword.arg != "key":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return {value.value}, False
        if isinstance(value, ast.Name) and value.id in constants:
            return {constants[value.id]}, False
        return set(), True
    return set(), False


def _in_scope(path: Path) -> bool:
    return not (_EXCLUDED_PARTS & set(path.parts))


def check(root: Path = _REPO_ROOT) -> list[Violation]:
    """Compare every entity writer in the tree against the registry."""
    registry = {entry.path: entry for entry in REGISTRY}
    violations: list[Violation] = []
    seen_writers: set[str] = set()

    for path in sorted((root / "contextplane").rglob("*.py")):
        if not _in_scope(path):
            continue
        relative = path.relative_to(root).as_posix()
        facts = read_writer(path, path.read_text(encoding="utf-8"))
        if not facts.writes:
            continue
        seen_writers.add(relative)

        entry = registry.get(relative)
        if entry is None:
            violations.append(
                Violation(
                    path=relative,
                    code="unregistered-writer",
                    detail=(
                        "writes entity data but is not registered. Route it through "
                        f"{_VALIDATION_MODULE} if it writes caller-supplied properties, or register it "
                        "with the fixed control keys it maintains and the reason it needs no validation."
                    ),
                )
            )
            continue

        if entry.kind == VALIDATED and not facts.validates:
            violations.append(
                Violation(
                    path=relative,
                    code="registered-but-bypassing",
                    detail=(
                        f"is registered as validating but calls neither {_VALIDATION_MODULE} nor "
                        f"{_VALIDATING_CALL}(). A writer that stopped validating keeps its entry and "
                        "reads as covered."
                    ),
                )
            )

        if entry.kind == FIXED_KEYS:
            if facts.has_dynamic_key:
                violations.append(
                    Violation(
                        path=relative,
                        code="dynamic-key-under-fixed-entry",
                        detail=(
                            "writes an attribute whose key is not a literal or module constant. A key the "
                            "caller can name is a profile-governed property, so this writer must validate "
                            "rather than be registered as maintaining fixed keys."
                        ),
                    )
                )
            widened = facts.fixed_keys - entry.keys
            if widened:
                violations.append(
                    Violation(
                        path=relative,
                        code="key-outside-entry",
                        detail=(
                            f"writes {', '.join(sorted(widened))}, which its entry does not name. Add the key "
                            "to the entry with a reason, or route the write through validation."
                        ),
                    )
                )

    for registered in sorted(registry):
        if registered not in seen_writers:
            violations.append(
                Violation(
                    path=registered,
                    code="stale-entry",
                    detail=(
                        "is registered as an entity writer but writes no entity data. Remove the entry — a "
                        "registry longer than the truth reads as more coverage than there is."
                    ),
                )
            )

    return violations


def _explain() -> str:
    lines = [
        "Every module writing `entities` or `attributes` must be registered, on one of two bases:",
        "",
        f"  {VALIDATED:<12} routes through {_VALIDATION_MODULE} (directly or via {_VALIDATING_CALL}())",
        f"  {FIXED_KEYS:<12} writes only the fixed control keys its entry names",
        "",
        "Registered writers:",
    ]
    for entry in REGISTRY:
        keys = f" [{', '.join(sorted(entry.keys))}]" if entry.keys else ""
        lines.append(f"  {entry.path} — {entry.kind}{keys}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--explain", action="store_true", help="describe the rule and list the registry")
    args = parser.parse_args(argv)

    if args.explain:
        print(_explain())
        return 0

    violations = check()
    if violations:
        print("profile-write-coverage gate: FAILED", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1

    print(f"profile-write-coverage gate: {len(REGISTRY)} entity writer(s) registered, all accounted for")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
