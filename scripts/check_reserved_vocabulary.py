#!/usr/bin/env python3
"""Lint gate: a governed noun that already means something is not reused.

Two governed nouns in this codebase already carried a second meaning, and both
were found the same way: by hand, one at a time, after the naming decision had
been made. Nothing prevented a third, and the next author to introduce a
governed noun would have done what the previous two did -- reached for the
obvious word.

The rule this encodes: a noun that already names one governed thing may not name
a second one. So this gate is deliberately forward-looking. It ships with an **empty allowlist**,
because at the time it was written nothing in the scanned surfaces violated it.
That is the point: every entry that ever appears in `ALLOWLIST` will be a
deliberate decision somebody wrote a reason for, not inherited debt.

## What is scanned, and why only these two surfaces

- **Wire schemas** (`contextplane/api/schemas/`) -- the vocabulary a client
  reads. The refusal that prompted this was about a *field* named `severity` on
  a reporting obligation, and the wire name is the one a UI author sees.
- **Migrations** (`contextplane/storage/migrations/versions/`) -- column names
  and the values inside CHECK constraints, which is where `incident` already
  lives twice.

Internal variables, function parameters and local names are **out of scope on
purpose**. A local called `severity` inside the PII scanner is the scanner's own
word for its own concept and reads correctly there; the failure mode this gate
exists for is a *second governed meaning* reaching a durable, cross-team surface.
A gate that fired on every local would be turned off within a week, and a gate
that is off catches nothing.

## Why the meanings are sourced, not restated

Each reserved noun names where it is already defined. If a reserved word's owner
module stops defining it, `stale_reservations` fails rather than silently
continuing to reserve a word nothing uses -- the same staleness property
`check_file_sizes.py` gives its allowlist, for the same reason: a registry
nobody re-checks stops describing the tree and starts describing its own past.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import re
import sys
from collections.abc import Iterable, Sequence

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

SCHEMA_ROOT = REPO_ROOT / "contextplane" / "api" / "schemas"
MIGRATION_ROOT = REPO_ROOT / "contextplane" / "storage" / "migrations" / "versions"


@dataclasses.dataclass(frozen=True)
class ReservedWord:
    """A noun that already carries one governed meaning in this codebase.

    `owner_paths` are repo-relative modules that define the existing meaning.
    They are checked for the presence of `defined_by` so a reservation cannot
    outlive the thing it protects.
    """

    meaning: str
    #: The symbol the existing meaning is defined as, used for the staleness check.
    defined_by: str
    owner_paths: tuple[str, ...]
    #: Why a second meaning would be a defect rather than a style preference.
    reason: str


#: The governed nouns, and what each one already means.
#:
#: Both entries are the collisions that were found by hand. New entries are added
#: when a noun becomes governed, not when a collision is discovered -- that
#: ordering is the whole difference between this gate and a grep.
RESERVED: dict[str, ReservedWord] = {
    "severity": ReservedWord(
        meaning="the PII scanner's advisory < warn < block ordering",
        defined_by="_POLICY_SEVERITY",
        owner_paths=("contextplane/security/pii_scanner.py",),
        reason=(
            "A reporting obligation carries `materiality`, not `severity`. Two "
            "orderings sharing one field name is a defect waiting for a reader "
            "who has only ever seen the other one."
        ),
    ),
    "incident": ReservedWord(
        meaning="an *external* operational incident a claim or lifecycle row points at",
        defined_by="EVIDENCE_INCIDENT",
        owner_paths=(
            "contextplane/context/lifecycle.py",
            "contextplane/service/memory/source_ingest.py",
        ),
        reason=(
            "This system's own governed object may not be called `incident`, "
            "because the word already names something outside it. A record and "
            "a pointer to a record must not share a noun."
        ),
    ),
}


@dataclasses.dataclass(frozen=True)
class AllowlistEntry:
    """One deliberate reuse, with the reason it was allowed.

    `reason` is a required constructor argument, not an optional comment: a bare
    path is indistinguishable from turning the gate off for that file, which is
    the same argument `check_file_sizes.py` makes about its own allowlist.
    """

    path: str
    identifier: str
    word: str
    reason: str


#: Empty, and expected to stay small. See the module docstring.
ALLOWLIST: tuple[AllowlistEntry, ...] = ()


@dataclasses.dataclass(frozen=True)
class Finding:
    path: str
    line: int
    identifier: str
    word: str


#: A Pydantic/dataclass field declaration: an indented `name: Type`.
_FIELD = re.compile(r"^\s+([a-z][a-z0-9_]*)\s*:\s*[A-Za-z_\[\"]")

#: An Alembic column, however the migration spells it.
_COLUMN = re.compile(r"""sa\.Column\(\s*["']([a-z][a-z0-9_]*)["']""")

#: A quoted value inside a CHECK constraint, which is where `incident` lives today.
_CHECK_VALUE = re.compile(r"""IN\s*\(([^)]*)\)""", re.IGNORECASE)
_QUOTED = re.compile(r"""['"]([a-z][a-z0-9_]*)['"]""")


def _words(identifier: str) -> frozenset[str]:
    """The snake_case components of an identifier.

    Component-wise rather than substring: `severity` must fail, and so must
    `obligation_severity`, but `severity_of_thunderstorm` is not a word this
    codebase would produce and a substring match would also catch `adversity`.
    """
    return frozenset(identifier.split("_"))


def _scan_text(path: pathlib.Path, patterns: Sequence[re.Pattern[str]]) -> Iterable[Finding]:
    rel = path.relative_to(REPO_ROOT).as_posix()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        identifiers: list[str] = []
        for pattern in patterns:
            identifiers.extend(pattern.findall(line))
        for group in _CHECK_VALUE.findall(line):
            identifiers.extend(_QUOTED.findall(group))
        for identifier in identifiers:
            for word in _words(identifier) & RESERVED.keys():
                yield Finding(path=rel, line=number, identifier=identifier, word=word)


def _permitted(finding: Finding) -> bool:
    reserved = RESERVED[finding.word]
    if finding.path in reserved.owner_paths:
        return True
    return any(
        entry.path == finding.path and entry.identifier == finding.identifier and entry.word == finding.word
        for entry in ALLOWLIST
    )


def collect_findings() -> list[Finding]:
    """Every reserved-word reuse on the scanned surfaces, owners excluded."""
    findings: list[Finding] = []
    for path in sorted(SCHEMA_ROOT.rglob("*.py")):
        findings.extend(_scan_text(path, (_FIELD,)))
    for path in sorted(MIGRATION_ROOT.rglob("*.py")):
        findings.extend(_scan_text(path, (_COLUMN,)))
    return [f for f in findings if not _permitted(f)]


def stale_reservations() -> list[str]:
    """Reserved words whose owner no longer defines the meaning being protected."""
    stale: list[str] = []
    for word, reserved in sorted(RESERVED.items()):
        defined = any(
            (REPO_ROOT / owner).is_file() and reserved.defined_by in (REPO_ROOT / owner).read_text(encoding="utf-8")
            for owner in reserved.owner_paths
        )
        if not defined:
            stale.append(
                f"{word!r} reserves {reserved.meaning}, but no owner still defines "
                f"{reserved.defined_by!r}: {', '.join(reserved.owner_paths)}"
            )
    return stale


def stale_allowlist() -> list[str]:
    """Allowlist entries that no longer name a real reuse."""
    live = {
        (f.path, f.identifier, f.word)
        for path in (*SCHEMA_ROOT.rglob("*.py"), *MIGRATION_ROOT.rglob("*.py"))
        for f in _scan_text(path, (_FIELD, _COLUMN))
    }
    return [
        f"{entry.path}:{entry.identifier} no longer uses {entry.word!r}; drop the allowlist entry"
        for entry in ALLOWLIST
        if (entry.path, entry.identifier, entry.word) not in live
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    problems: list[str] = [*stale_reservations(), *stale_allowlist()]
    for finding in collect_findings():
        reserved = RESERVED[finding.word]
        problems.append(
            f"{finding.path}:{finding.line}: {finding.identifier!r} reuses the reserved "
            f"noun {finding.word!r}, which already means {reserved.meaning}.\n"
            f"    {reserved.reason}\n"
            f"    Defined in: {', '.join(reserved.owner_paths)}"
        )

    if problems:
        print("Reserved-vocabulary gate failed:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nPick a different noun, or add an AllowlistEntry with a written reason.",
            file=sys.stderr,
        )
        return 1

    print(f"reserved-vocabulary gate: {len(RESERVED)} governed noun(s), no reuse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
