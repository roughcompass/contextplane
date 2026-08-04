"""Gate: new Alembic migration filenames must be named for what they do.

`check_no_phase_named_tests.py` excludes the migrations `versions/` subtree
entirely, because every filename in a phase-numbered chain necessarily embeds
a delivery marker (`0005_phase4_rbac_oidc.py`) as part of its revision id —
that was a framework-generated key, not a choice, and flagging it would only
have been noise.

That reasoning stops applying the moment the chain is squashed into one
behavior-named baseline. From here forward, a new migration's filename is a
free choice again, and the same rot the test-hygiene gate exists to prevent
in test files can recur here: `0002_phase9_something.py` tells a reader when
a change shipped, not what it does to the schema. This gate is the migration
equivalent — same failure mode, different subtree, so a separate small
script rather than folding an exemption inside out.

Two patterns are rejected:
  - `phase\\d+` anywhere in the filename (delivery-milestone naming).
  - `lmm` anywhere in the filename (the prefix this repository renamed away
    from; a new migration reintroducing it would be exactly the kind of
    half-finished rename this gate exists to catch early).

The baseline itself (`0001_baseline_schema.py`) is not exempted by name —
it simply does not match either pattern.

Run locally:
    python registry/scripts/check_migration_naming.py
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULT_SCOPE: tuple[str, ...] = ("registry/registry/storage/migrations/versions",)

_PHASE_RE = re.compile(r"phase\d+", re.IGNORECASE)
_LMM_RE = re.compile(r"lmm", re.IGNORECASE)


@dataclass(frozen=True)
class Hit:
    path: Path
    pattern: str


def _resolve_targets(scope: list[str]) -> list[Path]:
    out: list[Path] = []
    for entry in scope:
        target = (_WORKSPACE_ROOT / entry).resolve()
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix == ".py":
                out.append(target)
            continue
        for path in sorted(target.glob("*.py")):
            if path.name == "__init__.py":
                continue
            out.append(path)
    return out


def _scan(path: Path) -> Hit | None:
    if _PHASE_RE.search(path.name):
        return Hit(path=path, pattern="phase-numbered filename")
    if _LMM_RE.search(path.name):
        return Hit(path=path, pattern="'lmm' prefix")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify new Alembic migration filenames are behavior-named.",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=list(_DEFAULT_SCOPE),
        help="Repo-relative paths to scan (default: registry/registry/storage/migrations/versions).",
    )
    args = parser.parse_args(argv)

    missing = [entry for entry in args.paths if not (_WORKSPACE_ROOT / entry).exists()]
    if missing:
        print(
            f"scope does not exist: {', '.join(missing)}\n(full scope: {', '.join(args.paths)})",
            file=sys.stderr,
        )
        return 1

    targets = _resolve_targets(args.paths)
    if not targets:
        print("no migration files to scan in " + ", ".join(args.paths), file=sys.stderr)
        return 0

    hits = [hit for path in targets for hit in [_scan(path)] if hit is not None]
    if not hits:
        print(f"migration-naming gate: {len(targets)} file(s) scanned, 0 violation(s)")
        return 0

    for hit in hits:
        try:
            display = hit.path.relative_to(_WORKSPACE_ROOT)
        except ValueError:
            display = hit.path
        print(f"{display}: {hit.pattern}")

    print(
        f"\n{len(hits)} migration filename violation(s). Name the file for the schema "
        "change it makes, not for a delivery phase or the retired 'lmm' prefix.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
