"""Gate: the contract's tags still group it.

Tags are how a generated client is organised and how a reader finds anything in
a 189-path document. They stopped grouping: 49 tags over 189 operations, three
operations untagged, three delimiter conventions in use at once, and three paths
whose methods sat in different subdomains — so `/v1/capabilities` appeared under
`retrieval` when you read it and `capabilities` when you wrote it.

None of that is a bug in any one change. It is what a naming convention does with
no gate under it: five renames each landed cleanly in code while the HTTP surface
accumulated the sediment. So this is the gate.

Three rules.

**Every operation carries a tag.** An untagged operation is invisible in a tag-
grouped client and appears in no section of the docs. The three infrastructure
endpoints are exempt by name, because `/healthz`, `/metrics` and `/readyz` are
not part of the versioned API and grouping them with one would be worse.

**One delimiter.** A tag is `subdomain: leaf`, and a multi-word part is
hyphenated — `admin: memory-curation`, not `admin: memory curation`. The colon
form was already 19 of the 49 and is the only one that expresses hierarchy; a
bare space was three and expressed nothing. Checked as "no bare space in a tag",
which is the property, rather than as a list of permitted tags, which would need
editing every time a subdomain is added.

**Every method of a path shares a subdomain with the others.** Not "one tag per
path" — an operation may carry a cross-cutting tag as well, and `retrieval` is a
real one that legitimately spans subdomains. What must not happen is a path whose
methods have *no* tag in common, because then the path itself belongs nowhere and
a reader looking for it has to guess which section it landed in.

**A deprecated operation is exempt from the last rule**, and only that one. A
deprecated alias is usually mid-rename, and the split it leaves is precisely what
the deprecation is retiring — `GET /v1/entities` is the live example. The
exemption expires with the alias, which is the point: it cannot become permanent
without somebody deleting the sunset date, and ADR 0009 makes that a tracked
issue rather than an omission.

Anti-vacuity: a run that inspects zero operations fails rather than passes. A
gate that scans nothing and prints a tick is the failure mode this directory is
written against, and a tag gate reading a contract it could not parse would do
exactly that.

Run locally:

    python3 scripts/check_contract_tags.py
    python3 scripts/check_contract_tags.py --explain
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checklib import repo_root, require_nonempty, run_guard

CONTRACT: Final = repo_root() / "openapi.json"

#: HTTP methods an OpenAPI path item may hold. Anything else in there is a
#: sibling key like `parameters`, not an operation.
_METHODS: Final = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

#: Not part of the versioned API. Exempt from the tag requirement by name rather
#: than by a prefix rule, so adding a fourth is a deliberate act.
_UNTAGGED_BY_DESIGN: Final = frozenset({"/healthz", "/readyz", "/metrics"})


def _operations(document: Mapping[str, Any]) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Every (path, method, operation) in the contract."""
    found: list[tuple[str, str, Mapping[str, Any]]] = []
    for path, item in document.get("paths", {}).items():
        if not isinstance(item, Mapping):
            continue
        for method, operation in item.items():
            if method.lower() in _METHODS and isinstance(operation, Mapping):
                found.append((path, method.lower(), operation))
    return found


def violations(document: Mapping[str, Any]) -> list[str]:
    """Everything wrong with the contract's tags, so one run reports them all."""
    found: list[str] = []
    by_path: dict[str, list[tuple[str, frozenset[str], bool]]] = {}

    for path, method, operation in _operations(document):
        tags = frozenset(operation.get("tags") or ())
        deprecated = bool(operation.get("deprecated"))
        by_path.setdefault(path, []).append((method, tags, deprecated))

        if not tags and path not in _UNTAGGED_BY_DESIGN:
            found.append(
                f"{method.upper()} {path}: no tag. An untagged operation is invisible in a "
                "tag-grouped client and appears in no section of the docs."
            )
        for tag in sorted(tags):
            if " " in tag.replace(": ", ""):
                found.append(
                    f"{method.upper()} {path}: tag {tag!r} uses a bare space. One convention: "
                    "`subdomain: leaf`, with a multi-word part hyphenated."
                )

    for path, methods in sorted(by_path.items()):
        live = [(m, t) for m, t, deprecated in methods if not deprecated and t]
        if len(live) < 2:
            continue
        shared = frozenset.intersection(*(t for _, t in live))
        if not shared:
            listing = ", ".join(f"{m.upper()}={sorted(t)}" for m, t in live)
            found.append(
                f"{path}: its methods share no tag ({listing}). A path whose operations sit in "
                "different subdomains belongs to neither, and a reader has to guess which section "
                "it landed in. A deprecated operation is exempt; a live one is not."
            )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--explain", action="store_true", help="print the tag inventory")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    operations = _operations(document)
    require_nonempty(
        operations,
        f"the operation population in {CONTRACT.name}",
        hint="A tag gate that parsed no operations would pass every contract, including a broken one.",
    )

    tags: dict[str, int] = {}
    for _, _, operation in operations:
        for tag in operation.get("tags") or ():
            tags[tag] = tags.get(tag, 0) + 1

    print(f"contract-tags gate: {len(operations)} operation(s) inspected, {len(tags)} tag(s)")
    if args.explain:
        for tag, count in sorted(tags.items()):
            print(f"  {tag:<34} {count}")

    found = violations(document)
    if not found:
        return 0
    for line in found:
        print(line, file=sys.stderr)
    print(
        f"\nRegenerate with `make openapi-export` after fixing the routers. "
        f"{CONTRACT.name} is generated; editing it directly is undone by the next export.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
