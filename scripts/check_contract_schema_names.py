"""Gate: no two response models share a class name.

FastAPI names a schema after its Python class. When two classes in different
modules share a name, it cannot: it qualifies **both** by module path, so
`CitationResponse` becomes `contextplane__api__routers__memory__CitationResponse`
*and* `contextplane__api__schemas__simulation__CitationResponse`.

That is worse than it sounds, and the reason this gate exists rather than a
comment:

**The rename lands on the schema that was already published.** A new model
named the same as an existing one does not get an awkward name of its own — it
renames the incumbent. Every generated client that referenced the old name stops
compiling, and nothing in the adding change looks wrong. This is exactly how it
was found: E24 added a second `CitationResponse` and the *dashboard's* build
broke on a schema E24 never touched.

**A collision is invisible in review.** The two classes are in different files,
usually in different subsystems, and neither one is wrong on its own. The only
place the collision is visible is the generated contract — which is why the check
belongs here, over the exported document, rather than over the source.

The rule is the property, not a list: **no schema name contains `__`.** A
double underscore appears in a schema name only when FastAPI qualified it, and it
appears in no hand-written model name in this repository. Checking for the marker
rather than for duplicate class names means the gate keeps working when somebody
adds a model in a module structure nobody anticipated.

Anti-vacuity: a run that inspects zero schemas fails rather than passes. A gate
that silently found nothing to check is a gate that has stopped being one.

Run locally:
    python3 scripts/check_contract_schema_names.py
    python3 scripts/check_contract_schema_names.py --explain
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Final

from checklib import repo_root, require_nonempty, run_guard

CONTRACT: Final = repo_root() / "openapi.json"

#: The marker FastAPI leaves when it had to disambiguate. Two underscores never
#: appear in a hand-written model name here, so its presence is the collision.
_QUALIFIED_MARKER: Final = "__"


def qualified_names(document: dict[str, object]) -> list[str]:
    """Every schema name FastAPI had to qualify, sorted."""
    components = document.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    if not isinstance(schemas, dict):
        return []
    return sorted(name for name in schemas if _QUALIFIED_MARKER in name)


def _short_name(qualified: str) -> str:
    """The class name inside a qualified schema name."""
    return qualified.rsplit(_QUALIFIED_MARKER, 1)[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--explain", action="store_true", help="print the schema inventory")
    args = parser.parse_args(argv)

    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    components = document.get("components", {})
    schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
    require_nonempty(schemas, "the contract's schema inventory")

    print(f"contract-schema-names gate: {len(schemas)} schema(s) inspected")
    if args.explain:
        for name in sorted(schemas):
            print(f"  {name}")

    collisions = qualified_names(document)
    if not collisions:
        return 0

    by_class: dict[str, list[str]] = {}
    for name in collisions:
        by_class.setdefault(_short_name(name), []).append(name)

    print("\ncontract-schema-names gate: FastAPI had to qualify these schema names:", file=sys.stderr)
    for short, names in sorted(by_class.items()):
        print(f"\n  {short} is declared in {len(names)} modules:", file=sys.stderr)
        for name in names:
            module = name.rsplit(_QUALIFIED_MARKER, 1)[0].replace(_QUALIFIED_MARKER, ".")
            print(f"    - {module}", file=sys.stderr)
    print(
        "\nRename all but one of each group, and prefer the *new* model taking the longer name: "
        "a collision renames the schema that was already published, so every generated client "
        "referencing the incumbent stops compiling. Then run `make openapi-export`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
