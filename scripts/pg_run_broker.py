"""Parent-owned Postgres broker: one server, many isolated databases.

The suite's cost problem is that a real Postgres per worker is
unaffordable and a shared *database* per worker is unsound — the per-test
session commits rather than rolling back, so two workers in one database
see each other's rows. This module takes the only remaining position:
**share the server, never share mutable state.**

One parent process owns the server (a leased devstack cluster, one
testcontainers instance, or a capability-probed external server) and hands
each worker its own freshly cloned database. Workers never construct a
server and never choose a provider; a worker that could do either could
also start a second container mid-run and quietly change what was
measured.

Three things here exist only because the numbers this harness produces are
meant to be trusted:

**The sequence lease.** A measured sequence is a claim about a machine
over a span of time. If unrelated broker work — another test run, a stray
fixture — provisions a database between two children of that sequence, the
second child ran on a different machine state than the first, and the
comparison is void. The lease is exclusive, bound to one controller
identity, and closes broker admission to everything without a valid
control until the controller explicitly finalizes or invalidates it.

**The authenticated child control.** The lease alone cannot tell a genuine
child from any other process that knows the lease exists. Each child
carries an HMAC-SHA-256 control over the full set of facts that identify
its place in the sequence, signed with a secret held only in
controller/broker memory. The broker verifies and *atomically consumes* it
before pytest collection: a replayed control fails, and it fails before
any provider mutation, so a rejected child cannot leave a database behind.

**The manifest handoff.** Workers need full connection URLs, which are
credential-bearing. The parent writes them to a mode-0600 manifest, passes
each worker only its own URL, and deletes the manifest during cleanup.
Evidence keeps redacted digests — enough to prove which assignment a
worker got, never enough to reconstruct the URL.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, SupportsInt, cast

# Capabilities a provider must have before the parent will run workers in
# parallel against it. Anything missing means the run falls back to one
# worker rather than sharing mutable state, which is the failure mode this
# whole module exists to prevent.
_REQUIRED_CAPABILITIES = ("create", "clone", "terminate", "drop")

# How long a child control stays valid. Long enough to cover process
# startup on a loaded machine, short enough that a control captured from a
# process listing cannot be replayed into a later child.
_DEFAULT_CONTROL_TTL_SECONDS = 900

_CONTROL_ENV = "CONTEXTPLANE_INTEGRATION_CONTROL"
_WORKER_URL_ENV = "CONTEXTPLANE_TEST_DATABASE_URL"


class BrokerError(RuntimeError):
    """The broker refused an operation."""


class LeaseError(BrokerError):
    """A lease was overlapping, stale, or not owned by the caller."""


class AdmissionError(BrokerError):
    """Broker admission is closed and the caller presented no valid control."""


class ControlError(BrokerError):
    """A child control was missing, malformed, unauthentic, expired, or replayed."""


class CapabilityError(BrokerError):
    """A provider lacked a capability the requested mode needs."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def redacted_digest(value: str) -> str:
    """A short, non-reversible identity for a secret-bearing string.

    Evidence needs to prove *which* URL a worker was assigned without
    carrying the URL. A truncated SHA-256 does that: two runs assigning the
    same URL produce the same digest, and the digest yields nothing back.
    """
    return _sha256_hex(value.encode("utf-8"))[:16]


# -- capability probing ---------------------------------------------------


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider can actually do, as probed rather than assumed."""

    provider: str
    create: bool = False
    clone: bool = False
    terminate: bool = False
    drop: bool = False
    detail: str = ""

    @property
    def complete(self) -> bool:
        return all(getattr(self, name) for name in _REQUIRED_CAPABILITIES)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(name for name in _REQUIRED_CAPABILITIES if not getattr(self, name))

    def as_evidence(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "capabilities": {name: getattr(self, name) for name in _REQUIRED_CAPABILITIES},
            "complete": self.complete,
            "missing": list(self.missing),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class WorkerPlan:
    """How many workers the parent will actually run, and why.

    A plan that reduces the worker count records the reason: a run that
    silently fell back to one worker would otherwise be reported as a
    parallel measurement.
    """

    workers: int
    parallel: bool
    reason: str

    def as_evidence(self) -> dict[str, object]:
        return {"workers": self.workers, "parallel": self.parallel, "reason": self.reason}


def plan_workers(capabilities: ProviderCapabilities, requested: int) -> WorkerPlan:
    """Decide the worker count from probed capabilities.

    Parallelism requires *every* capability. A provider that can create but
    not clone, or drop but not terminate connections, gets one worker — the
    explicit fallback, never mutable sharing.
    """
    if requested < 1:
        raise CapabilityError(f"requested worker count must be >= 1, got {requested}")
    if requested == 1:
        return WorkerPlan(workers=1, parallel=False, reason="one worker requested")
    if capabilities.complete:
        return WorkerPlan(workers=requested, parallel=True, reason="all provider capabilities probed present")
    missing = ", ".join(capabilities.missing)
    return WorkerPlan(
        workers=1,
        parallel=False,
        reason=(
            f"provider {capabilities.provider} is missing {missing}; "
            "falling back to one worker rather than sharing a mutable database"
        ),
    )


# -- child controls -------------------------------------------------------


@dataclass(frozen=True)
class ControlPayload:
    """The facts a child control binds.

    Every field is here because a sequence could otherwise be spliced
    along that axis: two runs from different controllers, a candidate's
    child replayed into another candidate, a control minted for a
    different provider or product commit. Binding them all means a control
    authenticates one child of one sequence and nothing else.
    """

    controller_id: str
    lease_id: str
    sequence_id: str
    child_sequence_number: int
    mode: str
    role: str
    committed_worker_count: int
    provider: str
    expected_product_commit: str
    host_digest: str
    template_fingerprint: str
    collection_digest: str
    command_digest: str
    nonce: str
    expires_at: str
    candidate: str | None = None

    def canonical_bytes(self) -> bytes:
        """Deterministic bytes to sign.

        Sorted keys and fixed separators so a controller and a broker in
        different processes sign and verify the same byte string.
        """
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "controller_id": self.controller_id,
            "lease_id": self.lease_id,
            "sequence_id": self.sequence_id,
            "child_sequence_number": self.child_sequence_number,
            "mode": self.mode,
            "role": self.role,
            "committed_worker_count": self.committed_worker_count,
            "provider": self.provider,
            "expected_product_commit": self.expected_product_commit,
            "host_digest": self.host_digest,
            "template_fingerprint": self.template_fingerprint,
            "collection_digest": self.collection_digest,
            "command_digest": self.command_digest,
            "nonce": self.nonce,
            "expires_at": self.expires_at,
        }
        if self.candidate is not None:
            payload["candidate"] = self.candidate
        return payload

    def as_evidence(self) -> dict[str, object]:
        """Bound fields minus the replay-sensitive nonce.

        Evidence records what a control asserted, not the token itself.
        """
        payload = self.as_dict()
        payload.pop("nonce", None)
        return payload

    @property
    def expiry(self) -> datetime:
        return datetime.fromisoformat(self.expires_at)


def control_ttl_expiry(seconds: int = _DEFAULT_CONTROL_TTL_SECONDS, *, now: datetime | None = None) -> str:
    base = now if now is not None else _utc_now()
    return (base + timedelta(seconds=seconds)).isoformat()


def sign_control(payload: ControlPayload, secret: bytes) -> str:
    """HMAC-SHA-256 of the canonical payload, hex-encoded."""
    return hmac.new(secret, payload.canonical_bytes(), hashlib.sha256).hexdigest()


def serialize_control(payload: ControlPayload, secret: bytes) -> str:
    """The full control document a child presents.

    The secret never appears in the document — only a MAC computed with
    it, which is what lets the broker verify without the child ever
    holding the signing key.
    """
    return json.dumps(
        {"payload": payload.as_dict(), "mac": sign_control(payload, secret)},
        sort_keys=True,
        separators=(",", ":"),
    )


def write_control_file(path: Path, payload: ControlPayload, secret: bytes) -> Path:
    """Write a mode-0600 control file.

    Created with 0600 from the outset via `os.open` rather than written
    then chmod'ed: between an open-for-write and a chmod there is a window
    where the control is world-readable, and a control is a bearer token
    for admission to the broker.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, serialize_control(payload, secret).encode("utf-8"))
    finally:
        os.close(fd)
    return path


def parse_control(document: str) -> tuple[ControlPayload, str]:
    """Parse a control document into its payload and MAC.

    Raises `ControlError` rather than a JSON or key error so every
    malformed-control path reaches the caller as one refusal type.
    """
    try:
        parsed = json.loads(document)
        raw = parsed["payload"]
        mac = str(parsed["mac"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ControlError(f"control document is malformed: {exc}") from exc

    try:
        return (
            ControlPayload(
                controller_id=str(raw["controller_id"]),
                lease_id=str(raw["lease_id"]),
                sequence_id=str(raw["sequence_id"]),
                child_sequence_number=int(raw["child_sequence_number"]),
                mode=str(raw["mode"]),
                role=str(raw["role"]),
                committed_worker_count=int(raw["committed_worker_count"]),
                provider=str(raw["provider"]),
                expected_product_commit=str(raw["expected_product_commit"]),
                host_digest=str(raw["host_digest"]),
                template_fingerprint=str(raw["template_fingerprint"]),
                collection_digest=str(raw["collection_digest"]),
                command_digest=str(raw["command_digest"]),
                nonce=str(raw["nonce"]),
                expires_at=str(raw["expires_at"]),
                candidate=None if raw.get("candidate") is None else str(raw["candidate"]),
            ),
            mac,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlError(f"control payload is missing or has a wrong field: {exc}") from exc


# -- the exclusive sequence lease -----------------------------------------


@dataclass
class SequenceLease:
    """An exclusive claim on the broker for one measured sequence.

    Holds the sequence-secret in memory only. `__repr__` is overridden
    because a dataclass repr would print the secret into any log line or
    traceback that touches the lease, and a leaked sequence-secret lets
    anything mint admissible controls.
    """

    controller_id: str
    sequence_id: str
    provider: str
    lease_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    acquired_at: datetime = field(default_factory=_utc_now)
    _secret: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)
    _consumed_nonces: set[str] = field(default_factory=set, repr=False)
    _finalized: bool = False
    _invalidated: bool = False

    def __repr__(self) -> str:  # pragma: no cover - defensive against secret leakage
        return (
            f"SequenceLease(controller_id={self.controller_id!r}, lease_id={self.lease_id!r}, "
            f"sequence_id={self.sequence_id!r}, provider={self.provider!r}, active={self.active})"
        )

    @property
    def secret(self) -> bytes:
        """The sequence-secret, for the controller that owns this lease.

        Deliberately a property with no setter and excluded from `repr` and
        every `as_evidence` payload: it must reach a signature computation
        and nothing else.
        """
        return self._secret

    @property
    def active(self) -> bool:
        return not self._finalized and not self._invalidated

    def assert_owner(self, controller_id: str) -> None:
        if controller_id != self.controller_id:
            raise LeaseError(f"lease {self.lease_id} is held by {self.controller_id}, not {controller_id}")
        if not self.active:
            state = "finalized" if self._finalized else "invalidated"
            raise LeaseError(f"lease {self.lease_id} is already {state}")

    def finalize(self, controller_id: str) -> None:
        """Release the lease after a completed sequence."""
        self.assert_owner(controller_id)
        self._finalized = True

    def invalidate(self, controller_id: str, reason: str) -> None:
        """Release the lease after a sequence that must not produce evidence."""
        self.assert_owner(controller_id)
        self._invalidated = True
        self.invalidation_reason = reason

    def verify_and_consume(self, document: str) -> ControlPayload:
        """Authenticate a child control and consume it exactly once.

        Order matters: every rejection below happens before the caller is
        allowed to touch the provider, so a bad control cannot leave a
        database, a connection, or a dropped clone behind.
        """
        if not self.active:
            raise ControlError(f"lease {self.lease_id} is no longer active")

        payload, mac = parse_control(document)

        expected = sign_control(payload, self._secret)
        if not hmac.compare_digest(expected, mac):
            # compare_digest, not `==`: a timing-variable comparison on a MAC
            # is a distinguishing oracle, and this one is reachable by
            # anything that can write a control file.
            raise ControlError("control MAC does not authenticate under this sequence-secret")

        if payload.lease_id != self.lease_id or payload.sequence_id != self.sequence_id:
            raise ControlError(
                f"control is for lease {payload.lease_id}/sequence {payload.sequence_id}, "
                f"not {self.lease_id}/{self.sequence_id}"
            )
        if payload.controller_id != self.controller_id:
            raise ControlError(f"control names controller {payload.controller_id}, not {self.controller_id}")

        if payload.expiry <= _utc_now():
            raise ControlError(f"control expired at {payload.expires_at}")

        if payload.nonce in self._consumed_nonces:
            raise ControlError(f"control nonce {payload.nonce} was already consumed")

        # Recorded last, so a control rejected for any reason above is not
        # burned and a genuine retry of a *failed admission* is still
        # possible; a control that got this far is consumed permanently.
        self._consumed_nonces.add(payload.nonce)
        return payload

    def as_evidence(self) -> dict[str, object]:
        """Non-secret lease identity. Never includes the secret."""
        return {
            "controller_id": self.controller_id,
            "lease_id": self.lease_id,
            "sequence_id": self.sequence_id,
            "provider": self.provider,
            "acquired_at": self.acquired_at.isoformat(),
            "active": self.active,
            "consumed_controls": len(self._consumed_nonces),
        }


# -- the broker manifest --------------------------------------------------


@dataclass
class BrokerManifest:
    """Parent-private map of worker IDs to assigned URLs.

    URL-bearing and therefore never evidence. Written 0600, read by the
    parent to build each child's environment, and deleted during cleanup.
    """

    run_id: str
    assignments: dict[str, str] = field(default_factory=dict)
    cleanup_identities: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    def __repr__(self) -> str:  # pragma: no cover - defensive against URL leakage
        return f"BrokerManifest(run_id={self.run_id!r}, workers={sorted(self.assignments)}, path={self.path!r})"

    def assign(self, worker_id: str, url: str, cleanup_identity: str) -> None:
        self.assignments[worker_id] = url
        self.cleanup_identities[worker_id] = cleanup_identity

    def digest(self) -> str:
        """Digest over the *redacted* assignment map.

        Both parent and child can compute this without the URLs, which is
        what lets a child prove it received the assignment the manifest
        recorded without the digest itself carrying a credential.
        """
        redacted = {worker: redacted_digest(url) for worker, url in sorted(self.assignments.items())}
        canonical = json.dumps({"run_id": self.run_id, "assignments": redacted}, sort_keys=True, separators=(",", ":"))
        return _sha256_hex(canonical.encode("utf-8"))

    def write(self, directory: Path | None = None) -> Path:
        """Write the manifest 0600 and remember where it went."""
        base = directory if directory is not None else Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        target = base / f"cp-broker-manifest-{self.run_id}.json"
        document = json.dumps(
            {
                "run_id": self.run_id,
                "assignments": self.assignments,
                "cleanup_identities": self.cleanup_identities,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, document.encode("utf-8"))
        finally:
            os.close(fd)
        self.path = target
        return target

    def worker_environment(self, worker_id: str) -> dict[str, str]:
        """The two variables a worker needs, and nothing else."""
        if worker_id not in self.assignments:
            raise BrokerError(f"no assignment for worker {worker_id!r}")
        return {
            _WORKER_URL_ENV: self.assignments[worker_id],
            "CONTEXTPLANE_BROKER_MANIFEST_DIGEST": self.digest(),
        }

    def delete(self) -> None:
        """Remove the private manifest. Idempotent."""
        if self.path is not None and self.path.exists():
            self.path.unlink()
        self.path = None

    def as_evidence(self) -> dict[str, object]:
        """Redacted digests only — never a URL."""
        return {
            "run_id": self.run_id,
            "manifest_digest": self.digest(),
            "assignments": {worker: redacted_digest(url) for worker, url in sorted(self.assignments.items())},
            "cleanup_identities": sorted(self.cleanup_identities.values()),
        }


# -- inventory ------------------------------------------------------------


@dataclass(frozen=True)
class Inventory:
    """A snapshot of server state taken before and after a measured run."""

    databases: tuple[str, ...]
    sessions: int
    templates: tuple[str, ...]

    def unexpected_against(self, baseline: Inventory) -> dict[str, object]:
        """What appeared that the baseline did not have.

        A measured sequence that leaves a database, a session, or a changed
        template behind is not a clean measurement, so the difference is
        reported rather than tolerated.
        """
        return {
            "new_databases": sorted(set(self.databases) - set(baseline.databases)),
            "removed_templates": sorted(set(baseline.templates) - set(self.templates)),
            "session_delta": self.sessions - baseline.sessions,
        }

    def matches(self, baseline: Inventory) -> bool:
        difference = self.unexpected_against(baseline)
        return (
            not difference["new_databases"]
            and not difference["removed_templates"]
            and int(cast("SupportsInt", difference["session_delta"])) <= 0
        )


# -- the broker -----------------------------------------------------------


@dataclass
class BrokerBoundary:
    """One timed provisioning or cleanup boundary.

    Recorded so the evidence can say where wall-clock went without the
    caller having to instrument each call site separately.
    """

    name: str
    seconds: float
    detail: str = ""

    def as_evidence(self) -> dict[str, object]:
        return {"boundary": self.name, "seconds": round(self.seconds, 6), "detail": self.detail}


class RunBroker:
    """Owns every database a run creates, and hands out no server.

    SQL runs through an injected `execute` callable so the same broker
    drives a devstack cluster (`psql`) and a testcontainers server
    (asyncpg) without knowing which. Unit tests inject a recorder and
    assert on the statements; the integration test injects a real one.
    """

    def __init__(
        self,
        *,
        provider: str,
        execute: Callable[[str], Any],
        list_databases: Callable[[], Sequence[str]] | None = None,
        count_sessions: Callable[[], int] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.provider = provider
        self._execute = execute
        self._list_databases = list_databases or (lambda: ())
        self._count_sessions = count_sessions or (lambda: 0)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._owned: list[str] = []
        self._admission_closed = False
        self._lease: SequenceLease | None = None
        self.boundaries: list[BrokerBoundary] = []

    # -- admission --------------------------------------------------------

    def open_sequence(self, controller_id: str, sequence_id: str) -> SequenceLease:
        """Take the exclusive lease and close admission.

        Overlap is rejected rather than queued: a second controller waiting
        for the lease would start measuring the moment the first released,
        against a machine the first one had just finished loading.
        """
        if self._lease is not None and self._lease.active:
            raise LeaseError(
                f"provider {self.provider} is already leased by {self._lease.controller_id} "
                f"for sequence {self._lease.sequence_id}"
            )
        self._lease = SequenceLease(controller_id=controller_id, sequence_id=sequence_id, provider=self.provider)
        self._admission_closed = True
        return self._lease

    def close_sequence(self, controller_id: str, *, reason: str | None = None) -> None:
        """Finalize or invalidate the lease and reopen admission."""
        if self._lease is None:
            raise LeaseError("no lease to close")
        if reason is None:
            self._lease.finalize(controller_id)
        else:
            self._lease.invalidate(controller_id, reason)
        self._admission_closed = False

    @property
    def admission_closed(self) -> bool:
        return self._admission_closed

    def admit(self, control_document: str | None) -> ControlPayload | None:
        """Gate every provisioning call while a sequence is open.

        With admission closed, a caller with no valid control is refused
        *before* any provider mutation — that ordering is the whole point,
        since a refusal that had already created a database would leave the
        run dirty.
        """
        if not self._admission_closed:
            return None
        if self._lease is None:  # pragma: no cover - admission is only closed with a lease
            raise AdmissionError("admission is closed but no lease is held")
        if control_document is None:
            raise AdmissionError(
                f"broker admission is closed for sequence {self._lease.sequence_id}; "
                f"a valid {_CONTROL_ENV} control is required"
            )
        return self._lease.verify_and_consume(control_document)

    # -- instrumentation --------------------------------------------------

    @contextmanager
    def boundary(self, name: str, detail: str = "") -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.boundaries.append(BrokerBoundary(name=name, seconds=time.monotonic() - started, detail=detail))

    # -- database lifecycle ----------------------------------------------

    def database_name(self, kind: str, label: str) -> str:
        """A unique name per run and consumer.

        `kind` separates worker databases from migration scratch and
        embedding scenarios so a leak is attributable to the consumer that
        caused it rather than to "the run".
        """
        safe = "".join(ch if ch.isalnum() else "_" for ch in label).strip("_").lower()
        return f"cp_{kind}_{self.run_id}_{safe}"[:63]

    def create_database(self, name: str, *, control: str | None = None) -> str:
        self.admit(control)
        with self.boundary("create_database", name):
            self._execute(f'CREATE DATABASE "{name}"')
        self._owned.append(name)
        return name

    def clone_database(self, name: str, *, template: str, control: str | None = None) -> str:
        """Clone *template* into *name*, outside a transaction.

        `CREATE DATABASE ... TEMPLATE` cannot run inside a transaction
        block, so this must never be wrapped in one by a caller trying to
        make provisioning atomic.
        """
        self.admit(control)
        with self.boundary("clone_database", f"{template}->{name}"):
            self._execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
        self._owned.append(name)
        return name

    def terminate_connections(self, name: str) -> None:
        """Disconnect every backend on *name* except this one."""
        with self.boundary("terminate_connections", name):
            self._execute(
                # `name` is a database identifier this broker minted via database_name(),
                # which reduces every non-alphanumeric character to `_`; no caller-supplied
                # string reaches the statement.
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "  # noqa: S608
                f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
            )

    def drop_database(self, name: str) -> None:
        """Terminate then drop, idempotently.

        The terminate has to come first: a `DROP DATABASE` with a live
        backend attached fails, and the backend most likely to be attached
        is one the run itself leaked.
        """
        with self.boundary("drop_database", name):
            self.terminate_connections(name)
            self._execute(f'DROP DATABASE IF EXISTS "{name}"')
        if name in self._owned:
            self._owned.remove(name)

    def disable_connections(self, name: str) -> None:
        """Mark a published template unconnectable."""
        self._execute(f"UPDATE pg_database SET datallowconn = false WHERE datname = '{name}'")  # noqa: S608 - same broker-minted identifier as terminate_connections(); no caller-supplied string reaches the statement

    def cleanup(self) -> list[str]:
        """Drop everything this broker created. Safe to call twice.

        Failures are collected rather than raised: a cleanup that stops at
        the first error leaves the rest of the run's databases behind, and
        the next run then inherits them.
        """
        failures: list[str] = []
        for name in list(self._owned):
            try:
                self.drop_database(name)
            except Exception as exc:  # noqa: BLE001 - cleanup collects every failure deliberately; narrowing this would let one undroppable database strand the rest for the next run to inherit
                failures.append(f"{name}: {exc}")
        return failures

    @property
    def owned_databases(self) -> tuple[str, ...]:
        return tuple(self._owned)

    # -- inventory --------------------------------------------------------

    def inventory(self) -> Inventory:
        databases = tuple(sorted(self._list_databases()))
        return Inventory(
            databases=databases,
            sessions=self._count_sessions(),
            templates=tuple(name for name in databases if name.startswith("cp_tmpl_")),
        )

    def as_evidence(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "run_id": self.run_id,
            "owned_databases": list(self._owned),
            "admission_closed": self._admission_closed,
            "lease": self._lease.as_evidence() if self._lease is not None else None,
            "boundaries": [boundary.as_evidence() for boundary in self.boundaries],
        }
