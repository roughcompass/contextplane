#!/usr/bin/env python3
"""The only implementation behind the canonical integration-test target.

A worker flag cannot do this job. Sealing collection, owning one server,
proving every node reported exactly once, and refusing a zero-test run are
properties of a runner, and a flag on somebody else's runner can be overridden
by the caller who sets it. So the target invokes this, this invokes pytest as
`sys.executable -m pytest`, and nothing in between accepts an argument from a
caller.

Qualification is the security boundary and it fails *closed on presence*, not
on effect. If a caller exports `PYTEST=true` or preloads a Makefile that
replaces the interpreter, scrubbing that variable out of the child would
produce a clean run and a passing gate — which is precisely the outcome that
makes the evidence worthless, because the attempt succeeded at hiding itself.
The attempt is therefore the failure. Evidence records which variable names
were attempted and never their values, since the values are exactly the sort
of thing that should not be written down.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess  # noqa: S404 - the whole point of this runner is to spawn a sealed child
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parent.parent
INTEGRATION_ROOT: Final = "tests/integration"

# Channels through which a caller could change what runs, what is reported, or
# which interpreter does the running. Each is listed by exact name rather than
# by prefix where the name is fixed, because a prefix match invites a later
# reader to assume the set is approximate. `PYTEST_*` is the one genuine
# family: pytest reads option channels from it that no fixed list can enumerate
# across versions.
_FORBIDDEN_EXACT: Final = frozenset(
    {
        # Replace the runner or the interpreter outright.
        "PYTEST",
        "PYTHON",
        # Inject pytest options without touching argv.
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_DEBUG",
        # Make-level command, flag, and file overrides. `MAKEFILES` is the
        # subtle one: it preloads a makefile before the target's own, so it
        # can redefine PYTEST or PYTHON without ever appearing in the
        # environment as those names.
        "MAKEFLAGS",
        "MFLAGS",
        "GNUMAKEFLAGS",
        "MAKEOVERRIDES",
        "MAKEFILES",
        # An inherited control would let one sequence's authentication be
        # replayed into another's child.
        "CONTEXTPLANE_INTEGRATION_CONTROL_INHERITED",
    }
)

_FORBIDDEN_PREFIXES: Final = ("PYTEST_", "GIT_")

# Variables the child genuinely needs. Everything else is dropped rather than
# forwarded: an allowlist that grows by accident is not an allowlist.
_CHILD_ALLOWLIST: Final = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TZ",
        "PYTHONPATH",
        "PYTHONHASHSEED",
        "VIRTUAL_ENV",
        "CONTEXTPLANE_TEST_PG",
        "CONTEXTPLANE_TEST_DATABASE_URL",
        "CONTEXTPLANE_INTEGRATION_CONTROL",
        "CONTEXTPLANE_PG_BINDIR",
        "DOCKER_HOST",
    }
)

# Only these plugins load. Autoload is off, so a plugin installed in the
# environment cannot join a measured run and change its timing or its
# reporting without appearing here first.
_REQUIRED_PLUGINS: Final = ("pytest_asyncio.plugin", "pytest_timeout")

# Argv shapes that reselect, reorder, or re-run the suite. A measured run whose
# selection differs from the collection digest is measuring a different suite.
_FORBIDDEN_ARGV_FLAGS: Final = (
    "-k",
    "-m",
    "-x",
    "--deselect",
    "--last-failed",
    "--lf",
    "--failed-first",
    "--ff",
    "--stepwise",
    "--maxfail",
    "--reruns",
    "--flaky",
    "--only-rerun",
    "-n",
    "--numprocesses",
    "--dist",
    "--shard-id",
    "--num-shards",
)


class QualificationError(RuntimeError):
    """The invocation cannot produce qualifying evidence.

    Raised before collection and before any provider mutation, so a rejected
    attempt costs nothing and changes nothing.
    """


@dataclass(frozen=True)
class QualificationFailure:
    """What was attempted, by name only."""

    attempted_variables: tuple[str, ...]
    attempted_arguments: tuple[str, ...]
    reason: str

    def as_evidence(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            # Names, never values. A rejected attempt is still an attempt to
            # smuggle something in, and writing the payload into the artifact
            # would carry it to every reader of the bundle.
            "attempted_variables": list(self.attempted_variables),
            "attempted_arguments": list(self.attempted_arguments),
        }


def forbidden_variables(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Every forbidden channel actually present, sorted for stable evidence."""
    found = {name for name in environ if name in _FORBIDDEN_EXACT or name.startswith(_FORBIDDEN_PREFIXES)}
    return tuple(sorted(found))


def forbidden_arguments(argv: Sequence[str]) -> tuple[str, ...]:
    """Selector-shaped argv, including the `--flag=value` spelling."""
    found: list[str] = []
    for argument in argv:
        head = argument.split("=", 1)[0]
        if head in _FORBIDDEN_ARGV_FLAGS:
            found.append(head)
    return tuple(sorted(set(found)))


def qualify(environ: Mapping[str, str], argv: Sequence[str]) -> None:
    """Refuse an invocation that could have changed what runs.

    Presence is the failure, not effect. Scrubbing a forbidden variable and
    continuing would turn a tampered invocation into a passing gate, which is
    the one outcome that makes the whole sealed-evidence scheme pointless.
    """
    variables = forbidden_variables(environ)
    arguments = forbidden_arguments(argv)
    if not variables and not arguments:
        return

    reasons = []
    if variables:
        reasons.append(f"forbidden environment channel(s) present: {', '.join(variables)}")
    if arguments:
        reasons.append(f"forbidden argument(s): {', '.join(arguments)}")
    failure = QualificationFailure(
        attempted_variables=variables,
        attempted_arguments=arguments,
        reason="; ".join(reasons),
    )
    raise QualificationError(failure.reason)


def build_child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """A fresh allowlisted environment, built up rather than filtered down.

    Constructed from nothing so a variable that nobody thought about is absent
    by default. Filtering an inherited environment has the opposite default,
    and the difference shows up the first time somebody invents a new channel.
    """
    child = {name: environ[name] for name in sorted(_CHILD_ALLOWLIST) if name in environ}
    # Autoload off is not a preference. With it on, any plugin present in the
    # environment joins a measured run and can change both its timing and what
    # it reports, so two runs of one commit on two machines are not comparable.
    child["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # Hash randomization changes dict and set iteration order, which changes
    # collection order, which changes the collection digest. Pinning it makes
    # the digest a property of the tree rather than of the process.
    child["PYTHONHASHSEED"] = "0"
    return child


def collection_command() -> list[str]:
    """The exact argv used to enumerate the suite. No caller input reaches it."""
    return [
        sys.executable,
        "-m",
        "pytest",
        INTEGRATION_ROOT,
        "--collect-only",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        *[argument for plugin in _REQUIRED_PLUGINS for argument in ("-p", plugin)],
    ]


def worker_command(node_ids: Sequence[str]) -> list[str]:
    """One worker's argv. Node IDs come from our own collection, not a caller."""
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        *[argument for plugin in _REQUIRED_PLUGINS for argument in ("-p", plugin)],
        *node_ids,
    ]


def collection_digest(node_ids: Sequence[str]) -> str:
    """A digest over the sorted node list.

    Sorted so that collection order — which pytest does not promise across
    filesystems — cannot change the digest, while adding or removing a single
    test always does.
    """
    payload = "\n".join(sorted(node_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Collection:
    """What the suite contains, and the digest that pins it."""

    node_ids: tuple[str, ...]
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "digest", collection_digest(self.node_ids))

    def as_evidence(self) -> dict[str, object]:
        return {"node_count": len(self.node_ids), "collection_digest": self.digest}


def parse_collection(stdout: str) -> tuple[str, ...]:
    """Read node IDs out of `--collect-only -q` output.

    Everything after the first blank line is pytest's summary, and lines
    without `::` are directory headers or warnings. Both are excluded by shape
    rather than by pattern-matching pytest's prose, which changes between
    versions.
    """
    node_ids: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            break
        if "::" not in line:
            continue
        node_ids.append(line)
    return tuple(node_ids)


def collect(environ: Mapping[str, str], *, cwd: Path | None = None) -> Collection:
    """Enumerate the whole integration root. An empty result is fatal.

    A zero-node run that exits 0 is the failure mode this whole phase exists
    to make impossible: it looks exactly like a fast, healthy suite.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, no caller input
        collection_command(),
        env=build_child_environment(environ),
        cwd=str(cwd or REPOSITORY_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        msg = f"collection failed with exit {completed.returncode}: {completed.stderr.strip()[:400]}"
        raise QualificationError(msg)

    node_ids = parse_collection(completed.stdout)
    if not node_ids:
        msg = "collection produced zero nodes; a zero-test run cannot qualify"
        raise QualificationError(msg)
    return Collection(node_ids=node_ids)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Deliberately tiny.

    The runner takes a worker count and nothing else. There is no passthrough
    for pytest arguments, and adding one would reopen every channel
    qualification closes.
    """
    parser = argparse.ArgumentParser(
        prog="run_integration_tests.py",
        description="Run the integration tier under sealed collection and scheduling.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Worker count. Defaults to the tracked committed default.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        qualify(os.environ, arguments)  # config: intentional - the ambient environment is the thing under inspection
    except QualificationError as error:
        print(f"integration runner: refusing to run: {error}", file=sys.stderr)
        return 2

    _parse_args(arguments)
    try:
        collection = collect(os.environ)  # config: intentional - the child environment is built from the ambient one
    except QualificationError as error:
        print(f"integration runner: {error}", file=sys.stderr)
        return 2

    print(f"integration runner: collected {len(collection.node_ids)} nodes ({collection.digest[:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
