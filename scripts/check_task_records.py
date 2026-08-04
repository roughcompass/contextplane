"""Lint gate: a task plan may not claim something the plan itself contradicts.

The planning workspace is a sibling repository whose task files are the record of
what has shipped. Nothing checked them against themselves, and every failure that
record has produced was the same shape — a claim nobody could disprove without
reading the file it was making a claim about.

Three of them, all real:

A task sat at `in-progress` for three months while a separate document recorded
    the same work as done. Two records, no gate, so both were believed by
    different readers, and the half of the contract that genuinely had not
    shipped stayed unnoticed the whole time.

A phase header read `Done` above a body whose tasks were still `pending`. The
    delivery it asserted included a schema change that was never made, and the
    assertion is what stopped anyone looking.

A task was `blocked` with no statement of what was blocking it. The two causes
    eventually written down were both wrong, and the verification attached to it
    passed against an unrelated process.

None of these needs judgement to catch. They are a file disagreeing with itself,
which is what this checks, in the same shape as the other gates here: run it, get
`file:line`, get a non-zero exit.

Deliberately not checked: whether a task's `Verify:` command actually passes, or
whether `done` is true. Those need the work run, and a gate that pretends to know
them would be the very thing being guarded against. This only catches a record
that cannot be true on its own terms.

Run locally:
    python scripts/check_task_records.py
    python scripts/check_task_records.py --explain
    python scripts/check_task_records.py --context-root ../.context
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# The planning workspace, relative to this repository. A sibling by design: it is
# a different repository with its own history and no remote.
_DEFAULT_CONTEXT_ROOT = Path(__file__).resolve().parents[1].parent / ".context"

# Templates are not work. Their `Status:` lines are placeholders for whoever
# copies them, and counting them as tasks is how a scan of the workspace reports
# pending work that does not exist.
_EXCLUDED_PARTS = frozenset({"_templates", ".git", "node_modules"})

# The fields every task block carries. Two of them fail silently when missing,
# which is why absence is an error rather than a note: a task with no
# `Path scope:` is treated as touching the whole repository, and one with no
# `Verify:` has no gate at all — both look like an ordinary task in review.
_REQUIRED_FIELDS: tuple[str, ...] = (
    "Status",
    "Owner",
    "Effort",
    "Depends on",
    "Path scope",
    "Verify",
    "Traces to",
)

_VALID_STATUSES = frozenset({"pending", "in-progress", "done", "blocked", "deferred"})

_TASK_HEADING = re.compile(r"^###\s+(?P<id>[A-Z][A-Z0-9]*(?:-[A-Za-z0-9]+)+)\b(?P<rest>.*)$")
_FIELD = re.compile(r"^\*\*(?P<label>[^:*]+):\*\*\s*(?P<value>.*)$")
_BLOCKER = re.compile(r"^\*\*Blocker:\*\*", re.MULTILINE)
# MULTILINE matters: the header status is never the first line of the file, so
# without it this pattern could not match anything and the rule it backs would
# have been dead on arrival — a gate that cannot fire, which is the whole class of
# problem being guarded against. A test that breaks a plan on purpose is what
# caught it.
_PHASE_DONE = re.compile(r"^\*\*Status:\*\*\s*(?:\*\*)?(Done|Shipped|Complete)", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Finding:
    """One way a record contradicts itself."""

    path: Path
    line_no: int
    rule: str
    detail: str


@dataclass
class Task:
    task_id: str
    line_no: int
    fields: dict[str, str]
    body: str


_RULES: dict[str, str] = {
    "current-phase-missing": (
        "A `.current-phase` pointer names a directory that does not exist, or one with no "
        "tasks.md. The orchestrator reads this file to find the active plan, so a stale "
        "pointer means it either fails or silently works the wrong plan. Point it at a real "
        "phase folder, or delete it if the slug has no active phase — absence is how "
        '"nothing is active" is spelled.'
    ),
    "phase-done-with-open-tasks": (
        "A phase header claims Done/Shipped/Complete while its own body still holds pending "
        "or in-progress tasks. Either the header is early or the task statuses were never "
        "flipped; both are the record asserting a delivery that has not happened."
    ),
    "blocked-without-blocker": (
        "A task is blocked or in-progress with no `**Blocker:**` line. An in-progress task "
        "with nothing recorded is indistinguishable from an abandoned one, which is how one "
        "stayed in-progress for three months while the work was actually finished."
    ),
    "unknown-status": (
        "A `**Status:**` value outside pending / in-progress / done / blocked / deferred. "
        "The orchestrator matches these literally and silently drops what it does not "
        "recognise, so a typo reads as a task with no status at all."
    ),
    "missing-field": (
        "A task block is missing a required field. Two of them fail silently: no "
        "`Path scope:` means full-repo scope, and no `Verify:` means no gate — neither is "
        "visible in review."
    ),
    "duplicate-task-id": (
        "Two task blocks share an id within one plan. Commit subjects are prefixed with the "
        "id, so a duplicate makes `git log --grep` ambiguous about which one shipped."
    ),
}


def _in_scope(path: Path) -> bool:
    return not any(part in _EXCLUDED_PARTS for part in path.parts)


def _parse_tasks(text: str) -> list[Task]:
    """Split a plan into task blocks, keyed by heading."""
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, start=1):
        match = _TASK_HEADING.match(line)
        if match:
            starts.append((idx, match.group("id")))

    tasks: list[Task] = []
    for position, (line_no, task_id) in enumerate(starts):
        end = starts[position + 1][0] - 1 if position + 1 < len(starts) else len(lines)
        block = lines[line_no:end]
        fields: dict[str, str] = {}
        for raw in block:
            field = _FIELD.match(raw.strip())
            if field:
                fields.setdefault(field.group("label").strip(), field.group("value").strip())
        tasks.append(Task(task_id=task_id, line_no=line_no, fields=fields, body="\n".join(block)))
    return tasks


def _check_plan(path: Path) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    findings: list[Finding] = []
    tasks = _parse_tasks(text)

    header = text.split("### ", 1)[0]
    header_claims_done = bool(_PHASE_DONE.search(header))
    open_tasks = [t for t in tasks if t.fields.get("Status") in ("pending", "in-progress")]
    if header_claims_done and open_tasks:
        listed = ", ".join(f"{t.task_id} ({t.fields['Status']})" for t in open_tasks[:6])
        findings.append(
            Finding(
                path=path,
                line_no=1,
                rule="phase-done-with-open-tasks",
                detail=f"header claims complete; still open: {listed}",
            )
        )

    seen: dict[str, int] = {}
    for task in tasks:
        if task.task_id in seen:
            findings.append(
                Finding(
                    path=path,
                    line_no=task.line_no,
                    rule="duplicate-task-id",
                    detail=f"{task.task_id} also defined at line {seen[task.task_id]}",
                )
            )
        seen[task.task_id] = task.line_no

        status = task.fields.get("Status")
        if status is None:
            continue
        # A status carrying trailing prose ("done (partial — see commit)") is
        # normal in this workspace; take the leading token.
        head = status.split()[0].strip("*`") if status.split() else ""
        if head not in _VALID_STATUSES:
            findings.append(
                Finding(path=path, line_no=task.line_no, rule="unknown-status", detail=f"{task.task_id}: {status!r}")
            )
        if head in ("blocked", "in-progress") and not _BLOCKER.search(task.body):
            findings.append(
                Finding(
                    path=path,
                    line_no=task.line_no,
                    rule="blocked-without-blocker",
                    detail=f"{task.task_id} is {head} with no **Blocker:** line",
                )
            )
        # Only for work still to be done. On a shipped task a missing field is
        # archaeology; on one about to be picked up it is a live hazard — no
        # `Verify:` means the work merges ungated, and no `Path scope:` means it
        # claims the whole repository. Checking closed tasks too would bury both
        # in a hundred findings about plans that already delivered.
        if head in ("pending", "in-progress", "blocked"):
            missing = [f for f in _REQUIRED_FIELDS if f not in task.fields]
            if missing:
                findings.append(
                    Finding(
                        path=path,
                        line_no=task.line_no,
                        rule="missing-field",
                        detail=f"{task.task_id} is {head} and lacks: {', '.join(missing)}",
                    )
                )
    return findings


def _check_pointers(development: Path) -> list[Finding]:
    findings: list[Finding] = []
    for pointer in sorted(development.rglob(".current-phase")):
        if not _in_scope(pointer):
            continue
        target = pointer.read_text(encoding="utf-8").strip()
        phase = pointer.parent / target
        if not target or not phase.is_dir() or not (phase / "tasks.md").is_file():
            findings.append(
                Finding(
                    path=pointer,
                    line_no=1,
                    rule="current-phase-missing",
                    detail=f"points at {target!r}, which is not a phase folder with a tasks.md",
                )
            )
    return findings


def _explain() -> int:
    print("What this gate checks, and what to do about each:\n")
    for rule, guidance in _RULES.items():
        print(f"  {rule}")
        print(f"    {guidance}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify that task plans do not contradict themselves.")
    parser.add_argument("--context-root", type=Path, default=_DEFAULT_CONTEXT_ROOT)
    parser.add_argument(
        "--if-present",
        action="store_true",
        help="skip rather than fail when the planning workspace is absent (it is a separate repository)",
    )
    parser.add_argument("--explain", action="store_true", help="describe every rule, then exit")
    args = parser.parse_args(argv)

    if args.explain:
        return _explain()

    development = args.context_root / "development"
    if not development.is_dir():
        # Absent is expected in a checkout of this repository alone, and a
        # failure anywhere it is expected trains people to ignore the gate. It
        # is still not silent, and it is still not the default.
        message = f"no planning workspace at {development}"
        if args.if_present:
            print(f"skipped: {message}", file=sys.stderr)
            return 0
        print(
            f"{message}\n\nThis gate reads the sibling planning repository. Pass --if-present to\n"
            "skip where that repository is not checked out, or --context-root to point at it.",
            file=sys.stderr,
        )
        return 1

    plans = [p for p in sorted(development.rglob("tasks.md")) if _in_scope(p)]
    if not plans:
        print(f"no task plans found under {development}", file=sys.stderr)
        return 1

    findings = _check_pointers(development)
    for plan in plans:
        findings.extend(_check_plan(plan))

    if not findings:
        print(f"task-record gate: {len(plans)} plan(s) checked, {len(_RULES)} rule(s) enforced")
        return 0

    for finding in findings:
        print(f"{finding.path}:{finding.line_no}: {finding.rule}: {finding.detail}")
    print(f"\n{len(findings)} record problem(s). Run with --explain for what each one means.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
