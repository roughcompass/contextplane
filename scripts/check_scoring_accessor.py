"""Gate: only the tenant-resolving accessor reads a governed *weights* magnitude.

`contextplane/profile/scoring.py` exists because a consumer that calls
`ranking.weights(...)` directly gets the deployment's core value and silently
ignores the tenant's bound override. Its own docstring calls that "the failure
this module exists to make impossible to write by accident". It was not
impossible: for the whole life of that module every scoring consumer in the tree
read the registry directly and `resolve_weights` had no caller outside its unit
tests, so a tenant could publish, validate and activate a scoring extension and
be served the core values with nothing anywhere saying so.

That is the failure this check makes structural rather than advisory.

**Weights only, and that boundary is not arbitrary.** `validate_overrides`
accepts a weight map, demands the key set match the core exactly and demands it
sum to one. Thresholds and ladders cannot be overridden at all, so they carry no
tenant dimension and reading them directly is correct — `confidence_decay.py`
and `salience.py` do, and should.

**Why a script and not the loader.** `ranking.py` sits at the bottom of the
import graph and cannot see the profile system, let alone know which caller is
entitled to bypass it. The rule is about the *caller*, not the value, which is
exactly what a type or a runtime refusal cannot express here — and it is why
E9-T3's shape (refuse the read) does not transfer to this problem.

Anti-vacuity: the accessor itself must still contain a call, so this fails if
the population it scans is empty rather than reporting a clean tree that has
stopped reading the registry at all.

Run locally:

    python3 scripts/check_scoring_accessor.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import TypeGuard

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checklib import repo_root, run_guard

#: The one module allowed to read a weights magnitude, because it is the one
#: that resolves the tenant's override first.
_ACCESSOR = Path("contextplane/profile/scoring.py")

#: Only this accessor is tenant-scoped. `threshold` and `ladder` are not
#: overridable, so a direct read of one is the correct call and not a bypass.
_GUARDED = "weights"


def _reads_weights(node: ast.AST) -> TypeGuard[ast.Call]:
    """`ranking.weights(...)` or a bare `weights(...)` imported from it.

    Matched on the attribute name rather than on the resolved import, because
    both spellings reach the same function and a check that only understood one
    would be a rule with a documented way around it.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == _GUARDED and isinstance(func.value, ast.Name) and func.value.id == "ranking"
    return False


def main() -> int:
    root = repo_root()
    offenders: list[str] = []
    accessor_reads = 0

    for path in sorted((root / "contextplane").rglob("*.py")):
        relative = path.relative_to(root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as bad:  # pragma: no cover - a syntax error fails elsewhere first
            print(f"{relative}: {bad}", file=sys.stderr)
            return 1

        for node in ast.walk(tree):
            if not _reads_weights(node):
                continue
            if relative == _ACCESSOR:
                accessor_reads += 1
                continue
            offenders.append(f"{relative}:{node.lineno}")

    print(f"scoring-accessor gate: {accessor_reads} read(s) in {_ACCESSOR}, {len(offenders)} outside it")

    if accessor_reads == 0:
        print(
            f"\n{_ACCESSOR} reads no weights magnitude. Either the accessor stopped resolving the core "
            "default, or this check is scanning for a call that no longer exists — both make a clean "
            "result here meaningless.",
            file=sys.stderr,
        )
        return 1

    if offenders:
        for line in offenders:
            print(line, file=sys.stderr)
        print(
            "\nA weights magnitude is tenant-overridable, so reading it outside the accessor serves every "
            "tenant the core value and gives a tenant whose override activated no way to tell. Call "
            "`contextplane.profile.scoring.resolve_weights(session, tenant_id=..., model_id=...)` instead. "
            "A pure function with no session takes the resolved map as a required argument — see "
            "`extraction/salience.py`'s `combine`.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
