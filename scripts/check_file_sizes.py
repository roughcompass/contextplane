#!/usr/bin/env python3
"""Lint gate: no shipped module exceeds an 800-line ceiling, repo-wide.

This gate started narrower than its name now promises. It was born scoped to
`contextplane/arc/service/` alone, at the exact moment that package was split to
relieve a 962-line file. That scoping made sense for one afternoon and then
quietly stopped: the ceiling is a repo-wide exit criterion, but the only
thing measuring it only ever looked at one subtree. Two files elsewhere in
the tree crossed 800 lines and nobody noticed, because nothing was watching
them; two more files that were promised a shrink at a later date grew
instead, because nothing re-checked the promise. A criterion that is
measured once and then trusted forever is not a criterion, it is a
snapshot -- this script exists so "no file over ~800 lines" is checked on
every commit against the whole shipped tree, not audited by hand at whatever
phase boundary someone remembers to look.

Scope is every `.py` file under `contextplane/` (the application
package) and `scripts/` (operational CLIs) -- the two roots
`make lint`'s ruff/mypy invocations already treat as "shipped source." Tests
are out of scope by design, the same way `check_test_assertions.py` and the
docstring ratchet both draw that line: a test file's size is not the
service/API surface this ceiling protects.

Two closed categories hold every file currently over the ceiling, and they
are deliberately different shapes so they cannot be confused for each other:

- `PERMANENT_EXEMPTIONS` -- a file that is *supposed* to be this large and
  will not shrink by design (today: the single-file curated DDL baseline
  migration). Exempt files are skipped entirely; they never fail this gate
  and never go stale, because there is nothing to drain.
- `ALLOWLIST` -- a drainable ratchet, one entry per file currently over the
  ceiling, each carrying a reason. This is the same shape
  `scripts/check_test_assertions.py`'s allowlist already proved out in this
  repository (73 entries at birth, 0 today): a debt marker that lets the
  gate ship without failing on every pre-existing violation, while still
  catching every *new* one. A bare path with no reason is indistinguishable
  from turning the gate off for that file, so `AllowlistEntry.reason` is a
  required field, not an optional comment -- there is no constructor call
  that produces an entry without one.

An allowlist that only ever grows is worse than no allowlist: it stops
meaning "currently over, tracked" and starts meaning "nobody removes these."
Two independent proofs make that impossible to do by accident:

1. A file at or over the ceiling that is not exempt or allowlisted fails,
   naming itself and its line count.
2. Every `ALLOWLIST` entry is re-checked against the file it names,
   independent of whatever `--paths` scope the current invocation was given
   (the same independence `check_test_assertions.py::_stale_allowlist_entries`
   uses, for the same reason: a narrowed `--paths` must not be able to hide a
   rotten entry outside it). An entry whose file no longer exists, or whose
   file has dropped under the ceiling, is stale and fails the gate until the
   entry is removed. Without this, the allowlist only ever grows, and it
   rots into a list of lies nobody is checking -- a waiver nobody needs is a
   waiver nobody is thinking about.

The ARC service tree (`contextplane/arc/service/`) that this gate grew
out of carries no allowlist or exemption entries today, and
`test_check_arc_service_sizes.py` pins that specifically: this repo-wide
generalisation must not be the moment that tree's own strictness quietly
loosens.

Run locally:
    python scripts/check_file_sizes.py
    python scripts/check_file_sizes.py --explain
    python scripts/check_file_sizes.py --paths contextplane/arc/service
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from checklib import repo_root, require_nonempty, run_guard

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Resolve from the repo root, not the workspace above it. Going up one extra
# level and back down through a literal directory name breaks in any checkout
# not named that -- a git worktree, most often -- and the gate then scans
# nothing while still exiting non-zero. Computed from `__file__`, not the cwd,
# so it holds whichever directory the gate is invoked from.
_REPO_ROOT = repo_root()

#: Shipped application code and operational CLIs -- the two roots `make
#: lint`'s ruff/mypy invocations already treat as source. Not `tests/`: a
#: test file's size is not the service/API surface this ceiling protects,
#: the same line `check_test_assertions.py` and the docstring ratchet draw.
_DEFAULT_SCOPE: tuple[str, ...] = ("contextplane", "scripts")

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}
)

#: A file at or above this many lines fails the gate, unless exempt or
#: allowlisted.
_CEILING = 800

#: A file at or above this many lines (and still under its ceiling) is
#: reported as approaching it. 85% of 800.
_WARN_AT = 680


@dataclasses.dataclass(frozen=True)
class PermanentExemption:
    """A file that is supposed to be this large and will not shrink.

    Distinct from `AllowlistEntry` on purpose: an exemption is not a debt
    to drain, it is a durable design decision (today: a curated single-file
    DDL baseline, which is the entire point of a migration squash). Mixing
    the two categories into one list would make every future reader have to
    re-derive, from the reason text alone, which entries are ever expected
    to disappear -- keeping them as separate types makes that machine-
    checkable instead of a matter of careful reading.
    """

    path: str
    reason: str


#: The migration squash's whole purpose is one curated, reviewable DDL
#: baseline in one file -- splitting it for a line count would re-introduce
#: the fragmented migration history the squash exists to replace.
PERMANENT_EXEMPTIONS: tuple[PermanentExemption, ...] = (
    PermanentExemption(
        path="contextplane/storage/migrations/versions/0001_baseline_schema.py",
        reason=(
            "Curated single-file DDL baseline. A migration squash's entire purpose is one "
            "reviewable file that recreates schema history from a clean database; splitting it "
            "by table would re-fragment exactly what the squash collapsed, for no reader benefit."
        ),
    ),
)


@dataclasses.dataclass(frozen=True)
class AllowlistEntry:
    """One file currently over the ceiling, and why it is not split today.

    `reason` is required, not optional -- a bare path with no reason is a
    gate bypass wearing a waiver's clothes. Landing an entry here is a
    deliberate, reviewed act: either the split has a real seam and is
    tracked to happen later, or a split was considered and rejected because
    it would not improve cohesion (a forced line-count cut is worse than the
    number it satisfies). Removing an entry is only correct in the same
    change that actually shrinks the file under the ceiling -- removing it
    for any other reason reopens the gap this gate exists to close.
    """

    path: str
    reason: str


#: Every file currently over the ceiling outside `PERMANENT_EXEMPTIONS`,
#: re-measured against the tree this gate ships against -- not carried
#: forward from an earlier, staler count. See this file's own history for
#: what changed between the two measurements.
ALLOWLIST: tuple[AllowlistEntry, ...] = (
    AllowlistEntry(
        path="scripts/seed.py",
        reason=(
            "Generic seed-bundle loader, already factored into one function per domain "
            "(vocabulary, external systems, entities, cross-entity facts, bitemporal attributes, "
            "tenants, actors, adoptions, edges) mirroring storage/models.py's own table grouping. "
            "A domain-by-domain module split is plausible dev-tooling cleanup, not a service-cohesion "
            "fix this ceiling was written to force -- deferred rather than forced here."
        ),
    ),
    AllowlistEntry(
        path="scripts/refactor_intent_nomenclature.py",
        reason=(
            "Reached 815 lines scoping the residue scan to the groups under check, so a "
            "single-group run reports only its own group's residue while residue no group "
            "claims stays visible to the all-groups gate. This file is one auditable unit by "
            "design: the manifest loader, the four modes, the root guard, and the preimage/"
            "postimage digest gates all have to agree about what a run may write, and a "
            "reader checking that safety argument has to see the whole of it at once. A seam "
            "chosen to clear a line count would land wherever the arithmetic put it rather "
            "than where the argument actually divides, and would outlive the constraint that "
            "produced it. Deliberately not self-expiring: this engine is still invoked as a "
            "gate after the nomenclature cutover it was written for, so it is not retired "
            "when that cutover ends, and a reason that claimed otherwise would go stale "
            "without anyone noticing. Drains when it is retired or split on a real seam."
        ),
    ),
    AllowlistEntry(
        path="contextplane/service/memory/promotion.py",
        reason=(
            "Grew 943 to 1131 lines; a prior plan recorded this shrinking via an attribute-write "
            "helper extraction that never happened. Corrected here: even if that extraction had "
            "happened, it would only move the ~160 lines of _write_canonical/_write_attribute/"
            "_write_edge/_current_canonical_value/_assert_no_pii out, leaving the file at roughly "
            "970 lines -- still over the ceiling, because the bulk of this file is the four "
            "state-transition workflows (propose/accept/reject/reverse) themselves, not their "
            "write helpers. A split that actually clears the ceiling means moving one of those "
            "workflows to a collaborator, which is a real design decision (mirroring the "
            "artifact-service lifecycle/materialisation/integrity split) that deserves its own "
            "reviewed task, not a byproduct of a line-count gate. Tracked here until that task runs."
        ),
    ),
    AllowlistEntry(
        path="contextplane/api/routers/memory_curation.py",
        reason=(
            "Split once already: the thirty request/response models that used to live here moved "
            "to api/schemas/memory_curation.py (matching the catalog.py convention), taking the "
            "file from 1315 to 1099 lines. What remains is route handlers plus the per-route "
            "rationale docstrings explaining non-obvious authorization and HTTP-tunneling "
            "decisions (why :link/:discard skip HttpMethodRouter, why claim history double-checks "
            "visibility, why direct assertion routes around extraction's containment checks) -- "
            "cutting further means cutting that rationale, which is the opposite of what this file "
            "is for. No further split attempted."
        ),
    ),
    AllowlistEntry(
        path="contextplane/api/mcp/tools/memory_curation.py",
        reason=(
            "The module's own docstring already makes the cohesion argument: thirteen MCP tools "
            "mirroring one REST router's one coordinated capability (queue, promotion review, "
            "confirmation, history, capability requests, direct assertion), deliberately kept in "
            "one module rather than split one-tool-per-file, because that would scatter one "
            "contract across thirteen places that all have to stay in step with the REST router "
            "it mirrors. Splitting here would work against the cohesion the file already states."
        ),
    ),
    AllowlistEntry(
        path="scripts/check_privileged_writes.py",
        reason=(
            "One flat, growing list of Rule(table, allowed_callers, guidance) data plus the "
            "scanner that enforces it. Splitting the data into a sibling module was tried and "
            "reverted: scripts/ has no __init__.py, so a cross-script import resolves under "
            "one invocation style (python scripts/check_privileged_writes.py, per the Makefile) "
            "and not the other (tests/unit/test_check_privileged_writes.py's own "
            "`from scripts.check_privileged_writes import ...`), which would have made the gate "
            "or its test suite fail depending on how it runs -- a correctness regression, not a "
            "cohesion win. Crossed 800 lines when the ten-predicate activation gate's own two "
            "new writer entries (arc_authoring_proposal_versions, arc_revisions) and a new "
            "arc_artifacts rule landed; every other entry is pre-existing and unrelated to that change."
        ),
    ),
    AllowlistEntry(
        path="scripts/prove_quickstart.py",
        reason=(
            "One end-to-end proof of the documented quickstarts, run against a genuinely clean "
            "clone -- the whole point is that the sequence runs as one linear narrative with "
            "shared executor/step/outcome plumbing. Splitting the steps across files would not "
            "change what runs, only make the sequence harder to read start to finish."
        ),
    ),
    AllowlistEntry(
        path="contextplane/api/routers/workspaces.py",
        reason=(
            "Waived previously at 872 lines with a revisit-if-it-grows condition; it has not "
            "grown (871 today). Condition re-confirmed, waiver renewed rather than treated as "
            "expired."
        ),
    ),
    AllowlistEntry(
        path="contextplane/service/workspace/core.py",
        reason=(
            "Unchanged at 862 lines. This is the workspace-level perceivability chokepoint every "
            "other workspace module (entries.py, search.py, purge.py) calls through rather than "
            "re-implementing access checks -- the same reason service/governance/visibility.py "
            "stays one file for cross-tenant reads. Splitting an access-control chokepoint into "
            "pieces is how one enforcement site starts disagreeing with another; previously "
            "recorded as a carry with no owning phase, corrected here to a reasoned waiver."
        ),
    ),
    AllowlistEntry(
        path="contextplane/storage/models.py",
        reason=(
            "One of exactly two modules (the other is arc/models.py, kept separate deliberately) "
            "declaring mapped classes against a single shared `Base`; the module's own docstring "
            "notes both must be imported before `Base.metadata` is read for any schema-wide "
            "operation. Splitting the catalog side further would multiply import-order footguns "
            "around that one invariant for no cohesion gain -- the tables are already one schema."
        ),
    ),
    AllowlistEntry(
        path="contextplane/api/routers/admin_progression.py",
        reason=(
            "Same shape as memory_curation.py before its split -- request/response models "
            "interleaved with handlers and supersession-write helpers -- and the same schema-"
            "extraction move would likely help here too. Not attempted in this change: one split "
            "was judged safe and proven move-only here; a second, unproven one is exactly the "
            "forced-to-hit-a-number move this gate is not supposed to cause. Left for a dedicated "
            "follow-up."
        ),
    ),
    AllowlistEntry(
        path="contextplane/service/catalog/progression.py",
        reason=(
            "At the ceiling exactly (800 lines): one closed-schema validator, one state-machine "
            "service class, and their shared vocabulary/gate-satisfaction helpers, all directly "
            "coupled to the same progression_definitions meta-schema. No line-count-neutral seam "
            "identified; splitting the validator from the service it validates for would separate "
            "two things that change together every time the meta-schema does."
        ),
    ),
)


@dataclasses.dataclass(frozen=True)
class FileCeiling:
    """One file held to a *tighter* ceiling than the repo-wide one, and why.

    The inverse of an `AllowlistEntry`: not a waiver for a file that is too
    large, but a stricter bound on a file whose whole value is staying small.
    A file earns one when its size is the property, not a side effect --
    where the difference between 250 lines and 700 is the difference between
    a module that delegates and a module that has quietly re-absorbed the
    work it delegated. The repo-wide ceiling cannot express that: 800 lines
    is the wall past which any file is unreadable, not the point at which a
    specific file has stopped doing its job.
    """

    path: str
    ceiling: int
    reason: str


#: Every file with a stricter ceiling than `_CEILING`. Deliberately short: a
#: tighter bound is a claim about what a file is *for*, and one that cannot
#: state the property it protects belongs in review, not here.
FILE_CEILINGS: tuple[FileCeiling, ...] = (
    FileCeiling(
        path="contextplane/wiring/services.py",
        ceiling=250,
        reason=(
            "The composition root. It grew 570 to 1,231 lines in days because every new service "
            "in any domain was enumerated here, so every domain's changes collided in one file. "
            "Each area now registers itself through its own `wiring.py` and this module only "
            "orchestrates: build shared infrastructure, call the area builders in dependency "
            "order, expand what they return into the typed container. That property -- adding a "
            "service to an existing area touches that area's `wiring.py` and the container's "
            "field list, and nothing here -- is invisible to the 800-line ceiling, which this "
            "file could re-approach for years while quietly re-absorbing per-service wiring. "
            "This bound makes the regrowth fail a gate instead of a code review nobody ran."
        ),
    ),
)


@dataclasses.dataclass(frozen=True)
class Violation:
    path: str
    lines: int
    #: The ceiling this file was actually judged against -- `_CEILING` unless
    #: a `FileCeiling` names it, so the failure message quotes the number the
    #: author has to meet rather than the repo-wide one.
    ceiling: int


def _iter_py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file() and not any(part in _EXCLUDE_DIRS for part in p.parts))


def _line_count(path: Path) -> int:
    # Counts newline characters, matching `wc -l` exactly, so this gate and
    # a one-off shell check using `wc -l` against the same file never
    # disagree at the boundary.
    content = path.read_text(encoding="utf-8", errors="replace")
    return content.count("\n")


def _matches(rel: str, entry_path: str) -> bool:
    return rel == entry_path or rel.endswith(f"/{entry_path}")


def _exemption_for(rel: str) -> PermanentExemption | None:
    for e in PERMANENT_EXEMPTIONS:
        if _matches(rel, e.path):
            return e
    return None


def _allowlist_entry_for(rel: str) -> AllowlistEntry | None:
    for a in ALLOWLIST:
        if _matches(rel, a.path):
            return a
    return None


def _file_ceiling_for(rel: str) -> FileCeiling | None:
    for c in FILE_CEILINGS:
        if _matches(rel, c.path):
            return c
    return None


def _invalid_file_ceilings() -> list[str]:
    """Config errors in `FILE_CEILINGS`, each of which would make it lie.

    A path that does not exist is a bound nothing is under. A ceiling at or
    above the repo-wide one is not tighter, so it would silently *loosen*
    nothing while looking like a control. And a file that is also
    allowlisted or permanently exempt is being told two contradictory
    things about the same number.
    """
    out: list[str] = []
    for entry in FILE_CEILINGS:
        if not (_REPO_ROOT / entry.path).is_file():
            out.append(f"{entry.path}: FILE_CEILINGS entry names no existing file")
        if entry.ceiling >= _CEILING:
            out.append(f"{entry.path}: ceiling {entry.ceiling} is not tighter than the {_CEILING}-line ceiling")
        if not entry.reason.strip():
            out.append(f"{entry.path}: FILE_CEILINGS entry has no reason")
        if _allowlist_entry_for(entry.path) is not None or _exemption_for(entry.path) is not None:
            out.append(f"{entry.path}: has a tighter ceiling and a waiver at the same time")
    return out


def _stale_allowlist_entries() -> list[str]:
    """Every `ALLOWLIST` entry whose file no longer needs it.

    Checked against each entry's own file directly, independent of whatever
    `--paths` scope the current run was given -- the same independence
    `check_test_assertions.py::_stale_allowlist_entries` uses, so a narrowed
    `--paths` can never hide a rotten entry outside it. Two ways an entry
    goes stale: the file it names no longer exists (moved, renamed, deleted
    -- the entry is now pointing at nothing), or the file still exists but
    has dropped under the ceiling on its own (shrunk enough that the waiver
    it once needed no longer applies). Either way, a standing waiver nobody
    needs is a waiver nobody is thinking about.
    """
    stale: list[str] = []
    for entry in ALLOWLIST:
        candidate = _REPO_ROOT / entry.path
        if not candidate.is_file():
            stale.append(f"{entry.path}: file no longer exists at this path")
            continue
        lines = _line_count(candidate)
        if lines < _CEILING:
            stale.append(
                f"{entry.path}: now {lines} lines, under the {_CEILING}-line ceiling -- waiver no longer needed"
            )
    return stale


def _missing_exemptions() -> list[str]:
    """A permanent exemption whose file no longer exists is dead config,
    not a live design decision -- cheap to catch, so it is."""
    return [e.path for e in PERMANENT_EXEMPTIONS if not (_REPO_ROOT / e.path).is_file()]


def _duplicate_paths() -> list[str]:
    """The same path named twice, whether within one list or across both,
    is a sign the config was hand-edited into an inconsistent state -- and
    across the two lists specifically, it would leave the categories
    (drainable vs. permanent) contradicting each other for one file."""
    seen: dict[str, int] = {}
    for path in [e.path for e in PERMANENT_EXEMPTIONS] + [a.path for a in ALLOWLIST]:
        seen[path] = seen.get(path, 0) + 1
    return [
        f"{path} appears {count} times across PERMANENT_EXEMPTIONS/ALLOWLIST"
        for path, count in seen.items()
        if count > 1
    ]


def _missing_reasons() -> list[str]:
    """Belt-and-suspenders against a hand-edit that lands an entry with an
    empty or whitespace-only reason -- the dataclass field is required by
    type, but nothing stops `reason=""` from type-checking cleanly."""
    out = []
    for e in PERMANENT_EXEMPTIONS:
        if not e.reason.strip():
            out.append(f"{e.path}: PERMANENT_EXEMPTIONS entry has no reason")
    for a in ALLOWLIST:
        if not a.reason.strip():
            out.append(f"{a.path}: ALLOWLIST entry has no reason")
    return out


def _print_explain() -> int:
    print("file-sizes gate: what it checks and how to clear it.\n")
    print(f"Every .py file under {' and '.join(_DEFAULT_SCOPE)} must be under {_CEILING} lines.")
    print(f"A file at or above {_WARN_AT} lines (85% of the ceiling) is reported as a")
    print("warning even when it still passes, so the wall is visible before it is hit.\n")
    print("To clear a failure:")
    print("  1. Split the file along a seam it already has -- cohesion, not an arbitrary")
    print("     line-count cut. Prove the split move-only (symbol inventory unchanged, every")
    print("     changed test line an import, openapi.json byte-identical) if it touches a route.")
    print(
        "  2. If no split preserves cohesion, add an AllowlistEntry to ALLOWLIST in "
        "scripts/check_file_sizes.py naming the reason -- a bare path with no reason is rejected "
        "structurally, not just by convention."
    )
    print(
        "  3. If the file is supposed to be this large permanently (a curated DDL baseline is the "
        "only example today), add a PermanentExemption to PERMANENT_EXEMPTIONS instead -- that "
        "category is for files that will never shrink, not files waiting their turn."
    )
    print(
        "\nA file named in FILE_CEILINGS is held to a *tighter* bound instead, because its "
        "size is the property rather than a side effect. Clearing one means moving work back "
        "to whichever module owns it -- not raising the number."
    )
    print(f"\nHeld to a tighter ceiling ({len(FILE_CEILINGS)}):")
    for c in FILE_CEILINGS:
        print(f"  {c.path} (< {c.ceiling} lines)\n    {c.reason}")
    print(f"\nPermanently exempt ({len(PERMANENT_EXEMPTIONS)}):")
    for e in PERMANENT_EXEMPTIONS:
        print(f"  {e.path}\n    {e.reason}")
    print(f"\nCurrently allowlisted ({len(ALLOWLIST)}):")
    for a in ALLOWLIST:
        print(f"  {a.path}\n    {a.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify no shipped module exceeds the line ceiling.")
    parser.add_argument("--paths", nargs="+", default=list(_DEFAULT_SCOPE), help="Workspace-relative paths to scan.")
    parser.add_argument("--explain", action="store_true", help="Print the rule and the current exemption/allowlist.")
    args = parser.parse_args(argv)

    if args.explain:
        return _print_explain()

    missing = [entry for entry in args.paths if not (_REPO_ROOT / entry).exists()]
    if missing:
        print(
            f"scope does not exist under {_REPO_ROOT}: {', '.join(missing)}\n"
            "Nothing was checked, so this is a failure rather than a pass.",
            file=sys.stderr,
        )
        return 1

    sizes: list[tuple[str, int]] = []
    for entry in args.paths:
        target = (_REPO_ROOT / entry).resolve()
        files = [target] if target.is_file() else _iter_py_files(target)
        for f in files:
            try:
                rel = str(f.relative_to(_REPO_ROOT))
            except ValueError:
                # Scanned via an absolute path outside the assumed root (a
                # test's tmp_path, for instance). The report is cosmetic;
                # the ceiling check below still holds regardless of what the
                # path displays as.
                rel = f.as_posix()
            sizes.append((rel, _line_count(f)))

    require_nonempty(
        sizes,
        "the .py scan population (paths: " + ", ".join(args.paths) + ")",
        allow_empty=args.paths != list(_DEFAULT_SCOPE),
    )

    sizes.sort(key=lambda item: item[1], reverse=True)

    violations: list[Violation] = []
    #: (path, lines, ceiling) -- the ceiling travels with the row so a warning
    #: on a file with a tighter bound quotes that bound, not the repo-wide one.
    warnings: list[tuple[str, int, int]] = []
    exempt_seen = 0
    allowlisted_seen = 0

    for rel, lines in sizes:
        # Checked before the waivers: a file with a tighter ceiling may not
        # also hold one (`_invalid_file_ceilings` fails the run if it does),
        # so reaching a waiver branch first could only mask the stricter bound.
        tighter = _file_ceiling_for(rel)
        if tighter is not None:
            if lines >= tighter.ceiling:
                violations.append(Violation(path=rel, lines=lines, ceiling=tighter.ceiling))
            elif lines >= int(tighter.ceiling * _WARN_AT / _CEILING):
                warnings.append((rel, lines, tighter.ceiling))
            continue
        if _exemption_for(rel) is not None:
            exempt_seen += 1
            continue
        if _allowlist_entry_for(rel) is not None:
            allowlisted_seen += 1
            if lines < _CEILING:
                # Below the ceiling despite an entry naming it: not a
                # violation right now, but `_stale_allowlist_entries` below
                # will independently catch and fail on the now-unneeded
                # entry -- this branch just avoids double-reporting it here.
                pass
            continue
        if lines >= _CEILING:
            violations.append(Violation(path=rel, lines=lines, ceiling=_CEILING))
        elif lines >= _WARN_AT:
            warnings.append((rel, lines, _CEILING))

    stale = _stale_allowlist_entries()
    missing_exemptions = _missing_exemptions()
    duplicates = _duplicate_paths()
    missing_reasons = _missing_reasons()
    invalid_ceilings = _invalid_file_ceilings()

    if sizes:
        print(
            f"file-sizes gate: {len(sizes)} file(s) scanned, ceiling {_CEILING} lines "
            f"({exempt_seen} exempt, {allowlisted_seen} allowlisted, "
            f"{len(FILE_CEILINGS)} held tighter)"
        )
        for rel, lines in sizes[:8]:
            print(f"  {lines:>4}  {rel}")
    else:
        print("file-sizes gate: no .py files in scope: " + ", ".join(args.paths))

    for rel, lines, ceiling in warnings:
        print(f"warning: {rel} is {lines} lines, {ceiling - lines} below the {ceiling}-line ceiling")

    if not (violations or stale or missing_exemptions or duplicates or missing_reasons or invalid_ceilings):
        return 0

    for v in violations:
        print(f"{v.path}: {v.lines} lines meets or exceeds the {v.ceiling}-line ceiling", file=sys.stderr)
    for c in invalid_ceilings:
        print(f"invalid-file-ceiling: {c}", file=sys.stderr)
    for s in stale:
        print(f"stale-allowlist-entry: {s}", file=sys.stderr)
    for m in missing_exemptions:
        print(f"missing-exemption-target: {m}: file no longer exists at this path", file=sys.stderr)
    for d in duplicates:
        print(f"duplicate-entry: {d}", file=sys.stderr)
    for r in missing_reasons:
        print(f"missing-reason: {r}", file=sys.stderr)

    if violations:
        print(
            "\nSplit the file along a real seam, or add a reasoned AllowlistEntry/PermanentExemption "
            "in scripts/check_file_sizes.py. Run with --explain for the full criterion.",
            file=sys.stderr,
        )
    if stale:
        print(
            "\nRemove the stale entry from ALLOWLIST in scripts/check_file_sizes.py -- "
            "a waiver nobody needs is one nobody is thinking about.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
