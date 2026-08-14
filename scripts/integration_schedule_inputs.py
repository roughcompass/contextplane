"""What a sequence is scheduled against, read from the tree rather than argv.

Three inputs decide how a run is planned, and all three are properties of the
checkout rather than of the invocation: the worker count the repository has
committed, a fingerprint of the migration set the run will apply, and the
duration history keyed to that exact combination.

Kept out of both the runner and the scheduler on purpose. The scheduler is
deliberately free of I/O -- every boundary it enforces is measured in tenths of
a second, and a module that reads files cannot be driven by an injected clock.
The runner is the process that *uses* these, not the authority on where they
come from. Separating them also breaks a would-be import cycle: this module
names nothing the runner owns, so the dependency runs one way.

`frozen_history` takes a collection digest rather than a collection for that
reason -- it needs the digest and nothing else, and taking the object would mean
importing the runner that defines it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from integration_provenance import host_digest
from integration_scheduler import FrozenHistory, HistoryKey

REPOSITORY_ROOT: Final = Path(__file__).resolve().parent.parent


class ScheduleInputError(RuntimeError):
    """An input the schedule depends on is present but unusable.

    Distinct from an absent input, which several of these treat as a legitimate
    "not yet measured" rather than an error.
    """


# --- what the run is scheduled against ----------------------------------------


def schema_fingerprint() -> str:
    """A digest over the migration set the run will apply.

    History measured against a different schema is history about a different
    database, so the fingerprint keys it. The revision *filenames* are enough:
    adding, removing or renaming a revision changes what gets applied, and
    editing an already-applied one is forbidden elsewhere.
    """
    versions = REPOSITORY_ROOT / "contextplane" / "storage" / "migrations" / "versions"
    names = sorted(path.name for path in versions.glob("*.py")) if versions.is_dir() else []
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


def frozen_history(
    collection_digest: str,
    *,
    provider: str,
    workers: int,
    history_root: Path | None = None,
) -> FrozenHistory:
    """Snapshot the durations this sequence will schedule against.

    An absent history is normal and not an error -- the first run of a new
    collection has none, and the scheduler costs unseen nodes at the median of
    what it does know. What must not happen is history keyed loosely enough to
    let one provider's timings schedule another's, which is why the key carries
    all five discriminators even when the durations behind it are empty.
    """
    key = HistoryKey(
        source_collection_digest=collection_digest,
        provider=provider,
        schema_fingerprint=schema_fingerprint(),
        host_digest=host_digest(),
        topology=f"workers={workers}",
    )
    root = history_root or (REPOSITORY_ROOT / "run" / "integration-performance" / "duration-history")
    recorded = root / f"{hashlib.sha256(json.dumps(key.as_evidence(), sort_keys=True).encode()).hexdigest()}.json"
    durations: Mapping[str, float] = {}
    if recorded.is_file():
        durations = {str(node): float(seconds) for node, seconds in json.loads(recorded.read_text("utf-8")).items()}
    return FrozenHistory(key=key, durations=durations)


def workers_supported(workers: int, *, provider: str) -> int:
    """Clamp a worker count to what the resolved provider can actually serve.

    The container provider gives every worker its own server, so workers share
    nothing and the count is whatever the machine carries. The locally managed
    cluster is **one** server for all of them, and its migration-reversibility
    template is named by a content fingerprint every worker computes identically
    -- so concurrent workers race to create it, and the first to finish its
    session drops it while the others are still cloning from it. Measured at 8
    workers: five migration-reversibility nodes fail there and all five pass
    serially.

    Not a tuning knob, then. It is the difference between a provider that serves N
    independent sessions and one that serves a single shared session, and it
    belongs with the other scheduling inputs because it decides the topology the
    history key records.

    Clamped rather than refused, because a developer without a container runtime
    must still be able to run the tier; announced rather than silent, because a
    run that quietly used a count the repository did not commit is the exact thing
    a committed default exists to prevent. Lifting it needs an ownership protocol
    for that template.
    """
    if provider != "devstack" or workers <= 1:
        return workers
    print(
        f"integration runner: {provider} is a single shared server; running 1 worker instead of "
        f"{workers}. Concurrent workers race on the migration template, which has no ownership "
        "protocol yet. The container provider runs the committed count.",
        file=sys.stderr,
    )
    return 1
