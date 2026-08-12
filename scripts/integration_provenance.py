"""Which repository, which commit, and was the tree clean — answered once, safely.

Every number this phase produces is a claim about one tree at one commit. That
claim is only as good as the process that established it, and Git is unusually
easy to redirect: `GIT_DIR` alone makes `git -C <path>` answer about a
completely different repository, so a controller that inherited one could
certify a measurement against a tree nobody ran. `GIT_INDEX_FILE` and
`GIT_OBJECT_DIRECTORY` do the same thing one layer down.

So the whole namespace is rejected at entry rather than scrubbed and forgiven.
Scrubbing alone has the failure mode this phase exists to prevent: the run
succeeds, the evidence looks clean, and the attempt to redirect it left no
trace. Presence is the failure; the *names* attempted are recorded, never their
values, because the values are precisely the paths somebody wanted followed.

Three further properties are load-bearing:

- **One executable, resolved once.** `git` is realpath'd a single time and that
  absolute path is used for every call. Re-resolving per call lets a `PATH`
  change between two calls answer as two different programs.
- **The top level must be the root we were told to measure.** Resolution runs
  through the sanitized environment and the answer is compared by realpath, so
  a symlinked or nested checkout cannot pass itself off as the canonical one.
- **Ignored run output is not dirt.** Evidence lands under `run/`, which is
  gitignored, and a controller that called its own output a dirty tree could
  never take a second measurement. Everything else — any tracked modification,
  any non-ignored untracked path — invalidates qualification.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess  # noqa: S404 - runs git only, fixed argv, realpath'd binary, scrubbed env
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: The canonical product root, derived from this file rather than from the
#: caller's working directory. A controller that resolved its root from `cwd`
#: would measure whichever tree it happened to be launched in.
PRODUCT_ROOT: Final = Path(__file__).resolve().parent.parent

#: Evidence lives here and is gitignored. Output the controller itself wrote
#: must not read as a dirty tree, or no second measurement is ever possible.
IGNORED_OUTPUT_PREFIX: Final = "run/"

_GIT_PREFIX: Final = "GIT_"


class ProvenanceError(RuntimeError):
    """The repository, commit, or tree state cannot be established safely."""


class DirtyTree(ProvenanceError):
    """The tree changed under the measurement. Its own type so a caller that
    handles provenance failures broadly cannot swallow this one by accident."""


def attempted_git_variables(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Every inherited `GIT_*` name, sorted. Names only — never values."""
    return tuple(sorted(name for name in environ if name.startswith(_GIT_PREFIX)))


def reject_inherited_git(environ: Mapping[str, str]) -> None:
    """Refuse at entry if anything could redirect Git.

    The whole family, not a curated list of the dangerous ones. A curated list
    is a promise that nobody will add a redirecting variable to Git later, and
    that promise is not ours to make.
    """
    attempted = attempted_git_variables(environ)
    if not attempted:
        return
    msg = (
        "refusing to run with inherited Git environment: "
        + ", ".join(attempted)
        + ". Each of these can make `git -C <path>` answer about a different repository, which "
        "would let this controller certify a measurement against a tree nobody ran. Unset them "
        "and re-run."
    )
    raise ProvenanceError(msg)


def sanitized_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment Git and every child sees: no `GIT_*`, no prompting."""
    sanitized = {name: value for name, value in environ.items() if not name.startswith(_GIT_PREFIX)}
    # A Git call that blocks on a credential prompt inside a timed sequence
    # would be indistinguishable from a slow run.
    sanitized["GIT_TERMINAL_PROMPT"] = "0"
    return sanitized


def resolve_git_executable() -> str:
    """One absolute, realpath'd Git for the whole sequence.

    Resolved once and reused. Looking it up per call would let a `PATH` change
    between two calls answer as two different programs, and the second one
    would still be recorded under the first one's provenance.
    """
    found = shutil.which("git")
    if not found:
        msg = "no git executable on PATH; provenance cannot be established"
        raise ProvenanceError(msg)
    return os.path.realpath(found)


@dataclass(frozen=True)
class GitContext:
    """A pinned executable anchored at a verified top level.

    Constructed through `open_git()` so that a caller cannot hold one whose
    top level was never checked. Every call goes through `run()`, so there is
    exactly one place where an argv reaches a subprocess.
    """

    executable: str
    root: Path
    environment: Mapping[str, str]

    def run(self, *arguments: str) -> str:
        completed = subprocess.run(  # noqa: S603 - realpath'd git, fixed argv, sanitized env
            [self.executable, "-C", str(self.root), *arguments],
            capture_output=True,
            text=True,
            env=dict(self.environment),
            check=False,
        )
        if completed.returncode != 0:
            msg = f"git {' '.join(arguments)} failed: {completed.stderr.strip()[:400]}"
            raise ProvenanceError(msg)
        return completed.stdout

    def resolve_commit(self, revision: str) -> str:
        """A full object ID for a revision, or a refusal.

        `^{commit}` peels tags and refuses a tree or a blob, so a caller cannot
        bind a sequence to something that is not a commit.
        """
        return self.run("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").strip()

    def status_entries(self) -> tuple[str, ...]:
        """Porcelain v1 status lines, with ignored evidence output removed.

        `--untracked-files=all` lists every untracked path individually rather
        than collapsing a directory, so a single stray file inside an otherwise
        expected directory cannot hide behind its parent.
        """
        raw = self.run("status", "--porcelain=v1", "--untracked-files=all")
        entries: list[str] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            if _is_ignored_output(line):
                continue
            entries.append(line)
        return tuple(entries)

    def assert_clean(self, *, checkpoint: str) -> tuple[str, ...]:
        """A clean-tree checkpoint. Returns the (empty) entries for the record.

        Called before the sequence, around every child, and after the sequence.
        A tree that changed mid-sequence means the later children measured
        something the earlier ones did not, and no amount of averaging fixes
        that.
        """
        entries = self.status_entries()
        if entries:
            shown = ", ".join(entries[:10])
            msg = (
                f"working tree is not clean at checkpoint {checkpoint!r}: {len(entries)} entry/entries "
                f"({shown}). Ignored output under {IGNORED_OUTPUT_PREFIX} is permitted; a tracked "
                "modification or a non-ignored untracked path is not, because it means the measured "
                "tree is not the commit the evidence names."
            )
            raise DirtyTree(msg)
        return entries


def _is_ignored_output(status_line: str) -> bool:
    """Is this status entry the controller's own gitignored evidence?

    Porcelain v1 is `XY <path>`, with a rename spelled `XY <old> -> <new>`.
    Both sides of a rename must be ignored output for the line to be excused —
    a tracked file renamed *into* `run/` is a tracked modification wearing a
    permitted path.
    """
    payload = status_line[3:] if len(status_line) > 3 else ""
    if not payload:
        return False
    candidates = payload.split(" -> ") if " -> " in payload else [payload]
    return all(candidate.strip().strip('"').startswith(IGNORED_OUTPUT_PREFIX) for candidate in candidates)


def open_git(environ: Mapping[str, str], *, expected_root: Path | None = None) -> GitContext:
    """Reject inherited Git, pin one executable, and verify the top level.

    The top-level comparison is by realpath on both sides. Comparing the
    strings would let `/tmp/x` and a symlink to it disagree, and comparing
    only Git's answer would accept any checkout that happened to be under the
    caller's cwd.
    """
    reject_inherited_git(environ)
    root = (expected_root or PRODUCT_ROOT).resolve()
    context = GitContext(
        executable=resolve_git_executable(),
        root=root,
        environment=sanitized_environment(environ),
    )
    reported = Path(context.run("rev-parse", "--show-toplevel").strip()).resolve()
    if reported != root:
        msg = (
            f"git reports top level {reported}, but the canonical product root is {root}. "
            "The controller measures the tree it was built from, not the one it was launched in."
        )
        raise ProvenanceError(msg)
    return context


@dataclass(frozen=True)
class CommitBinding:
    """The commit a sequence is bound to, resolved two independent ways.

    `expected` comes from the caller's `--expected-commit`; `head` is whatever
    `HEAD^{commit}` currently resolves to. They are recorded separately and
    compared rather than one being taken as the other, because the failure this
    catches — a checkout moving under a running sequence — makes them differ
    while either one alone still looks perfectly reasonable.
    """

    expected: str
    head: str

    @property
    def agrees(self) -> bool:
        return self.expected == self.head

    def as_evidence(self) -> dict[str, str]:
        return {"expected_commit": self.expected, "head_commit": self.head}


def bind_commit(context: GitContext, expected_commit: str) -> CommitBinding:
    """Resolve both sides and refuse a mismatch.

    Re-run before every child and again after the sequence. Checking once at
    the start proves the tree was right when nobody had run anything yet, which
    is the least interesting moment to check it.
    """
    if not expected_commit:
        msg = "--expected-commit is required; a sequence that names no commit certifies nothing"
        raise ProvenanceError(msg)
    binding = CommitBinding(
        expected=context.resolve_commit(expected_commit),
        head=context.resolve_commit("HEAD"),
    )
    if not binding.agrees:
        msg = (
            f"commit drift: --expected-commit resolves to {binding.expected[:12]} but HEAD is "
            f"{binding.head[:12]}. The checkout moved, so the evidence would name a commit the "
            "run did not measure."
        )
        raise ProvenanceError(msg)
    return binding


def host_digest() -> str:
    """A stable, non-identifying fingerprint of the measuring machine.

    Duration history keyed on this cannot schedule one machine's run from
    another's timings. Hashed rather than recorded verbatim: the hostname is
    the one field here that identifies a person's laptop, and the comparison
    this feeds only ever asks whether two records came from the same host.
    """
    payload = "\x00".join(
        (
            platform.system(),
            platform.machine(),
            platform.release().split("-")[0],
            str(os.cpu_count() or 0),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
