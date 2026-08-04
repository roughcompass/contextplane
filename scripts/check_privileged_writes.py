"""Lint gate: some tables may only be written through one module.

A table is *privileged* here when writing a row to it establishes something the
rest of the system then trusts. The trust holds only because there is exactly
one place that can create such a row — a second writer produces rows that look
identical while satisfying none of the invariants. That property cannot be
expressed as a type or a constraint, so it is enforced structurally: this gate
fails when an unlisted module writes to a privileged table.

Three tables are governed today:

`tenants` — inserting a row creates a new principal in the authorization model.
    Permitted caller: `auth/entitlements/actor_store.py`, which materializes a
    tenant on first successful entitlement resolution, guards with ON CONFLICT
    DO NOTHING, and emits a tenant audit event in the same transaction.

`lmm_claims` — every invariant a staged claim carries (it conforms to the
    ontology, its value matches the predicate's declared type, its subject
    resolves to a real entity, it has provenance, it is never more visible than
    the thing it describes) is a property of the write path, not of the row.
    Permitted callers: `service/claims.py`, and `service/contest.py` for one
    derived flag that carries no invariant — see the rule for why.

`lmm_claim_provenance` — provenance is immutable once written. A caller that
    can rewrite an excerpt can make a claim appear supported by evidence that
    never said it. Permitted caller: `service/claims.py`.

Migrations are excluded rather than enumerated: they legitimately seed rows
during schema bootstrapping, and the migration runner controls when they run.
Dev scripts and tests are out of scope for the same reason — they are not
deployed.

Adding a caller is a deliberate act. Before extending `_RULES`, be able to say
why the invariants the existing writer enforces are also enforced by the new
one; if they are not, the new caller belongs behind the existing writer instead.

Run locally:
    python registry/scripts/check_privileged_writes.py
    python registry/scripts/check_privileged_writes.py --paths registry/registry/service
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Default scope — the shipped application code only. Migrations, dev scripts,
# and tests are excluded: migrations run under operator control, dev scripts
# are not deployed, and tests need to seed rows directly.
_DEFAULT_SCOPE: tuple[str, ...] = ("registry/registry",)

# Subtrees never flagged even when inside the default scope.
_EXCLUDE_SUBTREES: tuple[str, ...] = ("registry/registry/storage/migrations",)

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
    }
)


@dataclasses.dataclass(frozen=True)
class Rule:
    """One privileged table, its permitted writers, and why they are permitted."""

    table: str
    allowed_callers: frozenset[str]
    guidance: str

    @property
    def pattern(self) -> re.Pattern[str]:
        """Match a write to this table, tolerating extra whitespace.

        UPDATE and DELETE are matched as well as INSERT. A module that can
        rewrite a staged claim's value, or flip one from `unlinked` to `staged`
        without re-resolving its subject, bypasses the same invariants as one
        that inserts a fresh row.
        """
        return re.compile(
            rf"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+{re.escape(self.table)}\b",
            re.IGNORECASE,
        )


RULES: tuple[Rule, ...] = (
    Rule(
        table="tenants",
        allowed_callers=frozenset({"registry/registry/auth/entitlements/actor_store.py"}),
        guidance=(
            "A tenant row is a principal in the authorization model. A new caller must guard "
            "with ON CONFLICT DO NOTHING and emit a tenant.* audit event in the same "
            "transaction, so tenant creation is always audited atomically."
        ),
    ),
    Rule(
        table="lmm_claims",
        allowed_callers=frozenset(
            {
                "registry/registry/service/claims.py",
                # Permitted for one derived column and nothing else.
                #
                # `is_contested` is a cached answer to "does an unresolved
                # disagreement involving this claim exist". It is not a claim
                # invariant: it says nothing about the ontology, the value, the
                # subject, the provenance, or the visibility, and setting it
                # cannot make an invalid claim look valid. The promotion gate
                # reads the column rather than running the query, so it has to be
                # maintained where disagreements are detected and resolved.
                #
                # Routing it back through the claim service would add a method
                # that exists solely for this caller -- indirection with no
                # guarantee attached. What this file must never do is touch a
                # column the write path derives, and the gate cannot check that
                # for you; a change here needs the column list read.
                "registry/registry/service/contest.py",
            }
        ),
        guidance=(
            "Claim invariants live in the write path, not the row: ontology conformance, "
            "declared value type, subject resolution, required provenance, and visibility "
            "never broader than the subject. A second writer produces rows that look "
            "identical while enforcing none of them. Write through ClaimService instead. "
            "The one exception writes a derived flag and no invariant; if your caller "
            "touches anything the write path derives, it does not belong on this list."
        ),
    ),
    Rule(
        table="lmm_claim_provenance",
        allowed_callers=frozenset({"registry/registry/service/claims.py"}),
        guidance=(
            "Provenance is immutable once written: correcting a claim creates a new claim. "
            "A caller that can rewrite an excerpt can make a claim appear to be supported "
            "by evidence that never said it."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def resolve_targets(scope: list[str]) -> list[Path]:
    """Expand the scope list into concrete .py files to scan."""
    excluded_roots = [(_REPO_ROOT / p).resolve() for p in _EXCLUDE_SUBTREES]
    out: list[Path] = []
    for entry in scope:
        target = (_REPO_ROOT / entry).resolve()
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix == ".py":
                out.append(target)
            continue
        for path in target.rglob("*.py"):
            if not path.is_file():
                continue
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            if any(path.is_relative_to(excl) for excl in excluded_roots):
                continue
            out.append(path)
    return out


@dataclasses.dataclass(frozen=True)
class Violation:
    path: str
    line_no: int
    line_text: str
    rule: Rule


def check_file(path: Path) -> list[Violation]:
    """Every privileged write in this file that its path is not permitted to make."""
    rel = str(path.relative_to(_REPO_ROOT))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []

    found: list[Violation] = []
    for rule in RULES:
        if rel in rule.allowed_callers:
            continue
        pattern = rule.pattern
        found.extend(
            Violation(path=rel, line_no=i + 1, line_text=line.strip(), rule=rule)
            for i, line in enumerate(lines)
            if pattern.search(line)
        )
    return found


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify that privileged tables are written only through their one module.",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=list(_DEFAULT_SCOPE),
        help="Repo-relative paths to scan (default: registry/registry).",
    )
    args = parser.parse_args(argv)

    targets = resolve_targets(args.paths)
    if not targets:
        print("no files in scope (paths: " + ", ".join(args.paths) + ")", file=sys.stderr)
        return 0

    violations = [v for path in targets for v in check_file(path)]
    if not violations:
        print(
            f"privileged-write gate: {len(targets)} file(s) scanned, "
            f"{len(RULES)} table(s) governed"
        )
        return 0

    for v in violations:
        print(f"{v.path}:{v.line_no}: unpermitted write to {v.rule.table}\n    {v.line_text}")

    tables = sorted({v.rule.table for v in violations})
    print(f"\n{len(violations)} unpermitted write(s) to {', '.join(tables)}.", file=sys.stderr)
    for rule in RULES:
        if rule.table in tables:
            print(f"\n  {rule.table}: {rule.guidance}", file=sys.stderr)
    print(
        "\nIf a new caller genuinely belongs, add its path to RULES in "
        "registry/scripts/check_privileged_writes.py and record why.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
